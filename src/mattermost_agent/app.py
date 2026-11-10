"""The app loop: connect, catch up, listen, and start again when the connection drops.

Each connection opens the WebSocket first and catches up second, so a post arriving during
catch-up waits on the socket instead of falling between the two. Handled ids remove the overlap.

Only live posts move last-seen, in the store. If the app dies during catch-up, the next start
simply repeats the catch-up.
"""

import asyncio
import os
import random
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from mattermost_agent.classify import classify, is_ignored, is_own
from mattermost_agent.events import (
    Channel,
    MalformedEvent,
    Me,
    Post,
    dm_partner,
    parse_websocket_message,
)
from mattermost_agent.logs import JobEvent, log_job, log_lifecycle, log_post
from mattermost_agent.mattermost import MattermostClient, MattermostError
from mattermost_agent.rules import RuleSet, first_match
from mattermost_agent.settings import DEFAULT_STATE_DIR
from mattermost_agent.store import RETENTION_MS, Job, Store, StoreError

# Catch-up starts this far before last-seen, because the clocks of servers can differ a little.
OVERLAP_MS = 5 * 60 * 1000
MAX_BACKOFF_SECONDS = 60
MAX_BACKOFF_DOUBLINGS = 6
# A call that may work a moment later is tried again after each of these waits.
RETRY_DELAYS_SECONDS = (1.0, 2.0)
HEARTBEAT_FILE = "heartbeat"
# The heartbeat and the check for bursts that became ready share one loop, so this is also how
# late, at most, a burst is marked ready after its quiet time.
HOUSEKEEPING_SECONDS = 5.0
HEARTBEAT_MAX_AGE_SECONDS = 45.0
PRUNE_SECONDS = 3600.0
# Docker kills the container 10 seconds after asking it to stop. This gets there first, in case
# something such as a DNS lookup in a background thread will not stop.
WATCHDOG_SECONDS = 9.0


def start_watchdog(seconds: float = WATCHDOG_SECONDS) -> None:
    """Force the process to end after `seconds`, however stuck the rest of it is."""
    timer = threading.Timer(seconds, os._exit, args=(1,))
    timer.daemon = True
    timer.start()


@dataclass
class Runtime:
    """Everything the loop talks to, so that every test can run offline."""

    client: MattermostClient
    store: Store
    rules: RuleSet
    connect_events: Callable[[], Awaitable[ClientConnection]]
    heartbeat_file: Path
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[], float] = random.random
    on_stop: Callable[[], None] = start_watchdog


def backoff_delay(failures: int, jitter: float) -> float:
    """Return the wait before the next attempt: doubling, randomised, 60 seconds at most.

    `jitter` is between 0 and 1. The randomness stops many clients reconnecting in step after
    the server comes back.
    """
    ceiling = min(MAX_BACKOFF_SECONDS, 2 ** min(failures, MAX_BACKOFF_DOUBLINGS))
    return ceiling * (0.5 + jitter / 2)


def _is_passing(error: MattermostError) -> bool:
    """Say whether the same call could work a moment later."""
    return (
        error.reason == "unreachable"
        or error.status == HTTPStatus.TOO_MANY_REQUESTS
        or (error.status is not None and error.status >= HTTPStatus.INTERNAL_SERVER_ERROR)
    )


async def _with_retries[**P, T](
    runtime: Runtime,
    call: Callable[P, Awaitable[T]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    for delay in RETRY_DELAYS_SECONDS:
        try:
            return await call(*args, **kwargs)
        except MattermostError as error:
            if not _is_passing(error):
                raise
        await runtime.sleep(delay)
    return await call(*args, **kwargs)


async def _username(runtime: Runtime, user_id: str, *, live: bool) -> str | None:
    """Return a username, or None if the lookup fails in a way that must not stop the listener.

    After a passing failure, catch-up raises: it runs again later, loses nothing and keeps the
    sender for `from` rules. A live post is not sent twice, so it goes on without a sender.
    """
    try:
        return await _with_retries(runtime, runtime.client.username, user_id)
    except MattermostError as error:
        if error.reason == "auth_rejected" or (_is_passing(error) and not live):
            raise
        log_lifecycle(
            "lookup_failed", reason=error.reason, status=error.status, error=error.__cause__
        )
        return None


async def _logged_channel(runtime: Runtime, me: Me, post: Post, *, live: bool) -> str | None:
    """Return the name to log: the channel's, or for a DM I sent, who it went to.

    A DM's own name is only two user ids, and on a DM sent to me the sender says more.
    """
    if post.channel_type in {"O", "P"}:
        return post.channel_name
    if post.channel_type != "D" or not is_own(post, me):
        return None
    partner = dm_partner(post.channel_name, me.user_id)
    username = None if partner is None else await _username(runtime, partner, live=live)
    return None if username is None else f"@{username}"


def _log_job(event: JobEvent, job: Job) -> None:
    log_job(
        event,
        job_id=job.id,
        status=job.status,
        scenario=job.scenario,
        rule=job.rule,
        place=job.place,
        post_count=job.post_count,
    )


async def _handle_post(runtime: Runtime, me: Me, post: Post, *, live: bool) -> None:
    if is_ignored(post):
        return
    kind = classify(post, me)
    sender = await _username(runtime, post.user_id, live=live)
    channel = await _logged_channel(runtime, me, post, live=live)
    rule = first_match(runtime.rules, post, sender, own=is_own(post, me))
    # Where the job came from, for the jobs listing: the channel, or `@` and the other person.
    place = channel or (f"@{sender}" if post.channel_type == "D" and sender else None)
    outcome = runtime.store.claim(
        post,
        kind=kind,
        rule=rule,
        place=place,
        live=live,
        now=runtime.clock(),
        cap=runtime.rules.jobs_per_hour,
        bursts=runtime.rules.bursts,
    )
    if outcome.claim == "duplicate":
        return
    log_post(
        "cap_hit" if outcome.claim == "cap_hit" else "post",
        kind=kind,
        channel=channel,
        sender=sender,
        length=len(post.message),
        rule=None if rule is None else rule.name,
        scenario=None if rule is None else rule.scenario,
    )
    if outcome.job is not None:
        _log_job("job_opened" if outcome.claim == "opened" else "job_joined", outcome.job)
    for job in outcome.closed:
        _log_job("job_ready", job)


def _needs_catch_up(channel: Channel, named: frozenset[str], floor: int) -> bool:
    """Read DMs and the channels the rules name, and only those with something new."""
    wanted = channel.type in {"D", "G"} or channel.name.casefold() in named
    return wanted and channel.last_post_at > floor


async def _catch_up(runtime: Runtime, me: Me) -> None:
    last_seen = runtime.store.last_seen()
    if last_seen is None:
        return
    oldest_allowed = int(runtime.clock() * 1000) - RETENTION_MS
    floor = max(last_seen - OVERLAP_MS, oldest_allowed)
    truncated = floor == oldest_allowed

    channels = await runtime.client.list_channels()
    named = runtime.rules.channel_names
    to_read = [channel for channel in channels if _needs_catch_up(channel, named, floor)]
    log_lifecycle("catchup_start", count=len(to_read))

    posts: list[Post] = []
    for channel in to_read:
        try:
            result = await _with_retries(runtime, runtime.client.posts_since, channel, floor)
        except MattermostError as error:
            if error.reason == "auth_rejected" or _is_passing(error):
                raise
            # Only this channel is affected, and repeating the catch-up would not change that.
            log_lifecycle("channel_skipped", reason=error.reason, status=error.status)
            truncated = True
            continue
        posts += result.posts
        truncated = truncated or result.truncated
    if truncated:
        log_lifecycle("catchup_truncated", count=len(posts))

    posts.sort(key=lambda post: post.create_at)
    for post in posts:
        await _handle_post(runtime, me, post, live=False)
        # Handling a post need not await anything once the sender is cached, so give the
        # heartbeat and the socket's keep-alive pings a turn after each one.
        await asyncio.sleep(0)
    log_lifecycle("catchup_done", count=len(posts))


async def _consume(runtime: Runtime, me: Me, socket: ClientConnection) -> None:
    while True:
        frame = await socket.recv()
        try:
            post = parse_websocket_message(frame)
        except MalformedEvent:
            log_lifecycle("frame_skipped", reason="protocol_error")
            continue
        if post is not None:
            await _handle_post(runtime, me, post, live=True)


async def _listen(runtime: Runtime, me: Me) -> None:
    """Keep a connection going until stopped. Only a rejected token ends this."""
    failures = 0
    while True:
        try:
            socket = await runtime.connect_events()
            try:
                log_lifecycle("connected")
                await _catch_up(runtime, me)
                failures = 0
                await _consume(runtime, me, socket)
            finally:
                await socket.close()
        except MattermostError as error:
            if error.reason == "auth_rejected":
                raise
            log_lifecycle(
                "disconnected", reason=error.reason, status=error.status, error=error.__cause__
            )
        except ConnectionClosed as error:
            log_lifecycle("disconnected", reason="unreachable", error=error)
        delay = backoff_delay(failures, runtime.jitter())
        failures += 1
        log_lifecycle("reconnect_wait", count=failures, delay_seconds=delay)
        await runtime.sleep(delay)


async def _start_and_listen(runtime: Runtime) -> None:
    identity = await runtime.client.get_me()
    server_now = identity.server_time_ms or int(runtime.clock() * 1000)
    # On the first run there is no history to read. Catch-up starts OVERLAP_MS before last-seen,
    # so this makes the first catch-up start from now.
    runtime.store.set_first_last_seen(server_now + OVERLAP_MS)
    log_lifecycle("startup")
    await _listen(runtime, identity.me)


def _beat(path: Path) -> None:
    try:
        path.touch()
    except OSError as error:
        raise StoreError(f"cannot write the heartbeat file {path}: {error.strerror}") from error


async def _housekeeping(runtime: Runtime) -> None:
    """Prove the process is alive, mark quiet bursts ready, and forget old posts and jobs hourly.

    A Mattermost outage does not stop the heartbeat, because restarting would not fix it.
    The logs show outages, and the heartbeat shows only that the process is stuck.
    """
    last_pruned: float | None = None
    while True:
        _beat(runtime.heartbeat_file)
        now = runtime.clock()
        for job in runtime.store.flip_ready(now):
            _log_job("job_ready", job)
        if last_pruned is None or now - last_pruned >= PRUNE_SECONDS:
            pruned = runtime.store.prune(now)
            if pruned.jobs:
                log_lifecycle("jobs_pruned", count=pruned.jobs)
            last_pruned = now
        await asyncio.sleep(HOUSEKEEPING_SECONDS)


def healthcheck(env: Mapping[str, str]) -> int:
    """Return 0 if the heartbeat file is fresh and 1 if not, for the container's healthcheck."""
    path = Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR) / HEARTBEAT_FILE
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return 1
    return 0 if age <= HEARTBEAT_MAX_AGE_SECONDS else 1


async def run(runtime: Runtime) -> None:
    """Run until cancelled, or raise the error that made running impossible."""
    tasks = {
        asyncio.create_task(_housekeeping(runtime)),
        asyncio.create_task(_start_and_listen(runtime)),
    }
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve(runtime: Runtime) -> None:
    """Run until SIGTERM or Ctrl+C, then stop cleanly."""
    main_task = asyncio.current_task()
    if main_task is None:
        raise RuntimeError("serve must run inside a task")
    loop = asyncio.get_running_loop()
    # The default handler would print the exception with its text.
    loop.set_exception_handler(
        lambda _, context: log_lifecycle("internal_error", error=context.get("exception"))
    )
    stopping = False

    def stop() -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        runtime.on_stop()
        log_lifecycle("shutdown", reason="signal")
        main_task.cancel()

    # Installed first: as PID 1 in a container, the process would otherwise ignore SIGTERM.
    signals = (signal.SIGTERM, signal.SIGINT)
    for number in signals:
        loop.add_signal_handler(number, stop)
    try:
        await run(runtime)
    except asyncio.CancelledError:
        if not stopping:
            raise
        log_lifecycle("stopped")
    finally:
        for number in signals:
            loop.remove_signal_handler(number)
        await runtime.client.aclose()

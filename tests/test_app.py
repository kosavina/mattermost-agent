"""The whole loop against fakes: real WebSockets on localhost and a fake REST transport."""

import asyncio
import io
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from helpers import captured_logs, make_post, posted_event, raw_post, squeezed
from websockets.asyncio.server import Server, ServerConnection, serve

from mattermost_agent import __main__ as entry
from mattermost_agent import app
from mattermost_agent.logs import configure_logging
from mattermost_agent.mattermost import MattermostClient, MattermostError, open_events
from mattermost_agent.rules import Bursts, parse_rules
from mattermost_agent.store import (
    FILE_NAME,
    JOB_RETENTION_SECONDS,
    RETENTION_MS,
    Store,
    StoreError,
    read_jobs,
)

TOKEN = "abcdefghijklmnopqrstuvwxyz"
SECRET_TEXT = "sentinel-private-message"
NOW = 2_000_000_000.0
NOW_MS = 2_000_000_000_000
# Later than where a first run sets last-seen, so that these posts count as new.
LATER = NOW_MS + 10 * 60 * 1000

BURSTS = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=20)
BURSTS_TEXT = (
    f"bursts: {{quiet_seconds: {BURSTS.quiet_seconds}, "
    f"max_wait_seconds: {BURSTS.max_wait_seconds}, max_posts: {BURSTS.max_posts}}}\n"
)
RULES = f"""
rules:
  - {{name: dm-from-anyone, dm: true, scenario: intake}}
  - {{name: analytics, channels: [analytics], scenario: review}}
  - {{name: named-sender, channels: [requests], from: [example.user], scenario: review}}
limits: {{jobs_per_hour: 20}}
{BURSTS_TEXT}"""

type Scenario = Callable[[], Awaitable[None]]


@dataclass
class Script:
    """What the fake server does on one connection, after its hello.

    It sends `frames`, runs `after`, sends `more`, then closes or waits for the client to.
    """

    frames: list[str] = field(default_factory=list)
    after: Scenario | None = None
    more: list[str] = field(default_factory=list)
    close: bool = False


class World:
    """A fake Mattermost server and the app wired to it."""

    def __init__(self, tmp_path: Path, stream: io.StringIO) -> None:
        self.tmp_path = tmp_path
        self.stream = stream
        self.store = Store(tmp_path / "state" / FILE_NAME)
        self.rules_text = RULES
        self.now = NOW
        self.channels: list[dict[str, Any]] = []
        self.posts: dict[str, list[dict[str, Any]]] = {}
        self.users = {"me-id": "example.me", "other-id": "example.user"}
        self.scripts: list[Script] = []
        self.requests: list[httpx2.Request] = []
        self.channel_list_status = 200
        self.channel_statuses: dict[str, int] = {}
        # Answers for one user, used in turn. The last one is repeated.
        self.user_statuses: dict[str, list[int]] = {}
        self.block_posts = False
        self.me_status = 200
        self.sleeps: list[float] = []
        self.connect_failures: list[MattermostError] = []
        self.stops = 0
        self.client = MattermostClient(
            "https://chat.example.com", TOKEN, transport=httpx2.MockTransport(self.rest)
        )

    async def rest(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v4")
        if path == "/users/me":
            body = {"id": "me-id", "username": "example.me"}
            return httpx2.Response(self.me_status, json=body)
        if path == "/users/me/channels":
            return httpx2.Response(self.channel_list_status, json=self.channels)
        if path.startswith("/users/"):
            user_id = path.rsplit("/", 1)[1]
            statuses = self.user_statuses.get(user_id, [200])
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            if status != 200:
                return httpx2.Response(status, json={})
            if user_id not in self.users:
                return httpx2.Response(404, json={})
            return httpx2.Response(200, json={"id": user_id, "username": self.users[user_id]})
        if self.block_posts:
            await asyncio.Event().wait()
        channel_id = path.split("/")[2]
        if channel_id in self.channel_statuses:
            return httpx2.Response(self.channel_statuses[channel_id], json={})
        posts = sorted(self.posts.get(channel_id, []), key=lambda p: p["create_at"], reverse=True)
        page, size = int(request.url.params["page"]), int(request.url.params["per_page"])
        chosen = posts[page * size : (page + 1) * size]
        body = {"order": [p["id"] for p in chosen], "posts": {p["id"]: p for p in chosen}}
        return httpx2.Response(200, json=body)

    async def websocket(self, connection: ServerConnection) -> None:
        script = self.scripts.pop(0) if self.scripts else Script()
        await connection.send(json.dumps({"event": "hello", "data": {}, "seq": 0}))
        for frame in script.frames:
            await connection.send(frame)
        if script.after is not None:
            await script.after()
        for frame in script.more:
            await connection.send(frame)
        if script.close:
            await connection.close()
        else:
            await connection.wait_closed()

    def runtime(self, address: str) -> app.Runtime:
        async def connect() -> Any:
            if self.connect_failures:
                raise self.connect_failures.pop(0)
            return await open_events(address, TOKEN)

        async def fake_sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            await asyncio.sleep(0)

        def stopped() -> None:
            self.stops += 1

        return app.Runtime(
            client=self.client,
            store=self.store,
            rules=parse_rules(self.rules_text),
            connect_events=connect,
            heartbeat_file=self.tmp_path / "state" / app.HEARTBEAT_FILE,
            clock=lambda: self.now,
            sleep=fake_sleep,
            jitter=lambda: 1.0,
            on_stop=stopped,
        )

    def go(self, scenario: Scenario) -> None:
        """Run the app while `scenario` runs, then stop it."""

        async def main() -> None:
            async with serve(self.websocket, "127.0.0.1", 0) as server:
                task = asyncio.create_task(app.run(self.runtime(address_of(server))))
                try:
                    await scenario()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await self.client.aclose()

        asyncio.run(main())

    def handled(self, post_id: str) -> bool:
        connection = sqlite3.connect(self.store.path)
        try:
            query = "SELECT 1 FROM handled WHERE post_id = ?"
            return connection.execute(query, (post_id,)).fetchone() is not None
        finally:
            connection.close()

    @property
    def log(self) -> str:
        return squeezed(self.stream.getvalue())

    def lines(self, event: str) -> list[str]:
        return [line for line in self.log.splitlines() if f" event={event} " in line]

    def post_paths(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.endswith("/posts")]


def address_of(server: Server) -> str:
    port = next(iter(server.sockets)).getsockname()[1]
    return f"ws://127.0.0.1:{port}/api/v4/websocket"


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    with captured_logs() as stream:
        yield World(tmp_path, stream)


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


def frame(
    post_id: str,
    create_at: int = LATER,
    *,
    channel_type: str = "D",
    channel_name: str = "me-id__other-id",
    **fields: Any,
) -> str:
    post = raw_post(id=post_id, create_at=create_at, **fields)
    return json.dumps(posted_event(post, channel_type=channel_type, channel_name=channel_name))


def channel(
    channel_id: str, *, name: str = "x__y", kind: str = "D", last_post_at: int = 0
) -> dict[str, Any]:
    return {"id": channel_id, "name": name, "type": kind, "last_post_at": last_post_at}


def test_a_live_post_is_logged_once(world: World) -> None:
    world.scripts = [Script([frame("a")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert len(world.lines("post")) == 1
    line = world.lines("post")[0]
    assert 'kind=dm channel=null sender="example.user" length=5' in line
    assert 'rule="dm-from-anyone" scenario="intake"' in line


def test_a_channel_post_logs_its_channel_name(world: World) -> None:
    world.scripts = [Script([frame("a", channel_type="O", channel_name="analytics")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'kind=channel channel="analytics"' in world.lines("post")[0]
    assert 'rule="analytics" scenario="review"' in world.lines("post")[0]


def test_an_unmatched_post_is_logged_without_a_rule(world: World) -> None:
    world.scripts = [Script([frame("a", channel_type="O", channel_name="random")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert "rule=null scenario=null" in world.lines("post")[0]


def test_the_message_text_never_reaches_the_log(world: World) -> None:
    world.scripts = [Script([frame("a", message=SECRET_TEXT)])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert f"length={len(SECRET_TEXT)}" in world.lines("post")[0]
    assert SECRET_TEXT not in world.log


def test_a_mention_is_recognised(world: World) -> None:
    event = posted_event(
        raw_post(id="a", create_at=LATER),
        channel_type="O",
        channel_name="random",
        mentions=json.dumps(["me-id"]),
    )
    world.scripts = [Script([json.dumps(event)])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'kind=mention channel="random"' in world.lines("post")[0]
    # Being mentioned is not a condition, and no rule names the channel.
    assert "rule=null" in world.lines("post")[0]


def test_my_own_dm_is_logged_with_who_it_went_to(world: World) -> None:
    world.scripts = [Script([frame("mine", user_id="me-id", message=SECRET_TEXT)])]
    world.go(lambda: until(lambda: world.handled("mine")))
    line = world.lines("post")[0]
    assert f'kind=dm channel="@example.user" sender="example.me" length={len(SECRET_TEXT)}' in line
    assert "rule=null scenario=null" in line
    assert SECRET_TEXT not in world.log


def test_my_own_channel_post_logs_the_channel(world: World) -> None:
    world.scripts = [Script([frame("a", user_id="me-id", channel_type="O", channel_name="dev")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'kind=channel channel="dev" sender="example.me"' in world.lines("post")[0]


@pytest.mark.parametrize(
    ("channel_type", "channel_name", "logged"),
    [
        ("D", "me-id__me-id", '"@example.me"'),
        ("D", "me-id__nobody", "null"),
        ("D", "unusual", "null"),
        ("G", "abc123", "null"),
    ],
)
def test_where_my_own_private_message_went(
    world: World, channel_type: str, channel_name: str, logged: str
) -> None:
    post = frame("a", user_id="me-id", channel_type=channel_type, channel_name=channel_name)
    world.scripts = [Script([post])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert f"channel={logged} " in world.lines("post")[0]


def test_my_own_post_matches_only_a_rule_that_names_me(world: World) -> None:
    world.rules_text = f"""
rules:
  - {{name: from-others, dm: true, scenario: one}}
  - {{name: from-me, dm: true, from: [example.me], scenario: two}}
limits: {{jobs_per_hour: 5}}
{BURSTS_TEXT}"""
    world.scripts = [Script([frame("a", user_id="me-id")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'rule="from-me" scenario="two"' in world.lines("post")[0]


def test_my_own_post_sent_twice_is_logged_once(world: World) -> None:
    mine = frame("a", user_id="me-id")
    world.scripts = [Script([mine, mine, frame("b", LATER + 1)])]
    world.go(lambda: until(lambda: world.handled("b")))
    assert len(world.lines("post")) == 2


def test_my_own_post_is_found_by_catch_up(world: World) -> None:
    direct = channel("dm-1", name="me-id__other-id")
    world.channels = [direct]
    mine = raw_post(id="mine", channel_id="dm-1", user_id="me-id", create_at=LATER + 20)

    async def lose_connection() -> None:
        await until(lambda: world.handled("first"))
        world.posts["dm-1"] = [mine]
        direct["last_post_at"] = mine["create_at"]

    world.scripts = [Script([frame("first", LATER + 10)], after=lose_connection, close=True)]
    world.go(lambda: until(lambda: world.handled("mine")))
    assert 'channel="@example.user" sender="example.me"' in world.lines("post")[1]


def test_three_quick_posts_make_one_job(world: World) -> None:
    posts = [frame(post_id, LATER + n, message=SECRET_TEXT) for n, post_id in enumerate("abc")]
    world.scripts = [Script(posts)]
    world.go(lambda: until(lambda: world.handled("c")))
    jobs = read_jobs(world.store.path)
    assert [(job.id, job.status, job.post_count) for job in jobs] == [(1, "collecting", 3)]
    assert len(world.lines("post")) == 3
    assert len(world.lines("job_opened")) == 1
    assert len(world.lines("job_joined")) == 2
    assert (
        'job=1 status=collecting scenario="intake" rule="dm-from-anyone" place="@example.user" '
        "posts=3" in world.lines("job_joined")[1]
    )
    assert world.lines("job_ready") == []
    assert SECRET_TEXT not in world.log


def test_a_quiet_burst_becomes_ready_and_the_next_post_opens_a_new_job(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "HOUSEKEEPING_SECONDS", 0.01)

    def status_of(job_id: int) -> str | None:
        found = [job for job in read_jobs(world.store.path) if job.id == job_id]
        return found[0].status if found else None

    async def wait_for_quiet() -> None:
        await until(lambda: world.handled("c"))
        world.now += BURSTS.quiet_seconds + 1
        await until(lambda: status_of(1) == "ready")

    posts = [frame(post_id, LATER + n, message=SECRET_TEXT) for n, post_id in enumerate("abc")]
    world.scripts = [Script(posts, after=wait_for_quiet, more=[frame("d", LATER + 3)])]
    world.go(lambda: until(lambda: world.handled("d")))
    jobs = read_jobs(world.store.path)
    assert [(job.id, job.status, job.post_count) for job in jobs] == [
        (2, "collecting", 1),
        (1, "ready", 3),
    ]
    assert len(world.lines("job_ready")) == 1
    assert "job=1 status=ready" in world.lines("job_ready")[0]
    assert "job=2 status=collecting" in world.lines("job_opened")[1]
    assert SECRET_TEXT not in world.log


def test_a_system_post_is_skipped(world: World) -> None:
    world.scripts = [Script([frame("s", type="system_join_channel"), frame("b", LATER + 1)])]
    world.go(lambda: until(lambda: world.handled("b")))
    assert not world.handled("s")
    assert len(world.lines("post")) == 1


def test_a_post_sent_twice_is_logged_once(world: World) -> None:
    world.scripts = [Script([frame("a"), frame("a"), frame("b", LATER + 1)])]
    world.go(lambda: until(lambda: world.handled("b")))
    assert len(world.lines("post")) == 2


def test_the_sender_is_the_real_user_and_not_the_name_in_the_event(world: World) -> None:
    world.users["webhook-owner"] = "actual.owner"
    event = posted_event(
        raw_post(id="a", create_at=LATER, user_id="webhook-owner"),
        channel_type="O",
        channel_name="requests",
        sender_name="@example.user",
    )
    world.scripts = [Script([json.dumps(event)])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'sender="actual.owner"' in world.lines("post")[0]
    assert "rule=null" in world.lines("post")[0]


def test_the_first_matching_rule_wins_in_the_loop(world: World) -> None:
    world.rules_text = f"""
rules:
  - {{name: first, dm: true, scenario: one}}
  - {{name: second, dm: true, scenario: two}}
limits: {{jobs_per_hour: 5}}
{BURSTS_TEXT}"""
    world.scripts = [Script([frame("a")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'rule="first" scenario="one"' in world.lines("post")[0]


def test_a_match_over_the_cap_is_logged_as_cap_hit(world: World) -> None:
    world.rules_text = f"""
rules:
  - {{name: dm, dm: true, scenario: intake}}
limits: {{jobs_per_hour: 1}}
{BURSTS_TEXT}"""
    # A second post in the same DM would join the first job; another DM needs a job of its own.
    other_dm = frame("b", LATER + 1, channel_name="me-id__third-id", channel_id="channel-2")
    world.scripts = [Script([frame("a"), other_dm])]
    world.go(lambda: until(lambda: world.handled("b")))
    assert len(world.lines("post")) == 1
    assert len(world.lines("cap_hit")) == 1
    assert 'rule="dm" scenario="intake"' in world.lines("cap_hit")[0]


def test_malformed_frames_are_skipped_without_quoting_them(world: World) -> None:
    world.scripts = [Script([SECRET_TEXT, frame("a")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert "reason=protocol_error" in world.lines("frame_skipped")[0]
    assert SECRET_TEXT not in world.log


def test_reconnect_catches_up_without_duplicates(world: World) -> None:
    direct = channel("dm-1")
    world.channels = [direct]
    missed = raw_post(id="missed", channel_id="dm-1", create_at=LATER + 20)

    async def lose_connection() -> None:
        await until(lambda: world.handled("first"))
        world.posts["dm-1"] = [missed]
        direct["last_post_at"] = missed["create_at"]

    world.scripts = [
        Script([frame("first", LATER + 10)], after=lose_connection, close=True),
        # The server also delivers the missed post live, as can happen around a reconnect.
        Script([frame("missed", LATER + 20), frame("last", LATER + 30)]),
    ]
    world.go(lambda: until(lambda: world.handled("last")))

    assert len(world.lines("post")) == 3
    assert len(world.sleeps) == 1
    assert len(world.lines("connected")) == 2
    assert set(world.post_paths()) == {"/api/v4/channels/dm-1/posts"}
    assert world.store.last_seen() == LATER + 30


def test_a_post_missed_while_disconnected_is_found_by_catch_up_alone(world: World) -> None:
    direct = channel("dm-1")
    world.channels = [direct]
    missed = raw_post(id="missed", channel_id="dm-1", create_at=LATER + 20, message="hi")

    async def lose_connection() -> None:
        await until(lambda: world.handled("first"))
        world.posts["dm-1"] = [missed]
        direct["last_post_at"] = missed["create_at"]

    world.scripts = [Script([frame("first", LATER + 10)], after=lose_connection, close=True)]
    world.go(lambda: until(lambda: world.handled("missed")))
    assert len(world.lines("post")) == 2
    assert 'kind=dm channel=null sender="example.user" length=2' in world.lines("post")[1]
    # Only live posts move last-seen: a crash now would repeat the catch-up, not lose it.
    assert world.store.last_seen() == LATER + 10


def test_catch_up_runs_oldest_first_across_channels(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.channels = [
        channel("dm-1", last_post_at=NOW_MS + 30),
        channel("dm-2", last_post_at=NOW_MS + 20),
    ]
    world.posts = {
        "dm-1": [raw_post(id="c", channel_id="dm-1", create_at=NOW_MS + 30, message="123")],
        "dm-2": [raw_post(id="b", channel_id="dm-2", create_at=NOW_MS + 20, message="12")],
    }
    world.go(lambda: until(lambda: "event=catchup_done" in world.log))
    lengths = [line.split("length=")[1].split()[0] for line in world.lines("post")]
    assert lengths == ["2", "3"]


def test_catch_up_reads_only_dms_and_named_channels_with_new_posts(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    active = NOW_MS + 5
    world.channels = [
        channel("dm", kind="D", last_post_at=active),
        channel("group", kind="G", last_post_at=active),
        channel("quiet-dm", kind="D", last_post_at=NOW_MS - 10 * 60 * 1000),
        channel("named", name="analytics", kind="O", last_post_at=active),
        channel("named-quiet", name="analytics", kind="P", last_post_at=1),
        channel("other", name="random", kind="O", last_post_at=active),
    ]
    world.go(lambda: until(lambda: "event=catchup_done" in world.log))
    assert world.post_paths() == [
        "/api/v4/channels/dm/posts",
        "/api/v4/channels/group/posts",
        "/api/v4/channels/named/posts",
    ]
    assert "count=3" in world.lines("catchup_start")[0]


def test_catch_up_lets_other_tasks_run_between_posts(world: World) -> None:
    """A long catch-up must not starve the heartbeat and the socket's keep-alive pings."""
    world.store.set_first_last_seen(NOW_MS)
    world.channels = [channel("dm", last_post_at=NOW_MS + 5)]
    world.posts = {
        "dm": [raw_post(id=f"p{n}", channel_id="dm", create_at=NOW_MS + 5 + n) for n in range(200)]
    }
    turns = 0

    async def scenario() -> None:
        nonlocal turns

        async def count_turns() -> None:
            nonlocal turns
            while True:
                if world.lines("catchup_start") and not world.lines("catchup_done"):
                    turns += 1
                await asyncio.sleep(0)

        counter = asyncio.create_task(count_turns())
        try:
            await until(lambda: "event=catchup_done" in world.log)
        finally:
            counter.cancel()

    world.go(scenario)
    assert turns >= 200


def test_the_first_run_reads_no_history(world: World) -> None:
    world.channels = [channel("dm", last_post_at=NOW_MS)]
    world.posts = {"dm": [raw_post(id="old", channel_id="dm", create_at=NOW_MS - 1000)]}
    world.go(lambda: until(lambda: "event=catchup_done" in world.log))
    assert world.post_paths() == []
    assert not world.handled("old")


def test_catch_up_goes_back_at_most_the_retention(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS - 2 * RETENTION_MS)
    world.channels = [channel("dm", last_post_at=NOW_MS)]
    too_old = NOW_MS - RETENTION_MS - 1000
    in_range = NOW_MS - RETENTION_MS + 1000
    world.posts = {
        "dm": [
            raw_post(id="too-old", channel_id="dm", create_at=too_old),
            raw_post(id="in-range", channel_id="dm", create_at=in_range),
        ]
    }
    world.go(lambda: until(lambda: "event=catchup_done" in world.log))
    assert world.handled("in-range")
    assert not world.handled("too-old")
    assert len(world.lines("catchup_truncated")) == 1


def test_a_failed_catch_up_is_retried_and_moves_nothing(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.channels = [channel("dm", last_post_at=NOW_MS + 5)]
    world.posts = {"dm": [raw_post(id="a", channel_id="dm", create_at=NOW_MS + 5)]}
    world.channel_list_status = 500

    async def scenario() -> None:
        await until(lambda: len(world.sleeps) >= 2)
        assert world.store.last_seen() == NOW_MS
        world.channel_list_status = 200
        await until(lambda: world.handled("a"))

    world.go(scenario)
    assert "reason=server_error" in world.lines("disconnected")[0]
    assert "status=500" in world.lines("disconnected")[0]


def test_a_live_post_survives_a_passing_lookup_failure(world: World) -> None:
    world.user_statuses = {"other-id": [500, 200]}
    world.scripts = [Script([frame("a")])]
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'sender="example.user"' in world.lines("post")[0]
    assert world.sleeps == [1.0]
    assert world.lines("disconnected") == []


def test_a_lookup_that_keeps_failing_loses_no_live_post(world: World) -> None:
    world.user_statuses = {"other-id": [500]}
    world.scripts = [
        Script(
            [
                frame("a", channel_type="O", channel_name="random"),
                frame("b", LATER + 1, channel_type="O", channel_name="random"),
            ]
        )
    ]
    world.go(lambda: until(lambda: world.handled("b")))
    assert world.handled("a")
    assert [line.count("sender=null") for line in world.lines("post")] == [1, 1]
    assert "status=500" in world.lines("lookup_failed")[0]
    assert world.lines("disconnected") == []
    assert world.store.last_seen() == LATER + 1


def test_a_hidden_sender_does_not_stop_catch_up_or_live_posts(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.user_statuses = {"hidden-id": [403]}
    world.channels = [channel("dm", last_post_at=NOW_MS + 5)]
    world.posts = {
        "dm": [raw_post(id="old", channel_id="dm", user_id="hidden-id", create_at=NOW_MS + 5)]
    }
    world.scripts = [Script([frame("live")])]
    world.go(lambda: until(lambda: world.handled("live")))
    assert world.handled("old")
    assert "status=403" in world.lines("lookup_failed")[0]
    # Trying again could not help, so nothing waited.
    assert world.sleeps == []
    assert world.lines("disconnected") == []


def test_a_passing_lookup_failure_repeats_the_catch_up_and_keeps_the_sender(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.user_statuses = {"other-id": [500, 500, 500, 200]}
    world.channels = [channel("dm", last_post_at=NOW_MS + 5)]
    world.posts = {"dm": [raw_post(id="a", channel_id="dm", create_at=NOW_MS + 5)]}
    world.go(lambda: until(lambda: world.handled("a")))
    assert 'sender="example.user"' in world.lines("post")[0]
    assert world.sleeps == [1.0, 2.0, 1.0]
    assert world.lines("lookup_failed") == []
    assert "status=500" in world.lines("disconnected")[0]


def test_a_refused_channel_is_skipped_and_the_rest_are_read(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.channel_statuses = {"refused": 403}
    world.channels = [
        channel("refused", last_post_at=NOW_MS + 5),
        channel("dm", last_post_at=NOW_MS + 5),
    ]
    world.posts = {"dm": [raw_post(id="a", channel_id="dm", create_at=NOW_MS + 5)]}
    world.scripts = [Script([frame("live")])]
    world.go(lambda: until(lambda: world.handled("live")))
    assert world.handled("a")
    assert "status=403" in world.lines("channel_skipped")[0]
    assert len(world.lines("catchup_truncated")) == 1
    assert world.lines("disconnected") == []


def test_a_passing_channel_failure_repeats_the_catch_up(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.channel_statuses = {"dm": 503}
    world.channels = [channel("dm", last_post_at=NOW_MS + 5)]
    world.posts = {"dm": [raw_post(id="a", channel_id="dm", create_at=NOW_MS + 5)]}

    async def scenario() -> None:
        await until(lambda: len(world.lines("disconnected")) >= 1)
        world.channel_statuses = {}
        await until(lambda: world.handled("a"))

    world.go(scenario)
    assert world.lines("channel_skipped") == []
    assert world.lines("catchup_truncated") == []


def test_a_channel_of_an_unknown_type_does_not_stop_catch_up(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.channels = [
        channel("new-kind", kind="X", last_post_at=NOW_MS + 5),
        channel("dm", last_post_at=NOW_MS + 5),
    ]
    world.posts = {"dm": [raw_post(id="a", channel_id="dm", create_at=NOW_MS + 5)]}
    world.go(lambda: until(lambda: world.handled("a")))
    assert set(world.post_paths()) == {"/api/v4/channels/dm/posts"}
    assert world.lines("disconnected") == []


def test_a_token_revoked_during_a_lookup_stops_the_app(world: World) -> None:
    world.user_statuses = {"other-id": [401]}
    world.scripts = [Script([frame("a")])]

    async def main() -> None:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            with pytest.raises(MattermostError) as error:
                await app.run(world.runtime(address_of(server)))
            assert error.value.status == 401
        await world.client.aclose()

    asyncio.run(main())
    assert not world.handled("a")


def test_failures_back_off_and_success_resets_the_delay(world: World) -> None:
    world.connect_failures = [MattermostError("unreachable") for _ in range(3)]
    closed_once = Script(close=True)
    stays_open = Script([frame("a")])
    world.scripts = [closed_once, stays_open]

    async def scenario() -> None:
        await until(lambda: world.handled("a"))

    world.go(scenario)
    assert world.sleeps == [1, 2, 4, 1]


def test_a_rejected_token_at_startup_stops_the_app(world: World) -> None:
    world.me_status = 401

    async def main() -> None:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            with pytest.raises(MattermostError) as error:
                await app.run(world.runtime(address_of(server)))
            assert error.value.reason == "auth_rejected"
        await world.client.aclose()

    asyncio.run(main())


def test_a_token_revoked_later_stops_the_app(world: World) -> None:
    world.connect_failures = [MattermostError("auth_rejected", 401)]

    async def main() -> None:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            with pytest.raises(MattermostError) as error:
                await app.run(world.runtime(address_of(server)))
            assert error.value.status == 401
        await world.client.aclose()

    asyncio.run(main())


def test_a_server_error_at_startup_stops_the_app(world: World) -> None:
    world.me_status = 500

    async def main() -> None:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            with pytest.raises(MattermostError) as error:
                await app.run(world.runtime(address_of(server)))
            assert error.value.status == 500
        await world.client.aclose()

    asyncio.run(main())


def test_nothing_secret_reaches_the_log_even_at_debug(world: World) -> None:
    configure_logging("DEBUG", world.stream)
    direct = channel("dm-1")
    world.channels = [direct]
    missed = raw_post(id="missed", channel_id="dm-1", create_at=LATER + 20, message=SECRET_TEXT)

    async def lose_connection() -> None:
        await until(lambda: world.handled("first"))
        world.posts["dm-1"] = [missed]
        direct["last_post_at"] = missed["create_at"]

    world.scripts = [
        Script(
            [SECRET_TEXT, frame("first", LATER + 10, message=SECRET_TEXT)],
            after=lose_connection,
            close=True,
        ),
        Script([frame("last", LATER + 30, message=SECRET_TEXT)]),
    ]
    world.go(lambda: until(lambda: world.handled("last")))
    assert len(world.lines("post")) == 3
    assert TOKEN not in world.log
    assert "Authorization" not in world.log
    assert SECRET_TEXT not in world.log


def test_the_heartbeat_file_is_refreshed(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "HOUSEKEEPING_SECONDS", 0.01)
    path = world.tmp_path / "state" / app.HEARTBEAT_FILE

    async def scenario() -> None:
        await until(path.exists)
        os.utime(path, (1, 1))
        await until(lambda: path.stat().st_mtime > 1)

    world.go(scenario)


def test_the_heartbeat_continues_during_an_outage(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "HOUSEKEEPING_SECONDS", 0.01)
    world.connect_failures = [MattermostError("unreachable") for _ in range(10_000)]
    path = world.tmp_path / "state" / app.HEARTBEAT_FILE

    async def scenario() -> None:
        await until(lambda: len(world.sleeps) >= 3)
        await until(path.exists)
        os.utime(path, (1, 1))
        await until(lambda: path.stat().st_mtime > 1)

    world.go(scenario)


def test_a_heartbeat_that_cannot_be_written_stops_the_app(world: World) -> None:
    async def main() -> None:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            runtime = world.runtime(address_of(server))
            runtime.heartbeat_file = world.tmp_path / "missing-folder" / "heartbeat"
            with pytest.raises(StoreError, match="heartbeat"):
                await app.run(runtime)
        await world.client.aclose()

    asyncio.run(main())


def test_old_posts_are_pruned_at_startup(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    old = NOW_MS - RETENTION_MS - 1
    ancient = make_post(id="ancient", create_at=old)
    world.store.claim(
        ancient, kind="dm", rule=None, place=None, live=False, now=NOW - 7200, cap=20, bursts=BURSTS
    )
    world.go(lambda: until(lambda: not world.handled("ancient")))


def test_old_jobs_are_pruned_at_startup(world: World) -> None:
    old = make_post(id="old", create_at=NOW_MS - 1000, message=SECRET_TEXT)
    rule = parse_rules(RULES).rules[1]
    opened = NOW - JOB_RETENTION_SECONDS - 1
    world.store.claim(
        old, kind="channel", rule=rule, place=None, live=False, now=opened, cap=20, bursts=BURSTS
    )
    world.go(lambda: until(lambda: read_jobs(world.store.path) == []))
    assert "count=1" in world.lines("jobs_pruned")[0]
    assert SECRET_TEXT not in world.log


def test_healthcheck_passes_on_a_fresh_file(tmp_path: Path) -> None:
    (tmp_path / app.HEARTBEAT_FILE).touch()
    assert app.healthcheck({"STATE_DIR": str(tmp_path)}) == 0


def test_healthcheck_fails_on_a_stale_file(tmp_path: Path) -> None:
    path = tmp_path / app.HEARTBEAT_FILE
    path.touch()
    old = time.time() - app.HEARTBEAT_MAX_AGE_SECONDS - 5
    os.utime(path, (old, old))
    assert app.healthcheck({"STATE_DIR": str(tmp_path)}) == 1


def test_healthcheck_fails_without_a_file(tmp_path: Path) -> None:
    assert app.healthcheck({"STATE_DIR": str(tmp_path)}) == 1


@pytest.mark.parametrize(
    ("failures", "jitter", "expected"),
    [
        (0, 1.0, 1),
        (0, 0.0, 0.5),
        (1, 1.0, 2),
        (2, 1.0, 4),
        (5, 1.0, 32),
        (6, 1.0, 60),
        (7, 1.0, 60),
        (1000, 1.0, 60),
        (1000, 0.0, 30),
    ],
)
def test_backoff_delay(failures: int, jitter: float, expected: float) -> None:
    assert app.backoff_delay(failures, jitter) == expected


def run_until_signal(world: World, ready: Callable[[], bool], sig: signal.Signals) -> float:
    """Start serve(), send a real signal once `ready`, and return how long stopping took."""

    async def main() -> float:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            task = asyncio.create_task(app.serve(world.runtime(address_of(server))))
            await until(ready)
            started = time.monotonic()
            os.kill(os.getpid(), sig)
            await asyncio.wait_for(task, 5)
            return time.monotonic() - started

    return asyncio.run(main())


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_app_while_listening(world: World, sig: signal.Signals) -> None:
    taken = run_until_signal(world, lambda: "event=catchup_done" in world.log, sig)
    assert taken < 5
    assert world.stops == 1
    assert "reason=signal" in world.lines("shutdown")[0]
    assert len(world.lines("stopped")) == 1


def test_a_signal_stops_the_app_during_backoff(world: World) -> None:
    world.connect_failures = [MattermostError("unreachable")]

    def ready() -> bool:
        return bool(world.sleeps)

    async def main() -> float:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            runtime = world.runtime(address_of(server))
            hang = asyncio.Event()

            async def endless_sleep(seconds: float) -> None:
                world.sleeps.append(seconds)
                await hang.wait()

            runtime.sleep = endless_sleep
            task = asyncio.create_task(app.serve(runtime))
            await until(ready)
            started = time.monotonic()
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, 5)
            return time.monotonic() - started

    assert asyncio.run(main()) < 5
    assert len(world.lines("stopped")) == 1


def test_a_signal_stops_the_app_during_catch_up(world: World) -> None:
    world.store.set_first_last_seen(NOW_MS)
    world.channels = [channel("dm", last_post_at=NOW_MS + 5)]
    world.block_posts = True
    taken = run_until_signal(world, lambda: "event=catchup_start" in world.log, signal.SIGTERM)
    assert taken < 5
    assert len(world.lines("stopped")) == 1


def test_a_second_signal_is_harmless(world: World) -> None:
    async def main() -> None:
        async with serve(world.websocket, "127.0.0.1", 0) as server:
            task = asyncio.create_task(app.serve(world.runtime(address_of(server))))
            await until(lambda: "event=catchup_done" in world.log)
            os.kill(os.getpid(), signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, 5)

    asyncio.run(main())
    assert world.stops == 1


def test_the_watchdog_forces_an_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fired = threading.Event()
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        fired.set()

    monkeypatch.setattr(app.os, "_exit", fake_exit)
    app.start_watchdog(0.01)
    assert fired.wait(2)
    assert codes == [1]


@pytest.fixture
def launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[io.StringIO]:
    """Prepare a valid environment for main(), with no server to talk to."""
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["mattermost-agent"])
    monkeypatch.delenv("MM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MM_URL", "https://chat.example.com")
    monkeypatch.setenv("MM_TOKEN", TOKEN)
    monkeypatch.setenv("RULES_FILE", str(rules))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    with captured_logs() as stream:
        yield stream


def stop_with(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def fake_serve(_: app.Runtime) -> None:
        raise error

    monkeypatch.setattr(entry, "serve", fake_serve)
    # main() configures logging itself, and that would replace the captured stream.
    monkeypatch.setattr(entry, "configure_logging", lambda _: None)


def test_main_explains_a_bad_setting(launch: io.StringIO, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MM_URL")
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == "mattermost-agent: set MM_URL"


def test_main_explains_bad_rules(
    launch: io.StringIO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RULES_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert "cannot read the rules file" in str(stopped.value.code)


def test_main_explains_an_unusable_state_folder(
    launch: io.StringIO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "a-file"
    blocked.touch()
    monkeypatch.setenv("STATE_DIR", str(blocked))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert "cannot use the database" in str(stopped.value.code)


def test_main_logs_and_explains_a_failure_to_connect(
    launch: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop_with(monkeypatch, MattermostError("unreachable"))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == "mattermost-agent: cannot reach the Mattermost server"
    assert "event=fatal reason=unreachable" in squeezed(launch.getvalue())


def test_main_logs_a_store_failure(launch: io.StringIO, monkeypatch: pytest.MonkeyPatch) -> None:
    stop_with(monkeypatch, StoreError("cannot use the database"))
    with pytest.raises(SystemExit):
        entry.main()
    assert "event=fatal reason=store_error" in squeezed(launch.getvalue())


def test_main_shows_only_the_class_of_an_unexpected_error(
    launch: io.StringIO, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stop_with(monkeypatch, ValueError(SECRET_TEXT))
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == "mattermost-agent: stopped by ValueError"
    assert stopped.value.__suppress_context__
    assert "event=fatal reason=null error=ValueError" in squeezed(launch.getvalue())
    captured = capsys.readouterr()
    assert SECRET_TEXT not in launch.getvalue() + captured.out + captured.err


def test_main_refuses_unknown_arguments(
    launch: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["mattermost-agent", "surprise"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert "usage" in str(stopped.value.code)


def test_main_healthcheck_needs_no_token(
    launch: io.StringIO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MM_TOKEN")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    (tmp_path / app.HEARTBEAT_FILE).touch()
    monkeypatch.setattr(sys, "argv", ["mattermost-agent", "healthcheck"])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 0

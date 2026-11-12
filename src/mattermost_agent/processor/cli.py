"""The terminal side of the processor: drafts, approve, reject, memory.

Like the jobs command, these print text to the terminal and never to the log, with control
characters escaped first. `drafts` shows every reply that still has its text: the local drafts
that wait for approve or reject, and the replies in the outbox that are not delivered yet or
never will be (refused, failed or expired). approve and reject work on a local draft only, and
resume the job's graph themselves: the job is taken with a compare-and-set, the graph continues
past its pause, and the job is done. A local draft is never sent.
"""

import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from langgraph.types import Command

from mattermost_agent.jobs import NO_PLACE, age, safe_text
from mattermost_agent.processor import memory as memory_files
from mattermost_agent.processor.checkpoints import forget_thread, open_checkpointer
from mattermost_agent.processor.folders import CHANNEL_ID
from mattermost_agent.processor.graph import ProcessorRuntime, build_graph, thread
from mattermost_agent.processor.scenarios import ScenariosError, load_scenarios
from mattermost_agent.processor.settings import load_processor_settings
from mattermost_agent.rules import RulesError, load_rules
from mattermost_agent.settings import DEFAULT_STATE_DIR, SettingsError
from mattermost_agent.store import (
    FILE_NAME,
    Draft,
    Reply,
    Store,
    StoreError,
    read_drafts,
    read_undelivered,
)


def format_drafts(drafts: list[Draft], now: float, replies: Sequence[Reply] = ()) -> str:
    """Return each reply that still has its text: a heading line, then the text. No log involved.

    `drafts` wait for approve or reject. `replies` are in the outbox: on their way to
    Mattermost, or kept because they could not go there.
    """
    if not drafts and not replies:
        return "no drafts waiting\n"
    parts = []
    for draft in drafts:
        job = draft.job
        place = NO_PLACE if job.place is None else safe_text(job.place)
        since = datetime.fromtimestamp(job.finished_at or job.opened_at).strftime("%Y-%m-%d %H:%M")
        parts += [
            f"--- job {job.id}  place={place}  backend={job.backend or NO_PLACE}  "
            f"waiting={age(now - job.opened_at)}  opened={since}",
            safe_text(draft.text),
            "",
        ]
    for reply in replies:
        row = reply.row
        place = NO_PLACE if row.place is None else safe_text(row.place)
        parts += [
            f"--- job {row.job_id}  place={place}  reply={row.mode}  state={row.state}  "
            f"outcome={row.outcome or NO_PLACE}  reason={row.reason or NO_PLACE}  "
            f"waiting={age(now - row.created_at)}",
            safe_text(reply.text),
            "",
        ]
    return "\n".join(parts) + "\n"


def show_drafts(env: Mapping[str, str]) -> int:
    path = Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR) / FILE_NAME
    try:
        text = format_drafts(read_drafts(path), time.time(), read_undelivered(path))
    except StoreError as error:
        raise SystemExit(f"mattermost-agent: {error}") from None
    sys.stdout.write(text)
    return 0


def _runtime(env: Mapping[str, str]) -> tuple[ProcessorRuntime, Store]:
    settings = load_processor_settings(env)
    os.umask(0o077)
    store = Store(settings.state_dir / FILE_NAME)
    runtime = ProcessorRuntime(
        store=store,
        checkpointer=open_checkpointer(settings.state_dir),
        rules=load_rules(settings.rules_file),
        scenarios=load_scenarios(settings.scenarios_file),
        roots=settings.roots,
        backends=settings.backends(),
        default_backend=settings.backend,
        timeout_s=settings.timeout_s,
    )
    return runtime, store


def decide_draft(env: Mapping[str, str], job_id: int, *, approved: bool) -> int:
    """Approve or reject a waiting draft: resume its graph and finish the job."""
    try:
        runtime, store = _runtime(env)
        draft = store.take_waiting(job_id, time.time())
        if draft is None:
            raise SystemExit(f"mattermost-agent: job {job_id} is not waiting for approval")
        graph = build_graph(runtime)
        try:
            # Resuming a thread without a checkpoint would start the graph afresh, and so run the
            # agent from this command. Only a paused thread may be continued.
            if not graph.get_state(thread(job_id)).tasks:
                raise LookupError
            graph.invoke(Command(resume=approved), thread(job_id))
        except Exception:
            # Put the job back as it was, so that the next try or the expiry can handle it.
            job = draft.job
            store.set_waiting(
                job_id,
                backend=job.backend or "",
                decision=job.decision or "",
                draft=draft.text,
                now=time.time(),
            )
            raise SystemExit(f"mattermost-agent: job {job_id} could not be resumed") from None
        store.set_done(job_id, now=time.time(), approval="approved" if approved else "rejected")
        forget_thread(runtime.checkpointer, job_id)
    except (SettingsError, RulesError, ScenariosError, StoreError) as error:
        raise SystemExit(f"mattermost-agent: {error}") from None
    verb = "approved (a local reply is never sent)" if approved else "rejected"
    sys.stdout.write(f"job {job_id} {verb}\n")
    return 0


def _conversation(env: Mapping[str, str], channel_id: str) -> Path:
    if CHANNEL_ID.fullmatch(channel_id) is None:
        raise SystemExit("mattermost-agent: a channel id is 26 lower-case letters and digits")
    try:
        settings = load_processor_settings(env)
    except SettingsError as error:
        raise SystemExit(f"mattermost-agent: {error}") from None
    return settings.roots.conversations / channel_id


def show_memory(env: Mapping[str, str], channel_id: str) -> int:
    folder = _conversation(env, channel_id)
    memory = memory_files.read_memory(folder) if folder.is_dir() else None
    if memory is None:
        sys.stdout.write("no memory for this conversation\n")
        return 0
    versions = memory_files.memory_versions(folder)
    sys.stdout.write(f"--- memory  earlier_versions={versions}\n{safe_text(memory)}\n")
    return 0


def forget_memory(env: Mapping[str, str], channel_id: str) -> int:
    folder = _conversation(env, channel_id)
    if folder.is_symlink() or not folder.is_dir():
        sys.stdout.write("no memory for this conversation\n")
        return 0
    try:
        count = memory_files.forget_memory(folder)
    except OSError as error:
        raise SystemExit(f"mattermost-agent: cannot forget: {type(error).__name__}") from None
    sys.stdout.write(f"forgotten: {count} file(s) overwritten and removed\n")
    return 0

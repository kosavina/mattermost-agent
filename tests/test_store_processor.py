"""The processor's side of the store: schema v3, the hand-over, the finishing writes, expiry."""

import sqlite3
from pathlib import Path

import pytest
from helpers import make_post

from mattermost_agent.rules import Bursts, Rule
from mattermost_agent.store import (
    DRAFT_RETENTION_SECONDS,
    FILE_NAME,
    SCHEMA_VERSION,
    Draft,
    Job,
    Store,
    StoreError,
    read_drafts,
    read_job,
    read_jobs,
)

NOW = 1_000_000.0
BURSTS = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=1)
INTAKE = Rule("dm", "intake", True, frozenset(), frozenset())
CHANNEL = "k3d7w1q9m2n4p6r8t0v2x4z6a8"
CANARY = "zebraquartz"

# The schema of version 2, as the listener wrote it before this milestone.
V2_SCHEMA = (
    "CREATE TABLE handled (post_id TEXT PRIMARY KEY, create_at INTEGER NOT NULL)",
    "CREATE TABLE last_seen (id INTEGER PRIMARY KEY CHECK (id = 1), create_at INTEGER NOT NULL)",
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        conversation_key TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        place TEXT,
        rule TEXT NOT NULL,
        scenario TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'collecting', 'ready', 'running', 'waiting_approval', 'done', 'failed'
        )),
        opened_at REAL NOT NULL,
        ready_at REAL NOT NULL,
        deadline_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        post_count INTEGER NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX jobs_one_collecting ON jobs (conversation_key) "
    "WHERE status = 'collecting'",
    "CREATE INDEX jobs_status_ready_at ON jobs (status, ready_at)",
    "CREATE INDEX jobs_opened_at ON jobs (opened_at)",
    """
    CREATE TABLE job_posts (
        post_id TEXT PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        create_at INTEGER NOT NULL,
        kind TEXT NOT NULL,
        message TEXT NOT NULL
    )
    """,
    "CREATE INDEX job_posts_job_id ON job_posts (job_id, create_at)",
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state" / FILE_NAME


@pytest.fixture
def store(path: Path) -> Store:
    return Store(path)


def ready_job(store: Store, post_id: str = "a", now: float = NOW, message: str = "hello") -> Job:
    """Open a job that is ready at once (max_posts 1)."""
    post = make_post(id=post_id, channel_id=CHANNEL, message=message)
    outcome = store.claim(
        post,
        kind="dm",
        rule=INTAKE,
        place="@ivan",
        sender="ivan",
        live=True,
        now=now,
        cap=10,
        bursts=BURSTS,
    )
    assert outcome.job is not None and outcome.job.status == "ready"
    return outcome.job


def query(path: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def columns(path: Path) -> list[str]:
    return [str(row[1]) for row in query(path, "PRAGMA table_info(jobs)")]


def test_a_new_file_is_version_3(store: Store, path: Path) -> None:
    assert query(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert columns(path)[-7:] == [
        "sender",
        "backend",
        "decision",
        "draft",
        "fail_reason",
        "approval",
        "finished_at",
    ]


def test_a_version_2_file_is_migrated_with_its_rows(path: Path) -> None:
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    for statement in V2_SCHEMA:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO jobs (conversation_key, channel_id, root_id, sender_id, place, rule, "
        "scenario, status, opened_at, ready_at, deadline_at, updated_at, post_count) "
        "VALUES ('k', ?, '', 'u', 'analytics', 'r', 's', 'ready', 1, 2, 3, 4, 1)",
        (CHANNEL,),
    )
    connection.execute("INSERT INTO job_posts VALUES ('p', 1, 1000, 'channel', ?)", (CANARY,))
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    store = Store(path)
    assert query(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    fresh = Store(path.parent / "fresh.sqlite3")
    assert columns(path) == columns(fresh.path)
    job = read_jobs(path)[0]
    assert (job.id, job.status, job.channel_id, job.sender, job.backend) == (
        1,
        "ready",
        CHANNEL,
        None,
        None,
    )
    assert store.posts(1)[0].message == CANARY
    assert store.take_ready(NOW) is not None


def test_a_newer_file_is_refused(path: Path) -> None:
    Store(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(StoreError, match="unknown schema version 99"):
        Store(path)
    with pytest.raises(StoreError, match="schema version 99"):
        read_jobs(path)


def test_claim_records_the_sender(store: Store) -> None:
    job = ready_job(store)
    assert (job.channel_id, job.sender) == (CHANNEL, "ivan")
    found = read_job(store.path, job.id)
    assert found is not None and found[0].sender == "ivan"


def test_take_ready_is_oldest_first_and_once(store: Store) -> None:
    first = ready_job(store, "a", now=NOW)
    second = ready_job(store, "b", now=NOW + 1)
    taken = store.take_ready(NOW + 10)
    assert taken is not None and taken.id == first.id and taken.status == "running"
    assert store.jobs_in("running") == [taken]
    other = Store(store.path).take_ready(NOW + 11)
    assert other is not None and other.id == second.id
    assert store.take_ready(NOW + 12) is None


def test_take_ready_skips_collecting_jobs(store: Store) -> None:
    post = make_post(id="c", channel_id=CHANNEL)
    store.claim(
        post,
        kind="dm",
        rule=INTAKE,
        place=None,
        live=True,
        now=NOW,
        cap=10,
        bursts=Bursts(90, 600, 20),
    )
    assert store.take_ready(NOW) is None


def test_the_act_path(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    store.set_waiting(job.id, backend="codex", decision="act", draft=CANARY, now=NOW + 5)
    drafts = read_drafts(path)
    assert len(drafts) == 1
    assert drafts[0].job.status == "waiting_approval"
    assert (drafts[0].job.backend, drafts[0].job.decision) == ("codex", "act")
    assert drafts[0].text == CANARY
    assert CANARY not in repr(drafts[0])

    taken = store.take_waiting(job.id, NOW + 6)
    assert taken is not None and taken.job.status == "running" and taken.text == CANARY
    assert store.take_waiting(job.id, NOW + 7) is None
    assert read_drafts(path) == []

    store.set_done(job.id, now=NOW + 8, approval="approved")
    done = read_jobs(path)[0]
    assert (done.status, done.backend, done.decision, done.approval) == (
        "done",
        "codex",
        "act",
        "approved",
    )
    assert done.finished_at == NOW + 8
    assert query(path, "SELECT draft FROM jobs") == [(CANARY,)]


def test_set_waiting_touches_only_a_running_job(store: Store, path: Path) -> None:
    """A startup sweep must not undo an approval the terminal command finished first."""
    job = ready_job(store)
    store.take_ready(NOW)
    store.set_done(job.id, now=NOW + 1, backend="codex", decision="act", approval="approved")
    store.set_waiting(job.id, backend="codex", decision="act", draft=CANARY, now=NOW + 2)
    assert read_jobs(path)[0].status == "done"
    assert read_drafts(path) == []


def test_the_ignore_path(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    store.set_done(job.id, now=NOW + 3, backend="claude", decision="ignore")
    done = read_jobs(path)[0]
    assert (done.status, done.backend, done.decision, done.approval) == (
        "done",
        "claude",
        "ignore",
        None,
    )


def test_the_failed_path(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    store.set_failed(job.id, reason="agent_timeout", now=NOW + 3, backend="codex")
    failed = read_jobs(path)[0]
    assert (failed.status, failed.fail_reason, failed.backend) == (
        "failed",
        "agent_timeout",
        "codex",
    )
    assert failed.finished_at == NOW + 3


def test_drafts_expire_after_three_days(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    store.set_waiting(job.id, backend="codex", decision="act", draft="d", now=NOW)
    assert store.expire_drafts(NOW + DRAFT_RETENTION_SECONDS - 1) == []
    expired = store.expire_drafts(NOW + DRAFT_RETENTION_SECONDS + 1)
    assert [(item.id, item.status, item.fail_reason) for item in expired] == [
        (job.id, "failed", "expired")
    ]
    assert read_drafts(path) == []
    assert store.expire_drafts(NOW + DRAFT_RETENTION_SECONDS + 2) == []


def test_job_ids_and_posts(store: Store) -> None:
    job = ready_job(store, message=CANARY)
    assert store.job_ids() == {job.id}
    posts = store.posts(job.id)
    assert [post.message for post in posts] == [CANARY]
    assert store.posts(999) == []


def test_jobs_in_lists_by_status(store: Store) -> None:
    first = ready_job(store, "a", now=NOW)
    second = ready_job(store, "b", now=NOW + 1)
    assert [job.id for job in store.jobs_in("ready")] == [first.id, second.id]
    assert store.jobs_in("done") == []


def test_draft_is_a_frozen_pair() -> None:
    job = Job(1, "waiting_approval", "s", "r", None, 1, 0.0, 0.0, 0.0)
    draft = Draft(job, "text")
    assert draft.job is job and draft.text == "text"

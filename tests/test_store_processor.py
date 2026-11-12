"""The processor's side of the store: the schema and its upgrades, the hand-over, the finishing
writes, the outbox row of a reply, expiry."""

import sqlite3
from pathlib import Path

import pytest
from helpers import make_post

from mattermost_agent.rules import Bursts, DeliveryMode, Rule
from mattermost_agent.store import (
    DRAFT_RETENTION_SECONDS,
    FILE_NAME,
    JOB_RETENTION_SECONDS,
    SCHEMA_VERSION,
    Draft,
    Job,
    Store,
    StoreError,
    read_drafts,
    read_job,
    read_jobs,
    read_outbox,
    read_undelivered,
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
# What version 3 added: the processor's columns. Frozen here, like the schema above.
V3_COLUMNS = (
    "sender TEXT",
    "backend TEXT",
    "decision TEXT",
    "draft TEXT",
    "fail_reason TEXT",
    "approval TEXT",
    "finished_at REAL",
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


def queue(
    store: Store,
    job_id: int,
    text: str = CANARY,
    *,
    mode: DeliveryMode = "draft",
    root_id: str = "",
    downgrade: str | None = None,
    refused: bool = False,
) -> bool:
    """Finish a running job with a reply for the outbox, as the processor's loop does."""
    return store.finish_with_reply(
        job_id,
        mode=mode,
        root_id=root_id,
        message=text,
        downgrade=downgrade,
        refused=refused,
        backend="codex",
        decision="act",
        now=NOW + 5,
    )


def query(path: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def columns(path: Path) -> list[str]:
    return [str(row[1]) for row in query(path, "PRAGMA table_info(jobs)")]


def test_a_new_file_is_version_4(store: Store, path: Path) -> None:
    assert SCHEMA_VERSION == 4
    assert query(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert columns(path)[-9:] == [
        "sender",
        "backend",
        "decision",
        "draft",
        "fail_reason",
        "approval",
        "finished_at",
        "reply_mode",
        "delivery",
    ]
    assert read_outbox(path) == []


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


def test_a_version_3_file_is_migrated_with_its_rows(path: Path) -> None:
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    for statement in V2_SCHEMA:
        connection.execute(statement)
    for column in V3_COLUMNS:
        connection.execute(f"ALTER TABLE jobs ADD COLUMN {column}")
    connection.execute(
        "INSERT INTO jobs (conversation_key, channel_id, root_id, sender_id, place, rule, "
        "scenario, status, opened_at, ready_at, deadline_at, updated_at, post_count, sender, "
        "backend, decision, draft) "
        "VALUES ('k', ?, 'root', 'u', '@ivan', 'r', 's', 'waiting_approval', 1, 2, 3, 4, 1, "
        "'ivan', 'codex', 'act', ?)",
        (CHANNEL, CANARY),
    )
    connection.execute("INSERT INTO job_posts VALUES ('p', 1, 1000, 'dm', 'hello')")
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    connection.close()

    store = Store(path)
    assert query(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    fresh = Store(path.parent / "fresh.sqlite3")
    assert columns(path) == columns(fresh.path)
    schema = "SELECT sql FROM sqlite_master WHERE name LIKE 'outbox%' ORDER BY name"
    assert query(path, schema) == query(fresh.path, schema)
    drafts = read_drafts(path)
    assert [(d.job.id, d.job.root_id, d.job.sender, d.text) for d in drafts] == [
        (1, "root", "ivan", CANARY)
    ]
    assert (drafts[0].job.reply_mode, drafts[0].job.delivery) == (None, None)
    assert store.posts(1)[0].message == "hello"
    assert read_outbox(path) == []
    # The upgraded file takes an outbox row like a new one.
    assert store.take_waiting(1, NOW) is not None
    assert queue(store, 1)


def test_a_version_2_file_goes_all_the_way(path: Path) -> None:
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    for statement in V2_SCHEMA:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    Store(path)
    fresh = Store(path.parent / "fresh.sqlite3")
    assert query(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert columns(path) == columns(fresh.path)
    assert read_outbox(path) == []


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


def test_a_taken_job_knows_its_thread(store: Store) -> None:
    post = make_post(id="reply", channel_id=CHANNEL, root_id="root-post")
    store.claim(
        post,
        kind="thread_reply",
        rule=INTAKE,
        place="analytics",
        live=True,
        now=NOW,
        cap=10,
        bursts=BURSTS,
    )
    taken = store.take_ready(NOW + 1)
    assert taken is not None and taken.root_id == "root-post"
    assert ready_job(store, "top").root_id == ""


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


def test_only_running_and_waiting_jobs_are_unfinished(store: Store) -> None:
    collecting = make_post(id="c", channel_id="other")
    store.claim(
        collecting,
        kind="dm",
        rule=INTAKE,
        place=None,
        live=True,
        now=NOW,
        cap=10,
        bursts=Bursts(90, 600, 20),
    )
    ready_job(store, "ready", now=NOW + 5)
    ids = []
    for post_id in ("running", "waiting", "done", "failed"):
        ready_job(store, post_id, now=NOW)
        taken = store.take_ready(NOW + 1)
        assert taken is not None
        ids.append(taken.id)
    running, waiting, done, failed = ids
    store.set_waiting(waiting, backend="codex", decision="act", draft="d", now=NOW + 2)
    store.set_done(done, now=NOW + 2)
    store.set_failed(failed, reason="agent_failed", now=NOW + 2)
    assert store.unfinished_job_ids() == {running, waiting}


def test_posts(store: Store) -> None:
    job = ready_job(store, message=CANARY)
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


def test_a_reply_is_queued_as_the_job_finishes(store: Store, path: Path) -> None:
    post = make_post(id="first", channel_id=CHANNEL, create_at=5_000)
    store.claim(
        post,
        kind="channel",
        rule=INTAKE,
        place="analytics",
        live=True,
        now=NOW,
        cap=10,
        bursts=BURSTS,
    )
    job = store.take_ready(NOW)
    assert job is not None
    assert queue(store, job.id, mode="send", root_id="first", downgrade=None)

    done = read_jobs(path)[0]
    assert (done.status, done.backend, done.decision) == ("done", "codex", "act")
    assert (done.reply_mode, done.delivery, done.finished_at) == ("send", "queued", NOW + 5)
    assert query(path, "SELECT draft FROM jobs") == [(None,)]
    (row,) = read_outbox(path)
    assert (row.job_id, row.mode, row.channel_id, row.root_id) == (job.id, "send", CHANNEL, "first")
    assert (row.last_post_at, row.state, row.outcome, row.downgrade) == (
        5_000,
        "pending",
        None,
        None,
    )
    assert (row.reason, row.post_id, row.attempts, row.retry_at) == (None, None, 0, None)
    assert (row.created_at, row.updated_at, row.place) == (NOW + 5, NOW + 5, "analytics")
    (reply,) = read_undelivered(path)
    assert reply.row == row and reply.text == CANARY
    assert CANARY not in repr(reply) and CANARY not in repr(row)


def test_last_post_at_is_the_bursts_last_post(store: Store, path: Path) -> None:
    bursts = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=3)
    for post_id, create_at in (("a", 7_000), ("b", 9_000), ("c", 8_000)):
        post = make_post(id=post_id, channel_id=CHANNEL, create_at=create_at)
        store.claim(
            post, kind="dm", rule=INTAKE, place=None, live=True, now=NOW, cap=10, bursts=bursts
        )
    job = store.take_ready(NOW)
    assert job is not None and job.post_count == 3
    assert queue(store, job.id)
    assert read_outbox(path)[0].last_post_at == 9_000


def test_a_reply_is_queued_once_and_only_for_a_running_job(store: Store, path: Path) -> None:
    job = ready_job(store)
    assert not queue(store, job.id)  # still ready: the processor has not taken it
    assert read_outbox(path) == [] and read_jobs(path)[0].status == "ready"
    store.take_ready(NOW)
    assert queue(store, job.id, text="first")
    assert not queue(store, job.id, text="second", mode="send")  # a repeat after a crash
    (reply,) = read_undelivered(path)
    assert (reply.row.mode, reply.text) == ("draft", "first")
    assert not queue(store, 999)


def test_a_downgrade_is_recorded_with_the_reply(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    assert queue(store, job.id, downgrade="link")
    (row,) = read_outbox(path)
    assert (row.mode, row.state, row.downgrade) == ("draft", "pending", "link")


def test_a_refused_reply_is_done_at_once_and_keeps_its_text(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    assert queue(store, job.id, text="/away", refused=True)
    done = read_jobs(path)[0]
    assert (done.status, done.reply_mode, done.delivery) == ("done", "draft", "refused_command")
    (reply,) = read_undelivered(path)
    assert (reply.row.state, reply.row.outcome, reply.text) == ("done", "refused_command", "/away")


def test_a_delivered_reply_is_not_listed_with_the_undelivered(store: Store, path: Path) -> None:
    job = ready_job(store)
    store.take_ready(NOW)
    queue(store, job.id)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE outbox SET message = NULL, state = 'done', outcome = 'drafted'")
    connection.commit()
    connection.close()
    assert read_undelivered(path) == []
    assert [row.outcome for row in read_outbox(path)] == ["drafted"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE outbox SET mode = 'local'",
        "UPDATE outbox SET mode = 'post'",
        "UPDATE outbox SET state = 'sent'",
        "INSERT INTO outbox (job_id, mode, channel_id, root_id, last_post_at, state, "
        "created_at, updated_at) VALUES (1, 'draft', 'c', '', 1, 'pending', 1, 1)",
        "INSERT INTO outbox (job_id, mode, channel_id, root_id, last_post_at, state, "
        "created_at, updated_at) VALUES (999, 'draft', 'c', '', 1, 'pending', 1, 1)",
    ],
)
def test_the_outbox_refuses_what_the_code_never_writes(
    store: Store, path: Path, statement: str
) -> None:
    """Other modes and states, a second row for a job, and a row for no job."""
    job = ready_job(store)
    store.take_ready(NOW)
    queue(store, job.id)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
    finally:
        connection.close()


def test_an_outbox_row_is_pruned_with_its_job_and_its_text_leaves_the_files(
    store: Store, path: Path
) -> None:
    old = ready_job(store, "old", now=NOW)
    store.take_ready(NOW)
    queue(store, old.id, text=f"reply {CANARY}")
    new = ready_job(store, "new", now=NOW + 100)
    store.take_ready(NOW + 100)
    queue(store, new.id, text="kept")
    folder = path.parent
    assert CANARY.encode() in b"".join(file.read_bytes() for file in folder.iterdir())

    pruned = store.prune(NOW + JOB_RETENTION_SECONDS + 1)
    assert pruned.jobs == 1
    assert [row.job_id for row in read_outbox(path)] == [new.id]
    assert [reply.text for reply in read_undelivered(path)] == ["kept"]
    assert CANARY.encode() not in b"".join(file.read_bytes() for file in folder.iterdir())

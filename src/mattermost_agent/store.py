"""SQLite storage of what the listener must remember across restarts.

It holds the ids of handled posts, the last-seen time, and the jobs: bursts of matched posts, with
their text. Message text lives in job_posts.message and nowhere else, and only a post that matched
a rule has a row there. The processor adds the agent's decision and draft to the job's row. Post
times (create_at, last-seen) are the server's Unix milliseconds. `now` and the job times are this
machine's Unix seconds.

Every operation opens its own short connection, and the queries are tiny, so they run directly on
the event loop. The `jobs` and `drafts` commands read the same file through read_jobs, read_job and
read_drafts, read-only, while the listener and the processor run. The listener owns a job up to
`ready`; the processor owns it from there (take_ready is the hand-over, a compare-and-set).
"""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, NamedTuple, get_args

from mattermost_agent.classify import Kind
from mattermost_agent.events import Post
from mattermost_agent.rules import Bursts, Rule

FILE_NAME = "state.sqlite3"
SCHEMA_VERSION = 3
RETENTION_MS = 30 * 24 * 60 * 60 * 1000
JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60
# A draft nobody approved or rejected in this time fails as `expired`.
DRAFT_RETENTION_SECONDS = 3 * 24 * 60 * 60
CAP_WINDOW_SECONDS = 3600

type JobStatus = Literal["collecting", "ready", "running", "waiting_approval", "done", "failed"]
JOB_STATUSES: tuple[str, ...] = get_args(JobStatus.__value__)
type FailReason = Literal[
    "agent_failed",
    "agent_timeout",
    "invalid_output",
    "unknown_scenario",
    "unknown_rule",
    "bad_folder",
    "expired",
]
FAIL_REASONS: tuple[str, ...] = get_args(FailReason.__value__)
type Approval = Literal["approved", "rejected"]
APPROVALS: tuple[str, ...] = get_args(Approval.__value__)

# The statuses after `ready` and the nullable columns from `sender` on belong to the processor.
SCHEMA = (
    """
    CREATE TABLE handled (
        post_id TEXT PRIMARY KEY,
        create_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE last_seen (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        create_at INTEGER NOT NULL
    )
    """,
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
        post_count INTEGER NOT NULL,
        sender TEXT,
        backend TEXT,
        decision TEXT,
        draft TEXT,
        fail_reason TEXT,
        approval TEXT,
        finished_at REAL
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
# From one schema version to the next, each in one transaction. Version 2 → 3: the processor's
# columns, all nullable, so the listener's rows need no values.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: tuple(
        f"ALTER TABLE jobs ADD COLUMN {column}"
        for column in (
            "sender TEXT",
            "backend TEXT",
            "decision TEXT",
            "draft TEXT",
            "fail_reason TEXT",
            "approval TEXT",
            "finished_at REAL",
        )
    ),
}
# In the order of Job's fields.
JOB_COLUMNS = (
    "id, status, scenario, rule, place, post_count, opened_at, ready_at, deadline_at, "
    "channel_id, sender, backend, decision, fail_reason, approval, finished_at, root_id"
)

type Claim = Literal["duplicate", "unmatched", "cap_hit", "opened", "joined"]


class StoreError(Exception):
    """The database cannot be used. The message says which file and why."""


@dataclass(frozen=True)
class Job:
    """One burst, without its text. Times are this machine's Unix seconds."""

    id: int
    status: JobStatus
    scenario: str
    rule: str
    place: str | None
    post_count: int
    opened_at: float
    ready_at: float
    deadline_at: float
    channel_id: str = ""
    sender: str | None = None
    backend: str | None = None
    decision: str | None = None
    fail_reason: str | None = None
    approval: str | None = None
    finished_at: float | None = None
    root_id: str = ""


@dataclass(frozen=True)
class JobPost:
    """One post of a job. repr() leaves out the text."""

    post_id: str
    create_at: int
    kind: str
    message: str = field(repr=False)


@dataclass(frozen=True)
class Draft:
    """A job waiting for approval, with its draft. repr() leaves out the text."""

    job: Job
    text: str = field(repr=False)


@dataclass(frozen=True)
class Outcome:
    """What became of a post: the job it opened or joined, and every job the call made ready."""

    claim: Claim
    job: Job | None = None
    closed: tuple[Job, ...] = ()


class Pruned(NamedTuple):
    handled: int
    jobs: int


def _jobs(connection: sqlite3.Connection, tail: str, params: tuple[object, ...]) -> list[Job]:
    """Return the jobs selected by `tail`, a fixed clause whose values come in as parameters."""
    rows = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs {tail}", params)  # noqa: S608
    return [Job(*row) for row in rows]


def _posts(connection: sqlite3.Connection, job_id: int) -> list[JobPost]:
    rows = connection.execute(
        "SELECT post_id, create_at, kind, message FROM job_posts "
        "WHERE job_id = ? ORDER BY create_at, post_id",
        (job_id,),
    )
    return [JobPost(*row) for row in rows]


def _close(connection: sqlite3.Connection, job: Job, now: float) -> Job:
    connection.execute(
        "UPDATE jobs SET status = 'ready', updated_at = ? WHERE id = ?", (now, job.id)
    )
    return replace(job, status="ready")


def _add_post(connection: sqlite3.Connection, job_id: int, post: Post, kind: Kind) -> None:
    connection.execute(
        "INSERT INTO job_posts (post_id, job_id, create_at, kind, message) VALUES (?, ?, ?, ?, ?)",
        (post.id, job_id, post.create_at, kind, post.message),
    )


class Store:
    """Open the database, creating it if needed, or raise StoreError so startup can fail fast."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Created by hand, because SQLite would use the default mode and leave a moment in
            # which others could read the file. SQLite gives its -wal and -shm files this mode.
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self._initialise()
        except (OSError, sqlite3.Error) as error:
            raise StoreError(f"cannot use the database {path}: {error}") from error

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions, the code below says BEGIN itself.
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        # Deleted text is overwritten with zeros, so that a pruned message cannot be read back
        # from the file. The WAL is emptied after each prune for the same reason.
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        try:
            connection = self._connect()
            try:
                # IMMEDIATE takes the write lock now, so a check and the write after it are atomic.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute("COMMIT")
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise StoreError(f"cannot use the database {self.path}: {error}") from error

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()
        # Also the writability check: BEGIN IMMEDIATE fails on a read-only file or directory.
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in SCHEMA:
                    connection.execute(statement)
            elif version != SCHEMA_VERSION and version not in MIGRATIONS:
                raise sqlite3.DatabaseError(f"unknown schema version {version}")
            while version in MIGRATIONS:
                for statement in MIGRATIONS[version]:
                    connection.execute(statement)
                version += 1
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def claim(
        self,
        post: Post,
        *,
        kind: Kind,
        rule: Rule | None,
        place: str | None,
        sender: str | None = None,
        live: bool,
        now: float,
        cap: int,
        bursts: Bursts,
    ) -> Outcome:
        """Record a post exactly once, put a matched one into its burst, and say what became of it.

        Remembering the post, moving last-seen, opening or joining the job and keeping the text
        happen together or not at all, so a crash cannot leave them disagreeing. A post over the
        cap is remembered too, and so is not retried; it opens no job. Only `live` posts move
        last-seen: a crash during catch-up then simply repeats the catch-up.

        A burst is the posts of one sender in one channel and thread, matched by one rule. The
        next post joins it while it is `collecting` and its quiet time has not run out; otherwise
        the old job is made ready here, without waiting for the housekeeping loop, and the post
        opens a new one. The partial unique index allows one collecting job per key.

        TODO(processor): an edit or a deletion after the post arrived is not seen. The text
        stays as it was, and a deleted post stays in its job.
        """
        with self._transaction() as connection:
            if live:
                connection.execute(
                    "INSERT INTO last_seen (id, create_at) VALUES (1, ?) "
                    "ON CONFLICT (id) DO UPDATE SET create_at = MAX(create_at, excluded.create_at)",
                    (post.create_at,),
                )
            inserted = connection.execute(
                "INSERT OR IGNORE INTO handled (post_id, create_at) VALUES (?, ?)",
                (post.id, post.create_at),
            ).rowcount
            if not inserted:
                return Outcome("duplicate")
            if rule is None:
                return Outcome("unmatched")

            key = f"{post.channel_id}/{post.root_id}/{post.user_id}"
            closed: list[Job] = []
            found = _jobs(
                connection, "WHERE conversation_key = ? AND status = 'collecting'", (key,)
            )
            current = found[0] if found else None
            if current is not None and current.ready_at <= now:
                closed.append(_close(connection, current, now))
                current = None
            # The same rule means the same name and scenario: the file may have changed in between.
            same_rule = current is not None and current.rule == rule.name
            if current is not None and same_rule and current.scenario == rule.scenario:
                count = current.post_count + 1
                status: JobStatus = "ready" if count >= bursts.max_posts else "collecting"
                ready_at = min(now + bursts.quiet_seconds, current.deadline_at)
                connection.execute(
                    "UPDATE jobs SET status = ?, ready_at = ?, updated_at = ?, post_count = ? "
                    "WHERE id = ?",
                    (status, ready_at, now, count, current.id),
                )
                job = replace(current, status=status, ready_at=ready_at, post_count=count)
                _add_post(connection, job.id, post, kind)
                if status == "ready":
                    closed.append(job)
                return Outcome("joined", job, tuple(closed))

            recent = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE opened_at > ?", (now - CAP_WINDOW_SECONDS,)
            ).fetchone()[0]
            if recent >= cap:
                return Outcome("cap_hit", closed=tuple(closed))
            if current is not None:
                # Opened by another rule, or by this rule with another scenario. The post opens
                # its own job, so this one is complete.
                closed.append(_close(connection, current, now))
            status = "ready" if bursts.max_posts <= 1 else "collecting"
            job = Job(
                id=0,
                status=status,
                scenario=rule.scenario,
                rule=rule.name,
                place=place,
                post_count=1,
                opened_at=now,
                ready_at=now + bursts.quiet_seconds,
                deadline_at=now + bursts.max_wait_seconds,
                channel_id=post.channel_id,
                sender=sender,
            )
            row = connection.execute(
                "INSERT INTO jobs (conversation_key, channel_id, root_id, sender_id, place, rule, "
                "scenario, status, opened_at, ready_at, deadline_at, updated_at, post_count, "
                "sender) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    key,
                    post.channel_id,
                    post.root_id,
                    post.user_id,
                    place,
                    rule.name,
                    rule.scenario,
                    status,
                    now,
                    job.ready_at,
                    job.deadline_at,
                    now,
                    1,
                    sender,
                ),
            ).fetchone()
            job = replace(job, id=row[0])
            _add_post(connection, job.id, post, kind)
            if status == "ready":
                closed.append(job)
            return Outcome("opened", job, tuple(closed))

    def flip_ready(self, now: float) -> list[Job]:
        """Make every collecting job whose quiet time has run out ready, and return them."""
        with self._transaction() as connection:
            due = _jobs(connection, "WHERE status = 'collecting' AND ready_at <= ?", (now,))
            return [_close(connection, job, now) for job in due]

    def last_seen(self) -> int | None:
        """Return the last-seen time, or None on the very first run."""
        with self._transaction() as connection:
            row = connection.execute("SELECT create_at FROM last_seen WHERE id = 1").fetchone()
        return None if row is None else row[0]

    def set_first_last_seen(self, create_at: int) -> None:
        """Set where catch-up starts from on the first run. Later calls change nothing."""
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO last_seen (id, create_at) VALUES (1, ?)", (create_at,)
            )

    def take_ready(self, now: float) -> Job | None:
        """Move the oldest ready job to `running` and return it, or None if there is none.

        A compare-and-set: a second processor started by mistake cannot take the same job.
        """
        with self._transaction() as connection:
            rows = connection.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? "  # noqa: S608 - constant columns
                "WHERE id = (SELECT id FROM jobs WHERE status = 'ready' ORDER BY ready_at, id "
                f"LIMIT 1) AND status = 'ready' RETURNING {JOB_COLUMNS}",
                (now,),
            ).fetchall()
        return Job(*rows[0]) if rows else None

    def take_waiting(self, job_id: int, now: float) -> Draft | None:
        """Move one waiting job to `running` for its approval, and return it with its draft."""
        with self._transaction() as connection:
            rows = connection.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? "  # noqa: S608 - constant columns
                f"WHERE id = ? AND status = 'waiting_approval' RETURNING {JOB_COLUMNS}, draft",
                (now, job_id),
            ).fetchall()
        if not rows:
            return None
        *columns, draft = rows[0]
        return Draft(Job(*columns), draft or "")

    def job(self, job_id: int) -> Job | None:
        with self._transaction() as connection:
            found = _jobs(connection, "WHERE id = ?", (job_id,))
        return found[0] if found else None

    def jobs_in(self, status: JobStatus) -> list[Job]:
        """Return every job in `status`, oldest first."""
        with self._transaction() as connection:
            return _jobs(connection, "WHERE status = ? ORDER BY id", (status,))

    def job_ids(self) -> set[int]:
        with self._transaction() as connection:
            return {row[0] for row in connection.execute("SELECT id FROM jobs")}

    def posts(self, job_id: int) -> list[JobPost]:
        """Return the job's posts with their text, oldest first."""
        with self._transaction() as connection:
            return _posts(connection, job_id)

    def set_waiting(
        self, job_id: int, *, backend: str, decision: str, draft: str, now: float
    ) -> None:
        """Record the agent's draft and wait for approval.

        Only a `running` job can start waiting: a startup sweep that resumes a paused thread must
        not undo an approval the terminal command finished first.
        """
        with self._transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'waiting_approval', backend = ?, decision = ?, "
                "draft = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                (backend, decision, draft, now, job_id),
            )

    def set_done(
        self,
        job_id: int,
        *,
        now: float,
        backend: str | None = None,
        decision: str | None = None,
        approval: Approval | None = None,
    ) -> None:
        """Finish the job. Values left None keep what set_waiting recorded."""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'done', backend = COALESCE(?, backend), "
                "decision = COALESCE(?, decision), approval = ?, finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                (backend, decision, approval, now, now, job_id),
            )

    def set_failed(
        self, job_id: int, *, reason: FailReason, now: float, backend: str | None = None
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed', fail_reason = ?, "
                "backend = COALESCE(?, backend), finished_at = ?, updated_at = ? WHERE id = ?",
                (reason, backend, now, now, job_id),
            )

    def expire_drafts(self, now: float) -> list[Job]:
        """Fail every draft that waited longer than DRAFT_RETENTION_SECONDS, and return them."""
        with self._transaction() as connection:
            rows = connection.execute(
                "UPDATE jobs SET status = 'failed', fail_reason = 'expired', "  # noqa: S608
                "finished_at = ?, updated_at = ? WHERE status = 'waiting_approval' "
                f"AND updated_at < ? RETURNING {JOB_COLUMNS}",
                (now, now, now - DRAFT_RETENTION_SECONDS),
            ).fetchall()
        return [Job(*row) for row in rows]

    def prune(self, now: float) -> Pruned:
        """Forget old posts and old jobs, with their text, and return how many of each.

        Posts are forgotten once catch-up cannot reach them, measured from last-seen and not from
        today, so that a long outage cannot forget posts that the next catch-up will read again.
        Jobs are forgotten 7 days after they opened, whatever their status.
        """
        with self._transaction() as connection:
            handled = connection.execute(
                "DELETE FROM handled "
                "WHERE create_at < (SELECT create_at - ? FROM last_seen WHERE id = 1)",
                (RETENTION_MS,),
            ).rowcount
            cutoff = now - JOB_RETENTION_SECONDS
            connection.execute(
                "DELETE FROM job_posts WHERE job_id IN (SELECT id FROM jobs WHERE opened_at < ?)",
                (cutoff,),
            )
            jobs = connection.execute("DELETE FROM jobs WHERE opened_at < ?", (cutoff,)).rowcount
        self._checkpoint()
        return Pruned(handled, jobs)

    def _checkpoint(self) -> None:
        """Move the WAL into the database file and empty it, so that deleted text leaves it too.

        It needs no transaction, and a reader can make it wait; then the next prune does it.
        """
        try:
            connection = self._connect()
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise StoreError(f"cannot use the database {self.path}: {error}") from error


@contextmanager
def _read_only(path: Path) -> Generator[sqlite3.Connection]:
    """Open the database read-only, as the `jobs` command does while the listener runs."""
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"schema version {version}; start the listener or the processor once to update"
                )
            yield connection
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise StoreError(f"cannot use the database {path}: {error}") from error


def read_jobs(path: Path) -> list[Job]:
    """Return every job, newest first."""
    with _read_only(path) as connection:
        return _jobs(connection, "ORDER BY id DESC", ())


def read_job(path: Path, job_id: int) -> tuple[Job, list[JobPost]] | None:
    """Return one job and its posts, oldest first, or None if there is no such job."""
    with _read_only(path) as connection:
        found = _jobs(connection, "WHERE id = ?", (job_id,))
        if not found:
            return None
        return found[0], _posts(connection, job_id)


def read_drafts(path: Path) -> list[Draft]:
    """Return every job waiting for approval with its draft, oldest first."""
    with _read_only(path) as connection:
        rows = connection.execute(
            f"SELECT {JOB_COLUMNS}, draft FROM jobs "  # noqa: S608 - a constant column list
            "WHERE status = 'waiting_approval' ORDER BY id"
        )
        return [Draft(Job(*row[:-1]), row[-1] or "") for row in rows]

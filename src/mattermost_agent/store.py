"""SQLite storage of what the listener must remember across restarts.

It holds the ids of handled posts, the last-seen time and the hourly matches, and never any message
text. Post times (create_at, last-seen) are the server's Unix milliseconds. `now` and matched_at
are this machine's Unix seconds.

Every operation opens its own short connection, and the queries are tiny, so they run directly on
the event loop.
"""

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

FILE_NAME = "state.sqlite3"
SCHEMA_VERSION = 1
RETENTION_MS = 30 * 24 * 60 * 60 * 1000
CAP_WINDOW_SECONDS = 3600

SCHEMA = (
    """
    CREATE TABLE handled (
        post_id TEXT PRIMARY KEY,
        create_at INTEGER NOT NULL,
        matched_at REAL
    )
    """,
    "CREATE INDEX handled_matched_at ON handled (matched_at) WHERE matched_at IS NOT NULL",
    """
    CREATE TABLE last_seen (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        create_at INTEGER NOT NULL
    )
    """,
)

type Claim = Literal["duplicate", "unmatched", "matched", "cap_hit"]


class StoreError(Exception):
    """The database cannot be used. The message says which file and why."""


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
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise sqlite3.DatabaseError(f"unknown schema version {version}")

    def claim(
        self,
        post_id: str,
        create_at: int,
        *,
        matched: bool,
        live: bool,
        now: float,
        cap: int,
    ) -> Claim:
        """Record a post exactly once, and say what became of it.

        Remembering the post, counting it against the cap and moving last-seen happen together
        or not at all, so a crash cannot leave them disagreeing. A post over the cap is remembered
        too, and so is not retried; it does not count as a match. Only `live` posts move last-seen:
        a crash during catch-up then simply repeats the catch-up.
        """
        with self._transaction() as connection:
            if live:
                connection.execute(
                    "INSERT INTO last_seen (id, create_at) VALUES (1, ?) "
                    "ON CONFLICT (id) DO UPDATE SET create_at = MAX(create_at, excluded.create_at)",
                    (create_at,),
                )
            inserted = connection.execute(
                "INSERT OR IGNORE INTO handled (post_id, create_at) VALUES (?, ?)",
                (post_id, create_at),
            ).rowcount
            if not inserted:
                return "duplicate"
            if not matched:
                return "unmatched"
            recent = connection.execute(
                "SELECT COUNT(*) FROM handled WHERE matched_at > ?",
                (now - CAP_WINDOW_SECONDS,),
            ).fetchone()[0]
            if recent >= cap:
                return "cap_hit"
            connection.execute(
                "UPDATE handled SET matched_at = ? WHERE post_id = ?", (now, post_id)
            )
            return "matched"

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

    def prune(self, now: float) -> int:
        """Forget posts too old for catch-up to reach, and return how many.

        Measured from last-seen and not from today, so that a long outage cannot forget posts
        that the next catch-up will read again. Matches still counting against the cap stay.
        """
        with self._transaction() as connection:
            return connection.execute(
                "DELETE FROM handled "
                "WHERE create_at < (SELECT create_at - ? FROM last_seen WHERE id = 1) "
                "AND (matched_at IS NULL OR matched_at <= ?)",
                (RETENTION_MS, now - CAP_WINDOW_SECONDS),
            ).rowcount

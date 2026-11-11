"""The processor's LangGraph checkpointer: its own SQLite file next to the listener's.

It holds step progress for the jobs being processed (the job id is the thread id), and the
agent's memory and draft texts while a job runs; never a post. The file is created by us with
mode 600 (SqliteSaver would use 644), deleted text is overwritten, and a finished job's thread is
deleted at once, then the WAL is emptied.
"""

import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

FILE_NAME = "checkpoints.sqlite3"


def open_checkpointer(state_dir: Path) -> SqliteSaver:
    """Open or create the checkpoint file, private to this user, and prepare its tables."""
    path = state_dir / FILE_NAME
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    # LangGraph may run a node in a worker thread; the saver has its own lock.
    connection = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA secure_delete = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    saver = SqliteSaver(connection)
    saver.setup()
    return saver


def thread_ids(saver: SqliteSaver) -> set[str]:
    """Return every thread that still has a checkpoint."""
    with saver.lock:
        rows = saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    return {row[0] for row in rows}


def forget_thread(saver: SqliteSaver, job_id: int) -> None:
    """Delete a job's checkpoints and empty the WAL, so that its texts leave the file too."""
    saver.delete_thread(str(job_id))
    with saver.lock:
        saver.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

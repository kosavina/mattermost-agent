import sqlite3
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from mattermost_agent.processor.checkpoints import (
    FILE_NAME,
    forget_thread,
    open_checkpointer,
    thread_ids,
)


@dataclass
class State:
    job_id: int
    note: str = ""


def graph_with(saver: SqliteSaver) -> CompiledStateGraph:
    builder = StateGraph(State)
    builder.add_node("step", lambda state: {"note": f"done {state.job_id}"})
    builder.add_edge(START, "step")
    builder.add_edge("step", END)
    return builder.compile(checkpointer=saver)


def test_the_file_is_private_and_in_wal_mode(tmp_path: Path) -> None:
    saver = open_checkpointer(tmp_path / "state")
    path = tmp_path / "state" / FILE_NAME
    assert (path.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "state").stat().st_mode & 0o777) == 0o700
    assert saver.conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert saver.conn.execute("PRAGMA secure_delete").fetchone() == (1,)
    assert thread_ids(saver) == set()


def test_threads_are_listed_and_forgotten(tmp_path: Path) -> None:
    saver = open_checkpointer(tmp_path / "state")
    graph = graph_with(saver)
    for job_id in (1, 2):
        graph.invoke({"job_id": job_id}, {"configurable": {"thread_id": str(job_id)}})
    assert thread_ids(saver) == {"1", "2"}
    forget_thread(saver, 1)
    assert thread_ids(saver) == {"2"}
    assert graph.get_state({"configurable": {"thread_id": "1"}}).values == {}
    forget_thread(saver, 1)  # already gone: no error


def test_forgotten_text_leaves_the_files(tmp_path: Path) -> None:
    saver = open_checkpointer(tmp_path / "state")
    graph = graph_with(saver)
    graph.invoke({"job_id": 7, "note": "zebraquartz"}, {"configurable": {"thread_id": "7"}})
    folder = tmp_path / "state"
    assert any(b"zebraquartz" in file.read_bytes() for file in folder.iterdir())
    forget_thread(saver, 7)
    assert not any(b"zebraquartz" in file.read_bytes() for file in folder.iterdir())


def test_reopening_keeps_the_mode(tmp_path: Path) -> None:
    open_checkpointer(tmp_path / "state").conn.close()
    saver = open_checkpointer(tmp_path / "state")
    assert ((tmp_path / "state" / FILE_NAME).stat().st_mode & 0o777) == 0o600
    assert isinstance(saver.conn, sqlite3.Connection)

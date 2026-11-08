import os
import sqlite3
import stat
from pathlib import Path

import pytest

from mattermost_agent.store import (
    CAP_WINDOW_SECONDS,
    FILE_NAME,
    RETENTION_MS,
    Claim,
    Store,
    StoreError,
)

NOW = 1_000_000.0
CAP = 2


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state" / FILE_NAME


@pytest.fixture
def store(path: Path) -> Store:
    return Store(path)


def claim(
    store: Store,
    post_id: str,
    *,
    matched: bool = True,
    live: bool = True,
    create_at: int = 1_000,
    now: float = NOW,
) -> Claim:
    return store.claim(post_id, create_at, matched=matched, live=live, now=now, cap=CAP)


def row(path: Path, post_id: str) -> tuple[int, float | None] | None:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT create_at, matched_at FROM handled WHERE post_id = ?", (post_id,)
        ).fetchone()
    finally:
        connection.close()


def test_a_new_post_is_recorded(store: Store, path: Path) -> None:
    assert claim(store, "a", matched=False) == "unmatched"
    assert row(path, "a") == (1_000, None)


def test_a_matched_post_is_recorded_with_its_time(store: Store, path: Path) -> None:
    assert claim(store, "a") == "matched"
    assert row(path, "a") == (1_000, NOW)


def test_a_duplicate_is_reported(store: Store) -> None:
    assert claim(store, "a") == "matched"
    assert claim(store, "a") == "duplicate"


def test_a_duplicate_does_not_use_the_cap(store: Store) -> None:
    assert claim(store, "a") == "matched"
    assert claim(store, "a") == "duplicate"
    assert claim(store, "a") == "duplicate"
    assert claim(store, "b") == "matched"


def test_unmatched_posts_do_not_use_the_cap(store: Store) -> None:
    for number in range(5):
        assert claim(store, f"plain-{number}", matched=False) == "unmatched"
    assert claim(store, "a") == "matched"
    assert claim(store, "b") == "matched"


def test_the_cap_is_hit(store: Store) -> None:
    assert claim(store, "a") == "matched"
    assert claim(store, "b") == "matched"
    assert claim(store, "c") == "cap_hit"


def test_a_post_over_the_cap_is_not_retried(store: Store, path: Path) -> None:
    claim(store, "a")
    claim(store, "b")
    assert claim(store, "c") == "cap_hit"
    assert row(path, "c") == (1_000, None)
    assert claim(store, "c", now=NOW + CAP_WINDOW_SECONDS + 1) == "duplicate"


def test_catch_up_counts_against_the_cap(store: Store) -> None:
    assert claim(store, "a", live=False) == "matched"
    assert claim(store, "b", live=False) == "matched"
    assert claim(store, "c", live=False) == "cap_hit"


def test_posts_over_the_cap_do_not_extend_it(store: Store) -> None:
    claim(store, "a")
    claim(store, "b")
    claim(store, "c")
    assert claim(store, "d", now=NOW + CAP_WINDOW_SECONDS) == "matched"


def test_the_hour_slides(store: Store) -> None:
    claim(store, "a", now=NOW)
    claim(store, "b", now=NOW + 1000)
    assert claim(store, "c", now=NOW + CAP_WINDOW_SECONDS - 1) == "cap_hit"
    assert claim(store, "d", now=NOW + CAP_WINDOW_SECONDS) == "matched"
    assert claim(store, "e", now=NOW + CAP_WINDOW_SECONDS + 1) == "cap_hit"


def test_the_cap_survives_a_restart(store: Store, path: Path) -> None:
    claim(store, "a")
    claim(store, "b")
    assert claim(Store(path), "c") == "cap_hit"


def test_handled_posts_survive_a_restart(store: Store, path: Path) -> None:
    claim(store, "a")
    assert claim(Store(path), "a") == "duplicate"


def test_no_last_seen_on_the_first_run(store: Store) -> None:
    assert store.last_seen() is None


def test_a_live_post_moves_last_seen(store: Store) -> None:
    claim(store, "a", create_at=500)
    assert store.last_seen() == 500


def test_catch_up_does_not_move_last_seen(store: Store) -> None:
    store.set_first_last_seen(100)
    claim(store, "a", live=False, create_at=500)
    assert store.last_seen() == 100


def test_last_seen_never_goes_back(store: Store) -> None:
    claim(store, "a", create_at=500)
    claim(store, "b", create_at=400)
    assert store.last_seen() == 500


def test_a_live_duplicate_still_moves_last_seen(store: Store) -> None:
    claim(store, "a", live=False, create_at=500)
    assert claim(store, "a", live=True, create_at=500) == "duplicate"
    assert store.last_seen() == 500


def test_an_unmatched_live_post_moves_last_seen(store: Store) -> None:
    claim(store, "a", matched=False, create_at=500)
    assert store.last_seen() == 500


def test_first_last_seen_is_set_only_once(store: Store) -> None:
    store.set_first_last_seen(100)
    store.set_first_last_seen(900)
    assert store.last_seen() == 100


def test_last_seen_survives_a_restart(store: Store, path: Path) -> None:
    claim(store, "a", create_at=500)
    assert Store(path).last_seen() == 500


def test_prune_forgets_only_posts_beyond_retention(store: Store, path: Path) -> None:
    latest = RETENTION_MS + 1_000
    claim(store, "old", matched=False, create_at=999)
    claim(store, "edge", matched=False, create_at=1_000)
    claim(store, "new", matched=False, create_at=latest)
    assert store.prune(NOW) == 1
    assert row(path, "old") is None
    assert row(path, "edge") is not None
    assert row(path, "new") is not None


def test_prune_does_nothing_on_the_first_run(store: Store, path: Path) -> None:
    claim(store, "a", live=False, matched=False, create_at=1)
    assert store.prune(NOW) == 0
    assert row(path, "a") is not None


def test_prune_is_measured_from_last_seen_and_not_from_today(store: Store, path: Path) -> None:
    claim(store, "a", matched=False, create_at=1_000)
    assert store.prune(NOW + 10 * RETENTION_MS) == 0
    assert row(path, "a") is not None


def test_prune_keeps_matches_that_still_count_against_the_cap(store: Store) -> None:
    claim(store, "old", create_at=1)
    claim(store, "new", create_at=RETENTION_MS + 1_000, now=NOW)
    assert store.prune(NOW) == 0
    assert claim(store, "c", create_at=RETENTION_MS + 1_000) == "cap_hit"


def test_prune_removes_old_matches_after_the_hour(store: Store, path: Path) -> None:
    claim(store, "old", create_at=1)
    claim(store, "new", create_at=RETENTION_MS + 1_000)
    assert store.prune(NOW + CAP_WINDOW_SECONDS) == 1
    assert row(path, "old") is None


def test_files_are_private(store: Store, path: Path) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_no_column_can_hold_message_text(store: Store, path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        columns = {
            name: {column[1] for column in connection.execute(f"PRAGMA table_info({name})")}
            for (name,) in tables
        }
    finally:
        connection.close()
    assert columns == {
        "handled": {"post_id", "create_at", "matched_at"},
        "last_seen": {"id", "create_at"},
    }


def test_a_read_only_folder_fails_at_startup(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    folder = tmp_path / "locked"
    folder.mkdir(mode=0o500)
    try:
        with pytest.raises(StoreError, match="cannot use the database"):
            Store(folder / FILE_NAME)
    finally:
        folder.chmod(0o700)


def test_a_folder_in_place_of_the_file_fails_at_startup(tmp_path: Path) -> None:
    (tmp_path / FILE_NAME).mkdir()
    with pytest.raises(StoreError, match="cannot use the database"):
        Store(tmp_path / FILE_NAME)


def test_a_file_that_is_not_a_database_fails_at_startup(tmp_path: Path) -> None:
    path = tmp_path / FILE_NAME
    path.write_bytes(b"this is not a database, " * 100)
    with pytest.raises(StoreError, match="cannot use the database"):
        Store(path)


def test_an_unknown_schema_version_fails_at_startup(store: Store, path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 99")
    finally:
        connection.close()
    with pytest.raises(StoreError, match="unknown schema version 99"):
        Store(path)


def test_a_failure_later_on_raises_store_error(store: Store, path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    path.chmod(0o400)
    with pytest.raises(StoreError, match="cannot use the database"):
        claim(store, "a")

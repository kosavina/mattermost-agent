import os
import sqlite3
import stat
from pathlib import Path

import pytest
from helpers import make_post

from mattermost_agent.rules import Bursts, Rule
from mattermost_agent.store import (
    CAP_WINDOW_SECONDS,
    FILE_NAME,
    JOB_RETENTION_SECONDS,
    JOB_STATUSES,
    RETENTION_MS,
    Outcome,
    Pruned,
    Store,
    StoreError,
    read_job,
    read_jobs,
)

NOW = 1_000_000.0
CAP = 2
BURSTS = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=20)
INTAKE = Rule(name="intake", scenario="triage", dm=False, channels=frozenset(), senders=frozenset())
REVIEW = Rule(name="review", scenario="check", dm=False, channels=frozenset(), senders=frozenset())
SECRET_WORD = "zebraquartz"


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
    rule: Rule | None = INTAKE,
    live: bool = True,
    create_at: int = 1_000,
    now: float = NOW,
    cap: int = CAP,
    bursts: Bursts = BURSTS,
    place: str | None = "analytics",
    **fields: object,
) -> Outcome:
    post = make_post(id=post_id, create_at=create_at, **fields)
    return store.claim(
        post, kind="channel", rule=rule, place=place, live=live, now=now, cap=cap, bursts=bursts
    )


def query(path: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def handled(path: Path, post_id: str) -> bool:
    return bool(query(path, "SELECT 1 FROM handled WHERE post_id = ?", post_id))


def jobs(path: Path) -> list[tuple[object, ...]]:
    """Return (id, status, post_count) per job, oldest first."""
    return query(path, "SELECT id, status, post_count FROM jobs ORDER BY id")


def job_of(path: Path, post_id: str) -> object | None:
    rows = query(path, "SELECT job_id FROM job_posts WHERE post_id = ?", post_id)
    return rows[0][0] if rows else None


def raw_bytes(path: Path) -> bytes:
    return b"".join(file.read_bytes() for file in path.parent.iterdir() if file.is_file())


def test_an_unmatched_post_is_recorded_without_a_job(store: Store, path: Path) -> None:
    assert claim(store, "a", rule=None) == Outcome("unmatched")
    assert handled(path, "a")
    assert jobs(path) == []
    assert job_of(path, "a") is None


def test_a_matched_post_opens_a_job(store: Store, path: Path) -> None:
    outcome = claim(store, "a")
    assert outcome.claim == "opened"
    assert outcome.closed == ()
    job = outcome.job
    assert job is not None
    assert (job.id, job.status, job.post_count) == (1, "collecting", 1)
    assert (job.scenario, job.rule, job.place) == ("triage", "intake", "analytics")
    assert (job.opened_at, job.ready_at, job.deadline_at) == (NOW, NOW + 90, NOW + 600)
    assert jobs(path) == [(1, "collecting", 1)]
    assert job_of(path, "a") == 1


def test_the_job_keeps_the_posts_text_and_kind(store: Store, path: Path) -> None:
    claim(store, "a", message=SECRET_WORD)
    assert query(path, "SELECT kind, message FROM job_posts") == [("channel", SECRET_WORD)]


def test_a_duplicate_is_reported(store: Store) -> None:
    assert claim(store, "a").claim == "opened"
    assert claim(store, "a") == Outcome("duplicate")


def test_a_duplicate_neither_joins_nor_uses_the_cap(store: Store, path: Path) -> None:
    claim(store, "a")
    claim(store, "a")
    claim(store, "a")
    assert jobs(path) == [(1, "collecting", 1)]
    assert claim(store, "b", channel_id="other").claim == "opened"


def test_unmatched_posts_do_not_use_the_cap(store: Store) -> None:
    for number in range(5):
        assert claim(store, f"plain-{number}", rule=None).claim == "unmatched"
    assert claim(store, "a", channel_id="one").claim == "opened"
    assert claim(store, "b", channel_id="two").claim == "opened"


def test_posts_close_together_make_one_job(store: Store, path: Path) -> None:
    claim(store, "a", now=NOW)
    second = claim(store, "b", now=NOW + 30)
    third = claim(store, "c", now=NOW + 60)
    assert second.claim == third.claim == "joined"
    assert third.job is not None
    assert (third.job.id, third.job.status, third.job.post_count) == (1, "collecting", 3)
    assert jobs(path) == [(1, "collecting", 3)]
    assert [job_of(path, post_id) for post_id in "abc"] == [1, 1, 1]


def test_each_post_extends_the_quiet_time(store: Store) -> None:
    claim(store, "a", now=NOW)
    second = claim(store, "b", now=NOW + 50)
    assert second.job is not None
    assert second.job.ready_at == NOW + 50 + 90


def test_a_gap_longer_than_the_quiet_time_makes_two_jobs(store: Store, path: Path) -> None:
    claim(store, "a", now=NOW)
    outcome = claim(store, "b", now=NOW + 91)
    assert outcome.claim == "opened"
    assert outcome.job is not None
    assert outcome.job.id == 2
    # The first job was due, and the post made it ready without waiting for the housekeeping.
    assert [(job.id, job.status) for job in outcome.closed] == [(1, "ready")]
    assert jobs(path) == [(1, "ready", 1), (2, "collecting", 1)]


def test_the_quiet_time_never_passes_the_deadline(store: Store, path: Path) -> None:
    claim(store, "p0", now=NOW)
    for number in range(1, 8):
        late = claim(store, f"p{number}", now=NOW + 80 * number)
    assert late.claim == "joined"
    assert late.job is not None
    assert late.job.ready_at == NOW + 600
    after = claim(store, "after", now=NOW + 601)
    assert after.claim == "opened"
    assert jobs(path) == [(1, "ready", 8), (2, "collecting", 1)]


def test_the_job_is_ready_at_max_posts(store: Store, path: Path) -> None:
    small = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=2)
    claim(store, "a", bursts=small)
    full = claim(store, "b", bursts=small, now=NOW + 1)
    assert full.claim == "joined"
    assert full.job is not None
    assert full.job.status == "ready"
    assert full.closed == (full.job,)
    assert claim(store, "c", bursts=small, now=NOW + 2).job == read_jobs(path)[0]
    assert jobs(path) == [(1, "ready", 2), (2, "collecting", 1)]


def test_max_posts_of_one_makes_every_post_its_own_ready_job(store: Store, path: Path) -> None:
    single = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=1)
    outcome = claim(store, "a", bursts=single)
    assert outcome.claim == "opened"
    assert outcome.job is not None
    assert outcome.job.status == "ready"
    assert outcome.closed == (outcome.job,)
    claim(store, "b", bursts=single)
    assert jobs(path) == [(1, "ready", 1), (2, "ready", 1)]


def test_another_sender_thread_or_channel_opens_another_job(store: Store, path: Path) -> None:
    claim(store, "a", cap=10)
    assert claim(store, "b", cap=10, user_id="someone-else").claim == "opened"
    assert claim(store, "c", cap=10, root_id="thread-1").claim == "opened"
    assert claim(store, "d", cap=10, channel_id="channel-2").claim == "opened"
    assert jobs(path) == [(number, "collecting", 1) for number in range(1, 5)]


def test_a_reply_and_a_top_level_post_are_different_places(store: Store, path: Path) -> None:
    claim(store, "a", root_id="thread-1")
    claim(store, "b", root_id="thread-1")
    claim(store, "c")
    assert jobs(path) == [(1, "collecting", 2), (2, "collecting", 1)]


def test_another_rule_closes_the_job_and_opens_a_new_one(store: Store, path: Path) -> None:
    claim(store, "a", rule=INTAKE)
    outcome = claim(store, "b", rule=REVIEW, now=NOW + 1)
    assert outcome.claim == "opened"
    assert [(job.id, job.status, job.rule) for job in outcome.closed] == [(1, "ready", "intake")]
    assert jobs(path) == [(1, "ready", 1), (2, "collecting", 1)]
    assert query(path, "SELECT rule, scenario FROM jobs WHERE id = 2") == [("review", "check")]


def test_a_changed_scenario_closes_the_job_and_opens_a_new_one(store: Store, path: Path) -> None:
    # The rules file was edited between the two posts: same rule name, another scenario.
    claim(store, "a", rule=INTAKE)
    renamed = Rule(
        name="intake", scenario="other", dm=False, channels=frozenset(), senders=frozenset()
    )
    outcome = claim(store, "b", rule=renamed, now=NOW + 1)
    assert outcome.claim == "opened"
    assert [(job.id, job.status) for job in outcome.closed] == [(1, "ready")]
    assert query(path, "SELECT id, scenario FROM jobs ORDER BY id") == [(1, "triage"), (2, "other")]


def test_an_unmatched_post_never_joins_a_job(store: Store, path: Path) -> None:
    claim(store, "a")
    assert claim(store, "b", rule=None, now=NOW + 1).claim == "unmatched"
    assert jobs(path) == [(1, "collecting", 1)]
    assert job_of(path, "b") is None


def test_opening_counts_against_the_cap_and_joining_does_not(store: Store, path: Path) -> None:
    assert claim(store, "a", channel_id="one").claim == "opened"
    assert claim(store, "b", channel_id="one").claim == "joined"
    assert claim(store, "c", channel_id="two").claim == "opened"
    assert claim(store, "d", channel_id="three") == Outcome("cap_hit")
    assert claim(store, "e", channel_id="one").claim == "joined"
    assert jobs(path) == [(1, "collecting", 3), (2, "collecting", 1)]
    assert handled(path, "d")
    assert job_of(path, "d") is None


def test_a_post_over_the_cap_is_not_retried(store: Store) -> None:
    claim(store, "a", channel_id="one")
    claim(store, "b", channel_id="two")
    assert claim(store, "c", channel_id="three").claim == "cap_hit"
    later = NOW + CAP_WINDOW_SECONDS + 1
    assert claim(store, "c", channel_id="three", now=later).claim == "duplicate"


def test_a_post_over_the_cap_does_not_close_another_rules_job(store: Store, path: Path) -> None:
    claim(store, "a", rule=INTAKE, channel_id="one")
    claim(store, "b", channel_id="two")
    assert claim(store, "c", rule=REVIEW, channel_id="one", now=NOW + 1) == Outcome("cap_hit")
    assert jobs(path) == [(1, "collecting", 1), (2, "collecting", 1)]


def test_catch_up_counts_against_the_cap(store: Store) -> None:
    assert claim(store, "a", live=False, channel_id="one").claim == "opened"
    assert claim(store, "b", live=False, channel_id="two").claim == "opened"
    assert claim(store, "c", live=False, channel_id="three").claim == "cap_hit"


def test_the_hour_slides(store: Store) -> None:
    claim(store, "a", channel_id="one", now=NOW)
    claim(store, "b", channel_id="two", now=NOW + 1000)
    assert claim(store, "c", channel_id="c", now=NOW + CAP_WINDOW_SECONDS - 1).claim == "cap_hit"
    assert claim(store, "d", channel_id="d", now=NOW + CAP_WINDOW_SECONDS).claim == "opened"
    assert claim(store, "e", channel_id="e", now=NOW + CAP_WINDOW_SECONDS + 1).claim == "cap_hit"


def test_the_cap_survives_a_restart(store: Store, path: Path) -> None:
    claim(store, "a", channel_id="one")
    claim(store, "b", channel_id="two")
    assert claim(Store(path), "c", channel_id="three").claim == "cap_hit"


def test_handled_posts_and_jobs_survive_a_restart(store: Store, path: Path) -> None:
    claim(store, "a")
    reopened = Store(path)
    assert claim(reopened, "a").claim == "duplicate"
    assert claim(reopened, "b").claim == "joined"


def test_flip_ready_makes_only_due_jobs_ready(store: Store, path: Path) -> None:
    claim(store, "a", channel_id="one", now=NOW)
    claim(store, "b", channel_id="two", now=NOW + 10)
    assert store.flip_ready(NOW + 89) == []
    flipped = store.flip_ready(NOW + 90)
    assert [(job.id, job.status, job.post_count) for job in flipped] == [(1, "ready", 1)]
    assert jobs(path) == [(1, "ready", 1), (2, "collecting", 1)]
    assert store.flip_ready(NOW + 90) == []


def test_a_failure_inside_claim_leaves_nothing_behind(store: Store, path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON job_posts BEGIN SELECT RAISE(ABORT, 'no'); END"
        )
    finally:
        connection.close()
    with pytest.raises(StoreError, match="cannot use the database"):
        claim(store, "a")
    assert not handled(path, "a")
    assert jobs(path) == []
    assert store.last_seen() is None


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
    assert claim(store, "a", live=True, create_at=500).claim == "duplicate"
    assert store.last_seen() == 500


def test_an_unmatched_live_post_moves_last_seen(store: Store) -> None:
    claim(store, "a", rule=None, create_at=500)
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
    claim(store, "old", rule=None, create_at=999)
    claim(store, "edge", rule=None, create_at=1_000)
    claim(store, "new", rule=None, create_at=latest)
    assert store.prune(NOW) == Pruned(handled=1, jobs=0)
    assert not handled(path, "old")
    assert handled(path, "edge")
    assert handled(path, "new")


def test_prune_does_nothing_on_the_first_run(store: Store, path: Path) -> None:
    claim(store, "a", live=False, rule=None, create_at=1)
    assert store.prune(NOW) == Pruned(0, 0)
    assert handled(path, "a")


def test_prune_is_measured_from_last_seen_and_not_from_today(store: Store, path: Path) -> None:
    claim(store, "a", rule=None, create_at=1_000)
    assert store.prune(NOW + 10 * RETENTION_MS).handled == 0
    assert handled(path, "a")


def test_prune_forgets_jobs_after_seven_days_whatever_their_status(
    store: Store, path: Path
) -> None:
    claim(store, "a", channel_id="one", now=NOW)
    claim(store, "b", channel_id="two", now=NOW + 1)
    store.flip_ready(NOW + 100)
    assert store.prune(NOW + JOB_RETENTION_SECONDS).jobs == 0
    assert store.prune(NOW + JOB_RETENTION_SECONDS + 1).jobs == 1
    assert jobs(path) == [(2, "ready", 1)]
    assert job_of(path, "a") is None
    assert job_of(path, "b") == 2


def test_a_pruned_message_is_gone_from_the_files(store: Store, path: Path) -> None:
    claim(store, "a", message=f"please check {SECRET_WORD} today")
    claim(store, "b", rule=None, message=SECRET_WORD, now=NOW + 1)
    assert raw_bytes(path).count(SECRET_WORD.encode()) == 1
    store.prune(NOW + JOB_RETENTION_SECONDS + 1)
    assert raw_bytes(path).count(SECRET_WORD.encode()) == 0


def test_an_unmatched_posts_text_never_reaches_the_file(store: Store, path: Path) -> None:
    claim(store, "a", rule=None, message=SECRET_WORD)
    assert SECRET_WORD.encode() not in raw_bytes(path)


def test_files_are_private(store: Store, path: Path) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_the_wal_files_are_private_too(store: Store, path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        side_files = [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm")]
        assert all(file.exists() for file in side_files)
        assert all(stat.S_IMODE(file.stat().st_mode) == 0o600 for file in side_files)
    finally:
        connection.close()


def test_text_can_live_only_in_job_posts_message(store: Store, path: Path) -> None:
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
        "handled": {"post_id", "create_at"},
        "last_seen": {"id", "create_at"},
        "jobs": {
            "id",
            "conversation_key",
            "channel_id",
            "root_id",
            "sender_id",
            "place",
            "rule",
            "scenario",
            "status",
            "opened_at",
            "ready_at",
            "deadline_at",
            "updated_at",
            "post_count",
        },
        "job_posts": {"post_id", "job_id", "create_at", "kind", "message"},
    }


def test_every_planned_status_is_allowed_and_nothing_else(store: Store, path: Path) -> None:
    claim(store, "a")
    connection = sqlite3.connect(path)
    try:
        for status in JOB_STATUSES:
            connection.execute("UPDATE jobs SET status = ? WHERE id = 1", (status,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE jobs SET status = 'unknown' WHERE id = 1")
    finally:
        connection.close()


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


def test_jobs_are_read_newest_first(store: Store, path: Path) -> None:
    claim(store, "a", channel_id="one")
    claim(store, "b", channel_id="two", now=NOW + 1)
    assert [job.id for job in read_jobs(path)] == [2, 1]
    assert read_jobs(path)[1].post_count == 1


def test_a_job_is_read_with_its_posts_in_order(store: Store, path: Path) -> None:
    claim(store, "late", create_at=3_000, message="three", now=NOW)
    claim(store, "early", create_at=2_000, message="two", now=NOW + 1)
    found = read_job(path, 1)
    assert found is not None
    job, posts = found
    assert job.post_count == 2
    assert [(post.post_id, post.kind, post.message) for post in posts] == [
        ("early", "channel", "two"),
        ("late", "channel", "three"),
    ]
    assert "two" not in repr(posts[0])


def test_an_unknown_job_reads_as_none(store: Store, path: Path) -> None:
    assert read_job(path, 7) is None


def test_jobs_are_read_while_the_listener_holds_the_write_lock(store: Store, path: Path) -> None:
    claim(store, "a")
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert [job.id for job in read_jobs(path)] == [1]
    finally:
        writer.close()


def test_jobs_are_read_without_the_wal_files(store: Store, path: Path) -> None:
    claim(store, "a")
    assert not path.with_name(path.name + "-wal").exists()
    assert [job.id for job in read_jobs(path)] == [1]


def test_reading_a_missing_database_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="cannot use the database"):
        read_jobs(tmp_path / FILE_NAME)


def test_reading_a_database_of_another_version_fails_clearly(store: Store, path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 99")
    finally:
        connection.close()
    with pytest.raises(StoreError, match="unknown schema version 99"):
        read_jobs(path)

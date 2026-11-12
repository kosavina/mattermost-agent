"""The jobs command, against a database the store wrote."""

import io
import sys
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from helpers import captured_logs, make_post

from mattermost_agent import __main__ as entry
from mattermost_agent import jobs
from mattermost_agent.rules import Bursts, Rule
from mattermost_agent.store import FILE_NAME, Store

NOW = 2_000_000_000.0
SECRET_TEXT = "sentinel-private-message"
BURSTS = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=20)
RULE = Rule(
    name="dm-from-anyone", scenario="intake", dm=True, channels=frozenset(), senders=frozenset()
)
JOB_1 = ["1", "collecting", "intake", "dm-from-anyone", "@example.user", "2"]
JOB_2 = ["2", "collecting", "intake", "dm-from-anyone", "@example.user", "1"]

type Run = Callable[..., tuple[int | str | None, str]]


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def store(state_dir: Path) -> Store:
    return Store(state_dir / FILE_NAME)


def add(store: Store, post_id: str, message: str, *, now: float, **fields: object) -> None:
    post = make_post(id=post_id, create_at=int(now * 1000), message=message, **fields)
    store.claim(
        post,
        kind="dm",
        rule=RULE,
        place="@example.user",
        live=True,
        now=now,
        cap=20,
        bursts=BURSTS,
    )


@pytest.fixture
def two_jobs(store: Store) -> Store:
    add(store, "a", f"first {SECRET_TEXT}", now=NOW)
    add(store, "b", "second\nline two", now=NOW + 1)
    add(store, "c", "elsewhere", now=NOW + 2, channel_id="channel-2")
    return store


@pytest.fixture
def run(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Run:
    """Run main() with the given arguments, and return the exit code and what it printed."""

    def run(*args: str) -> tuple[int | str | None, str]:
        monkeypatch.setattr(sys, "argv", ["mattermost-agent", *args])
        monkeypatch.setenv("STATE_DIR", str(state_dir))
        for variable in ("MM_URL", "MM_TOKEN", "MM_TOKEN_FILE", "RULES_FILE"):
            monkeypatch.delenv(variable, raising=False)
        with pytest.raises(SystemExit) as stopped:
            entry.main()
        return stopped.value.code, capsys.readouterr().out

    return run


@pytest.fixture
def log() -> Generator[io.StringIO]:
    with captured_logs() as stream:
        yield stream


def test_the_listing_shows_jobs_newest_first_without_text(two_jobs: Store, run: Run) -> None:
    code, out = run("jobs")
    assert code == 0
    header, first, second = out.splitlines()
    columns = ["id", "status", "scenario", "rule", "place", "posts", "opened", "ready"]
    columns += ["backend", "decision", "reply", "delivery", "fail_reason"]
    assert header.split() == columns
    assert first.split()[:6] == JOB_2
    assert second.split()[:6] == JOB_1
    assert SECRET_TEXT not in out
    assert "elsewhere" not in out


def test_one_job_shows_its_posts_in_order_with_their_text(two_jobs: Store, run: Run) -> None:
    code, out = run("jobs", "1")
    assert code == 0
    assert out.splitlines()[1].split()[:6] == JOB_1
    assert f"kind=dm\nfirst {SECRET_TEXT}\n" in out
    assert "kind=dm\nsecond\nline two\n" in out
    assert out.index(SECRET_TEXT) < out.index("second")
    assert "elsewhere" not in out


def test_nothing_reaches_the_log(two_jobs: Store, run: Run, log: io.StringIO) -> None:
    run("jobs", "1")
    run("jobs")
    assert log.getvalue() == ""


def test_an_empty_queue_says_so(store: Store, run: Run) -> None:
    assert run("jobs") == (0, "no jobs yet\n")


def test_an_unknown_job_is_named(two_jobs: Store, run: Run) -> None:
    assert run("jobs", "7") == ("mattermost-agent: no job 7", "")


def test_a_missing_database_is_named(run: Run) -> None:
    code, _ = run("jobs")
    assert isinstance(code, str)
    assert code.startswith("mattermost-agent: cannot use the database")


@pytest.mark.parametrize(
    "arguments", [["jobs", "x"], ["jobs", "-1"], ["jobs", "1", "2"], ["job"], ["outbox", "1"]]
)
def test_other_arguments_show_the_usage(run: Run, arguments: list[str]) -> None:
    code, _ = run(*arguments)
    assert str(code).startswith("usage: mattermost-agent [healthcheck | jobs [id] | outbox |")


def test_control_characters_cannot_reach_the_terminal(store: Store, run: Run) -> None:
    add(store, "a", "title\x1b]0;evil\x07 tab\there\r\nnext \x85", now=NOW)
    _, out = run("jobs", "1")
    for forbidden in ("\x1b", "\x07", "\r", "\x85"):
        assert forbidden not in out
    assert "title\\x1b]0;evil\\x07 tab\there\\x0d\nnext \\x85\n" in out


def test_a_post_time_out_of_range_is_shown_as_a_number(store: Store, run: Run) -> None:
    post = make_post(id="a", create_at=10**17, message="far future")
    store.claim(post, kind="dm", rule=RULE, place=None, live=True, now=NOW, cap=20, bursts=BURSTS)
    code, out = run("jobs", "1")
    assert code == 0
    assert f"--- {10**14} kind=dm\nfar future\n" in out


def test_safe_text_keeps_ordinary_text() -> None:
    text = "Привет, @example.user!\n\tsee https://example.com/x?y=1"
    assert jobs.safe_text(text) == text


def queue(store: Store, post_id: str, text: str, *, now: float, refused: bool = False) -> int:
    """Open a one-post job, take it, and queue its reply as the processor would."""
    post = make_post(id=post_id, channel_id=f"chat-{post_id}", create_at=int(now * 1000))
    one_post = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=1)
    store.claim(
        post,
        kind="dm",
        rule=RULE,
        place="@example.user",
        live=True,
        now=now,
        cap=20,
        bursts=one_post,
    )
    job = store.take_ready(now)
    assert job is not None
    store.finish_with_reply(
        job.id,
        mode="draft",
        root_id="",
        message=text,
        downgrade="link" if not refused else None,
        refused=refused,
        backend="codex",
        decision="act",
        now=now,
    )
    return job.id


def test_the_outbox_listing_shows_replies_newest_first_without_text(
    store: Store, run: Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = queue(store, "a", f"reply {SECRET_TEXT}", now=NOW)
    second = queue(store, "b", "/away", now=NOW + 60, refused=True)
    monkeypatch.setattr(jobs.time, "time", lambda: NOW + 3700)
    code, out = run("outbox")
    assert code == 0
    header, newer, older = out.splitlines()
    assert header.split() == [
        "job",
        "mode",
        "place",
        "state",
        "outcome",
        "downgrade",
        "reason",
        "age",
    ]
    assert newer.split() == [
        str(second),
        "draft",
        "@example.user",
        "done",
        "refused_command",
        "-",
        "-",
        "1h",
        "00m",
    ]
    assert older.split() == [
        str(first),
        "draft",
        "@example.user",
        "pending",
        "-",
        "link",
        "-",
        "1h",
        "01m",
    ]
    assert SECRET_TEXT not in out and "/away" not in out


def test_the_jobs_listing_shows_how_a_reply_is_delivered(store: Store, run: Run) -> None:
    queue(store, "a", "reply", now=NOW)
    queue(store, "b", "/away", now=NOW + 1, refused=True)
    _, out = run("jobs")
    _, newer, older = out.splitlines()
    assert newer.split()[-5:] == ["codex", "act", "draft", "refused_command", "-"]
    assert older.split()[-5:] == ["codex", "act", "draft", "queued", "-"]


def test_an_empty_outbox_says_so(store: Store, run: Run) -> None:
    assert run("outbox") == (0, "no replies yet\n")


def test_the_outbox_listing_never_reaches_the_log(store: Store, run: Run, log: io.StringIO) -> None:
    queue(store, "a", f"reply {SECRET_TEXT}", now=NOW)
    run("outbox")
    assert log.getvalue() == ""

"""locked_run with a fake agent: a Python script that misbehaves on request."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mattermost_agent.agents import AgentError
from mattermost_agent.agents.locked_run import clear_stale_tmp, locked_run, run_folder, tmp_root

STDERR_SECRET = "stderr-canary-ZEBRAQUARTZ"
FAKE = f'''
import os, subprocess, sys, time
mode = sys.argv[1]
sys.stderr.write("{STDERR_SECRET}\\n")
if mode == "echo":
    sys.stdout.write("got:" + sys.stdin.read())
elif mode == "fail":
    sys.stdout.write("partial output")
    sys.exit(3)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(sys.argv[2], "w").write(str(child.pid))
    sys.stdout.write("x")
    sys.stdout.flush()
    time.sleep(60)
elif mode == "env":
    sys.stdout.write(",".join(sorted(os.environ)))
'''


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE)
    return script


def argv(fake: Path, *rest: str) -> list[str]:
    return [sys.executable, str(fake), *rest]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers; ask the kernel for its state.
    state = subprocess.run(  # noqa: S603 - a fixed program and a number
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    )
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


def test_stdin_goes_in_and_stdout_comes_out(fake: Path, tmp_path: Path) -> None:
    stdout, duration = locked_run(argv(fake, "echo"), {}, cwd=tmp_path, stdin="hi", timeout_s=30)
    assert stdout == b"got:hi"
    assert 0 <= duration < 30


def test_only_the_given_env_reaches_the_agent(fake: Path, tmp_path: Path) -> None:
    stdout, _ = locked_run(argv(fake, "env"), {"HOME": "/x"}, cwd=tmp_path, stdin="", timeout_s=30)
    # Two variables are not ours: Python adds LC_CTYPE when it coerces the C locale, and macOS
    # gives every process __CF_USER_TEXT_ENCODING.
    seen = set(stdout.decode().split(",")) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert seen == {"HOME"}


def test_a_failure_raises_without_any_output(fake: Path, tmp_path: Path) -> None:
    with pytest.raises(AgentError) as info:
        locked_run(argv(fake, "fail"), {}, cwd=tmp_path, stdin="", timeout_s=30)
    assert (info.value.reason, info.value.exit_code) == ("exit", 3)
    assert "partial" not in str(info.value)
    assert STDERR_SECRET not in str(info.value)
    assert STDERR_SECRET not in repr(info.value)


def test_a_timeout_kills_the_whole_group(fake: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    with pytest.raises(AgentError) as info:
        locked_run(argv(fake, "hang", str(pid_file)), {}, cwd=tmp_path, stdin="", timeout_s=2)
    assert info.value.reason == "timeout"
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)


def test_a_missing_program_is_a_startup_denial(tmp_path: Path) -> None:
    with pytest.raises(AgentError) as info:
        locked_run([str(tmp_path / "nope")], {}, cwd=tmp_path, stdin="", timeout_s=5)
    assert info.value.reason == "denied_startup"


def test_stderr_never_reaches_the_terminal(
    fake: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    locked_run(argv(fake, "echo"), {}, cwd=tmp_path, stdin="", timeout_s=30)
    out, err = capfd.readouterr()
    assert STDERR_SECRET not in out + err


def test_run_folder_is_private_and_removed(tmp_path: Path) -> None:
    with run_folder(tmp_path) as run_dir:
        assert run_dir.parent == tmp_root(tmp_path)
        assert run_dir.is_dir()
        assert (run_dir.stat().st_mode & 0o777) == 0o700
        assert (run_dir.parent.stat().st_mode & 0o777) == 0o700
        (run_dir / "out.json").write_text("{}")
        kept = run_dir
    assert not kept.exists()


def test_run_folder_is_removed_after_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), run_folder(tmp_path) as run_dir:
        kept = run_dir
        raise RuntimeError
    assert not kept.exists()


def test_stale_run_folders_are_cleared(tmp_path: Path) -> None:
    root = tmp_root(tmp_path)
    root.mkdir(parents=True)
    (root / "run-old").mkdir()
    (root / "run-old" / "out.json").write_text("{}")
    (root / "stray").write_text("x")
    assert clear_stale_tmp(tmp_path) == 2
    assert list(root.iterdir()) == []
    assert clear_stale_tmp(tmp_path) == 0


def test_a_group_kill_the_system_refuses_still_kills_the_leader(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS answers EPERM to killpg when one member cannot be signalled; the run must still end."""

    def refusing(pgid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "killpg", refusing)
    pid_file = tmp_path / "grandchild.pid"
    with pytest.raises(AgentError) as info:
        locked_run(argv(fake, "hang", str(pid_file)), {}, cwd=tmp_path, stdin="", timeout_s=2)
    assert info.value.reason == "timeout"


def test_kill_running_kills_the_run_in_progress(fake: Path, tmp_path: Path) -> None:
    import threading

    from mattermost_agent.agents.locked_run import kill_running

    assert kill_running() is False
    pid_file = tmp_path / "grandchild.pid"
    errors: list[AgentError] = []

    def run() -> None:
        try:
            locked_run(argv(fake, "hang", str(pid_file)), {}, cwd=tmp_path, stdin="", timeout_s=30)
        except AgentError as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert kill_running() is True
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert [error.reason for error in errors] == ["exit"]
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(grandchild)

"""Run an agent process under our control: explicit env, own process group, no stderr.

Shared by every backend. The process gets only the env it is given, runs in its own session so
that a timeout or Ctrl+C kills it with every child, reads the prompt on stdin, and has its stderr
sent to /dev/null: Codex echoes the prompt there, and nothing from it may reach a log. Each run
gets a private temp folder under STATE_DIR/tmp (mode 700) for the schema and the output file.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from mattermost_agent.agents.port import AgentError

TMP_DIR_NAME = "tmp"
# Exit codes of a shell that could not run the program: not found, not executable.
STARTUP_EXIT_CODES = frozenset({126, 127})


def tmp_root(state_dir: Path) -> Path:
    return state_dir / TMP_DIR_NAME


def clear_stale_tmp(state_dir: Path) -> int:
    """Remove the run folders a crashed processor left behind. Return how many."""
    root = tmp_root(state_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    count = 0
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
            count += 1
        else:
            entry.unlink()
            count += 1
    return count


@contextmanager
def run_folder(state_dir: Path) -> Generator[Path]:
    """Yield a fresh private folder for one run, and remove it afterwards whatever happens."""
    root = tmp_root(state_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdtemp creates the folder with mode 700.
    path = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the process and everything it started. start_new_session made it the group leader."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired, OSError, ValueError):
        process.communicate(timeout=5)


def locked_run(
    argv: list[str],
    env: Mapping[str, str],
    *,
    cwd: Path,
    stdin: str,
    timeout_s: float,
) -> tuple[bytes, float]:
    """Run `argv` with exactly `env`, feed `stdin`, and return (stdout, seconds taken).

    Raises AgentError: `denied_startup` if the program cannot start, `timeout` after killing the
    whole process group, `exit` on a non-zero exit code. No exception carries the agent's output.
    """
    started = time.monotonic()
    try:
        # argv is built by our code from checked settings and folders, never from a message.
        process = subprocess.Popen(  # noqa: S603
            argv,
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as error:
        raise AgentError("denied_startup") from error
    try:
        stdout, _ = process.communicate(stdin.encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        raise AgentError("timeout") from None
    except BaseException:
        # Ctrl+C, SystemExit, or anything else: the agent must not outlive us.
        _kill_group(process)
        raise
    duration = time.monotonic() - started
    if process.returncode != 0:
        if process.returncode in STARTUP_EXIT_CODES:
            raise AgentError("denied_startup", process.returncode)
        raise AgentError("exit", process.returncode)
    return stdout, duration

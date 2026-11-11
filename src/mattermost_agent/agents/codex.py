"""Codex as a backend: `codex exec` under a permission profile our code writes for every run.

The profile is passed as `-c` overrides with `--ignore-user-config`, so no config file can drift
from what the tests check. Reads are allowed in the scenario folder (the working directory) and
in each read dir, by absolute path; the temp folders are denied; the network is off. Never pass
`--sandbox`: it switches the profile off. Codex re-executes its own binary inside the sandbox, so
the install folder and the binary's folder must be readable.
"""

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from mattermost_agent.agents.locked_run import locked_run, run_folder
from mattermost_agent.agents.port import AgentError, AgentResult, AgentTask, BackendCheckError
from mattermost_agent.agents.schema import OUTPUT_SCHEMA

PROFILE = "scenario"
SYSTEM_PATH = "/usr/bin:/bin"
CHECK_TIMEOUT_S = 30.0


def toml_string(value: str) -> str:
    """Quote `value` as a TOML basic string, so a path cannot break out of the profile."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        f"\\u{ord(char):04X}" if ord(char) < 0x20 or char == "\x7f" else char for char in escaped
    )
    return f'"{escaped}"'


def _table(entries: list[tuple[str, str]]) -> str:
    """Return an inline TOML table. Values are already TOML."""
    return "{" + ",".join(f"{toml_string(key)}={value}" for key, value in entries) + "}"


@dataclass(frozen=True)
class CodexBackend:
    """Flags and folders come from the settings; nothing here reads the environment."""

    binary: Path
    codex_home: Path
    install_dir: Path
    home: str
    user: str
    proxies: Mapping[str, str]
    state_dir: Path

    name: ClassVar[str] = "codex"
    version: ClassVar[str] = "codex-cli 0.167.0"

    def _env(self) -> dict[str, str]:
        return {
            "HOME": self.home,
            "USER": self.user,
            "PATH": SYSTEM_PATH,
            "CODEX_HOME": str(self.codex_home),
            **dict(self.proxies),
        }

    def filesystem_profile(self, task: AgentTask) -> str:
        """Return the profile's filesystem table: what the sandbox may read, and no writes."""
        read_dirs = list(dict.fromkeys(task.read_dirs))
        entries: list[tuple[str, str]] = [
            (":minimal", '"read"'),
            (":tmpdir", '"deny"'),
            (":slash_tmp", '"deny"'),
            (str(self.install_dir), '"read"'),
            (str(self.binary.parent), '"read"'),
            *((str(path), '"read"') for path in read_dirs),
            (":workspace_roots", _table([(".", '"read"')])),
        ]
        return _table(entries)

    def command(self, task: AgentTask, run_dir: Path) -> tuple[list[str], dict[str, str]]:
        argv = [
            str(self.binary),
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ignore-user-config",
            "-c",
            f'default_permissions="{PROFILE}"',
            "-c",
            f"permissions.{PROFILE}.filesystem={self.filesystem_profile(task)}",
            "-c",
            f"permissions.{PROFILE}.network.enabled=false",
            "--output-schema",
            str(run_dir / "schema.json"),
            "-C",
            str(task.scenario_dir),
            "-o",
            str(run_dir / "out.json"),
            "-",
        ]
        return argv, self._env()

    def run(self, task: AgentTask) -> AgentResult:
        with run_folder(self.state_dir) as run_dir:
            (run_dir / "schema.json").write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
            argv, env = self.command(task, run_dir)
            _, duration = locked_run(
                argv, env, cwd=task.scenario_dir, stdin=task.prompt, timeout_s=task.timeout_s
            )
            try:
                output = json.loads((run_dir / "out.json").read_text(encoding="utf-8"))
            except OSError, UnicodeDecodeError, ValueError:
                raise AgentError("invalid_json") from None
        if not isinstance(output, dict):
            raise AgentError("invalid_json")
        return AgentResult(output=output, backend=self.name, duration_s=duration)

    def check(self) -> None:
        if not (self.binary.is_file() and self.install_dir.is_dir()):
            raise BackendCheckError("codex: CODEX_BIN or CODEX_INSTALL_DIR does not exist")
        if not (self.codex_home / "auth.json").is_file():
            raise BackendCheckError("codex: no auth.json in CODEX_HOME; sign in there first")
        try:
            # Our binary path and a fixed flag; nothing from outside.
            result = subprocess.run(  # noqa: S603
                [str(self.binary), "--version"],
                env=self._env(),
                capture_output=True,
                timeout=CHECK_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendCheckError("codex: cannot run CODEX_BIN --version") from error
        if result.returncode != 0 or result.stdout.decode(errors="replace").strip() != self.version:
            raise BackendCheckError(f"codex: the binary is not {self.version}, the tested version")

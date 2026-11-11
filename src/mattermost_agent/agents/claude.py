"""Claude Code as a backend: `claude -p` with the file tools only, confined to the working
directory and the read dirs.

`--restricted` and `--setting-sources ""` keep Claude from loading settings, plugins, hooks and
any CLAUDE.md or AGENTS.md on disk, so the scenario's AGENTS.md goes in with
`--append-system-prompt-file`.
Bash, Write and WebFetch do not exist in the run. Refused tool calls come back in the JSON, as
`permission_denials`; their number is logged, never their content.
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

TOOLS = "Read,Glob,Grep"
INSTRUCTIONS_FILE = "AGENTS.md"
SYSTEM_PATH = "/usr/bin:/bin"
CHECK_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class ClaudeBackend:
    """Flags and folders come from the settings; nothing here reads the environment."""

    binary: Path
    home: str
    user: str
    proxies: Mapping[str, str]
    state_dir: Path

    name: ClassVar[str] = "claude"
    version: ClassVar[str] = "2.1.311"

    def _env(self) -> dict[str, str]:
        # USER is needed on macOS, or Claude cannot find its Keychain login.
        return {"HOME": self.home, "USER": self.user, "PATH": SYSTEM_PATH, **dict(self.proxies)}

    def command(self, task: AgentTask, run_dir: Path) -> tuple[list[str], dict[str, str]]:
        argv = [
            str(self.binary),
            "-p",
            "--tools",
            TOOLS,
            "--restricted",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
        ]
        for path in dict.fromkeys(task.read_dirs):
            argv += ["--add-dir", str(path)]
        argv += [
            # The file, not the text: argv is visible to every local user, and has a size limit.
            "--append-system-prompt-file",
            str(task.scenario_dir / INSTRUCTIONS_FILE),
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(OUTPUT_SCHEMA),
        ]
        return argv, self._env()

    def run(self, task: AgentTask) -> AgentResult:
        with run_folder(self.state_dir) as run_dir:
            argv, env = self.command(task, run_dir)
            stdout, duration = locked_run(
                argv, env, cwd=task.scenario_dir, stdin=task.prompt, timeout_s=task.timeout_s
            )
        try:
            report = json.loads(stdout.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            raise AgentError("invalid_json") from None
        if not isinstance(report, dict) or report.get("is_error") is not False:
            raise AgentError("invalid_json")
        output = report.get("structured_output")
        if not isinstance(output, dict):
            raise AgentError("invalid_json")
        denials = report.get("permission_denials")
        return AgentResult(
            output=output,
            backend=self.name,
            duration_s=duration,
            denials=len(denials) if isinstance(denials, list) else 0,
        )

    def _probe(self, *flags: str) -> bytes:
        try:
            # Our binary path and fixed flags; nothing from outside.
            result = subprocess.run(  # noqa: S603
                [str(self.binary), *flags],
                env=self._env(),
                capture_output=True,
                timeout=CHECK_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BackendCheckError(f"claude: cannot run CLAUDE_BIN {flags[0]}") from error
        if result.returncode != 0:
            raise BackendCheckError(f"claude: CLAUDE_BIN {flags[0]} failed")
        return result.stdout

    def check(self) -> None:
        if not self.binary.is_file():
            raise BackendCheckError("claude: CLAUDE_BIN does not exist")
        reported = self._probe("--version").decode(errors="replace").split()
        if not reported or reported[0] != self.version:
            raise BackendCheckError(f"claude: the binary is not {self.version}, the tested version")
        try:
            status = json.loads(self._probe("auth", "status"))
        except ValueError:
            raise BackendCheckError("claude: auth status did not return JSON") from None
        if not (isinstance(status, dict) and status.get("loggedIn") is True):
            raise BackendCheckError("claude: not logged in; run claude /login once")

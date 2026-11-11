"""The agent interface: what the processor hands to a backend, and what it gets back.

A backend is a locked-down command-line agent (Codex or Claude Code). Our code decides every
flag, folder and permission; nothing in a message or in the agent's answer can change them.
`command()` is pure, so tests can check the exact argv and env; `run()` executes it.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

type Reason = Literal["exit", "timeout", "invalid_json", "invalid_output", "denied_startup"]


@dataclass(frozen=True)
class AgentTask:
    """One agent run. repr() leaves out the texts."""

    scenario_dir: Path
    """The agent's working directory: our instructions, trusted, read-only. Resolved and checked."""
    read_dirs: tuple[Path, ...]
    """The conversation folder and the attached projects. Resolved and checked, read-only."""
    instructions: str = field(repr=False)
    """The scenario's AGENTS.md text: ours, trusted."""
    prompt: str = field(repr=False)
    """Our fixed task text plus the data block with the burst and the memory."""
    timeout_s: float = 120.0


@dataclass(frozen=True)
class AgentResult:
    """The parsed answer, before validation. Never the agent's stderr or a transcript."""

    output: dict[str, object] = field(repr=False)
    backend: str = ""
    duration_s: float = 0.0
    denials: int = 0
    """How many tool calls the backend refused (Claude reports them; Codex cannot)."""


class AgentError(Exception):
    """The agent run failed. The message is the reason only; no output of the agent is in it."""

    def __init__(self, reason: Reason, exit_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason: Reason = reason
        self.exit_code = exit_code


class BackendCheckError(Exception):
    """A backend cannot be used: binary, version or login. The message names what, not values."""


class AgentBackend(Protocol):
    """One agent program behind the processor."""

    @property
    def name(self) -> str: ...

    def command(self, task: AgentTask, run_dir: Path) -> tuple[list[str], dict[str, str]]:
        """Return the exact argv and env for `task`. `run_dir` is this run's private temp folder."""
        ...

    def run(self, task: AgentTask) -> AgentResult:
        """Run the agent once and return its parsed output, or raise AgentError."""
        ...

    def check(self) -> None:
        """Raise BackendCheckError unless the binary, its version and a login are in place."""
        ...

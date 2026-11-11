"""The processor's settings, read from environment variables and checked once at startup.

Environment variables:

- STATE_DIR: the listener's state folder; checkpoints.sqlite3 and tmp/ are added. Required.
- AGENT_ROOT: the agent root (~/agent), with scenarios/, projects/ and conversations/. Required.
- RULES_FILE: the listener's rules.yaml. Required.
- AGENT_BACKEND: codex or claude, the backend for scenarios that name none. Default codex.
- CODEX_BIN, CODEX_HOME, CODEX_INSTALL_DIR, CLAUDE_BIN: where the agents are. Defaults below.
- AGENT_TIMEOUT_SECONDS: one agent run. Default 120. POLL_SECONDS: the queue poll. Default 3.
- LOG_LEVEL: as the listener's.
- HOME, USER, HTTPS_PROXY, HTTP_PROXY, NO_PROXY: passed on to the agents, nothing else is.

Startup refuses to run with any MM_* variable set (the processor must never hold the token), with
any folder on the shared volume (/Volumes), with a root that does not resolve inside AGENT_ROOT,
or with the state folder or the Codex home inside a folder the agents may read. Error messages
name the variable, never its value.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mattermost_agent.agents import BACKENDS, AgentBackend, ClaudeBackend, CodexBackend
from mattermost_agent.settings import SettingsError, _check_log_level

DEFAULTS = {
    "AGENT_BACKEND": "codex",
    "CODEX_BIN": "/opt/homebrew/bin/codex",
    "CODEX_INSTALL_DIR": "/opt/homebrew/Caskroom/codex",
    "CLAUDE_BIN": "/opt/homebrew/bin/claude",
    "AGENT_TIMEOUT_SECONDS": "120",
    "POLL_SECONDS": "3",
}
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
SHARED_VOLUMES = Path("/Volumes")
LISTENER_PREFIX = "MM_"
NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")


@dataclass(frozen=True)
class Roots:
    """The three folders the agents may read from, resolved. Never named in config."""

    scenarios: Path
    projects: Path
    conversations: Path

    def __iter__(self):
        return iter((self.scenarios, self.projects, self.conversations))


@dataclass(frozen=True)
class ProcessorSettings:
    state_dir: Path
    agent_root: Path
    roots: Roots
    rules_file: Path
    scenarios_file: Path
    backend: str
    codex_bin: Path
    codex_home: Path
    codex_install_dir: Path
    claude_bin: Path
    timeout_s: float
    poll_s: float
    log_level: str
    home: str
    user: str
    proxies: dict[str, str]

    def backends(self) -> dict[str, AgentBackend]:
        """Build every known backend from these settings. check() is the caller's job."""
        return {
            "codex": CodexBackend(
                binary=self.codex_bin,
                codex_home=self.codex_home,
                install_dir=self.codex_install_dir,
                home=self.home,
                user=self.user,
                proxies=self.proxies,
                state_dir=self.state_dir,
            ),
            "claude": ClaudeBackend(
                binary=self.claude_bin,
                home=self.home,
                user=self.user,
                proxies=self.proxies,
                state_dir=self.state_dir,
            ),
        }


def inside(path: Path, root: Path) -> bool:
    """Say whether `path` is `root` or below it. Both must be resolved."""
    return path == root or root in path.parents


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise SettingsError(f"set {name}")
    return value


def _setting(env: Mapping[str, str], name: str) -> str:
    return env.get(name) or DEFAULTS[name]


def _local_path(env: Mapping[str, str], name: str) -> Path:
    """Resolve the folder a variable names, refusing the shared volume (SQLite and Codex break)."""
    path = Path(_required(env, name) if name not in DEFAULTS else _setting(env, name))
    resolved = path.expanduser().resolve()
    if inside(resolved, SHARED_VOLUMES):
        raise SettingsError(f"{name} is on a shared volume; it must be a folder on the local disk")
    return resolved


def _number(env: Mapping[str, str], name: str) -> float:
    value = _setting(env, name)
    if NUMBER.fullmatch(value) is None or float(value) <= 0:
        raise SettingsError(f"{name} must be a positive number")
    return float(value)


def _roots(agent_root: Path) -> Roots:
    if not agent_root.is_dir():
        raise SettingsError("AGENT_ROOT is not a folder")
    if not (agent_root / "scenarios").is_dir():
        raise SettingsError("AGENT_ROOT has no scenarios/ folder")
    try:
        for name in ("projects", "conversations"):
            (agent_root / name).mkdir(mode=0o700, exist_ok=True)
        roots = Roots(
            *(
                (agent_root / name).resolve(strict=True)
                for name in ("scenarios", "projects", "conversations")
            )
        )
    except OSError:
        raise SettingsError("cannot create or open the folders under AGENT_ROOT") from None
    for root in roots:
        if not inside(root, agent_root) or root == agent_root:
            raise SettingsError("a folder under AGENT_ROOT points outside it (a symlink?)")
    # Disjoint, or a project could contain every conversation (conversations -> projects/x/c).
    for root in roots:
        if any(other != root and inside(root, other) for other in roots):
            raise SettingsError("the folders under AGENT_ROOT overlap (a symlink?)")
    return roots


def load_processor_settings(env: Mapping[str, str]) -> ProcessorSettings:
    """Return the checked settings, or raise SettingsError so that startup stops."""
    if any(name.startswith(LISTENER_PREFIX) for name in env):
        raise SettingsError(
            "the processor must not see MM_* variables: unset them (it never holds the token)"
        )
    backend = _setting(env, "AGENT_BACKEND")
    if backend not in BACKENDS:
        raise SettingsError(f"AGENT_BACKEND must be one of {', '.join(sorted(BACKENDS))}")
    state_dir = _local_path(env, "STATE_DIR")
    agent_root = _local_path(env, "AGENT_ROOT")
    roots = _roots(agent_root)
    codex_home = (
        Path(env["CODEX_HOME"]).expanduser().resolve()
        if env.get("CODEX_HOME")
        else agent_root / "codex-home"
    )
    if inside(codex_home, SHARED_VOLUMES):
        raise SettingsError("CODEX_HOME is on a shared volume; it must be on the local disk")
    for name, folder in (("STATE_DIR", state_dir), ("CODEX_HOME", codex_home)):
        if any(inside(folder, root) for root in roots):
            raise SettingsError(f"{name} is inside a folder the agents may read")
    return ProcessorSettings(
        state_dir=state_dir,
        agent_root=agent_root,
        roots=roots,
        rules_file=Path(_required(env, "RULES_FILE")),
        scenarios_file=roots.scenarios / "scenarios.yaml",
        backend=backend,
        codex_bin=Path(_setting(env, "CODEX_BIN")),
        codex_home=codex_home,
        codex_install_dir=Path(_setting(env, "CODEX_INSTALL_DIR")),
        claude_bin=Path(_setting(env, "CLAUDE_BIN")),
        timeout_s=_number(env, "AGENT_TIMEOUT_SECONDS"),
        poll_s=_number(env, "POLL_SECONDS"),
        log_level=_check_log_level(env.get("LOG_LEVEL")),
        home=_required(env, "HOME"),
        user=_required(env, "USER"),
        proxies={name: env[name] for name in PROXY_VARIABLES if env.get(name)},
    )

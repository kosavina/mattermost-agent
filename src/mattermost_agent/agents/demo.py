"""Run one made-up burst through a backend and print the decision, for trying a backend by hand.

    AGENT_ROOT=~/agent STATE_DIR=~/agent/state \\
        uv run python -m mattermost_agent.agents.demo codex intake

The scenario folder is AGENT_ROOT/scenarios/<scenario>, with its AGENTS.md. Everything is printed
to the terminal only; nothing is logged. (The processor's own settings module takes over the
environment reading in the next step; this keeps to the few variables the agents need.)
"""

import json
import os
import sys
import time
from pathlib import Path

from mattermost_agent.agents import (
    BACKENDS,
    AgentBackend,
    AgentError,
    AgentTask,
    BackendCheckError,
    ClaudeBackend,
    CodexBackend,
)
from mattermost_agent.agents.prompt import build_prompt
from mattermost_agent.agents.schema import validate_answer
from mattermost_agent.store import JobPost

USAGE = "usage: python -m mattermost_agent.agents.demo <codex|claude> <scenario>"
DEFAULTS = {
    "CODEX_BIN": "/opt/homebrew/bin/codex",
    "CODEX_INSTALL_DIR": "/opt/homebrew/Caskroom/codex",
    "CLAUDE_BIN": "/opt/homebrew/bin/claude",
    "AGENT_TIMEOUT_SECONDS": "120",
}
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")


def _setting(name: str) -> str:
    value = os.environ.get(name) or DEFAULTS.get(name)
    if not value:
        raise SystemExit(f"demo: set {name}")
    return value


def _backend(name: str, agent_root: Path, state_dir: Path) -> AgentBackend:
    home, user = _setting("HOME"), _setting("USER")
    proxies = {key: os.environ[key] for key in PROXY_VARIABLES if os.environ.get(key)}
    if name == "codex":
        return CodexBackend(
            binary=Path(_setting("CODEX_BIN")),
            codex_home=Path(os.environ.get("CODEX_HOME") or agent_root / "codex-home"),
            install_dir=Path(_setting("CODEX_INSTALL_DIR")),
            home=home,
            user=user,
            proxies=proxies,
            state_dir=state_dir,
        )
    return ClaudeBackend(
        binary=Path(_setting("CLAUDE_BIN")),
        home=home,
        user=user,
        proxies=proxies,
        state_dir=state_dir,
    )


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in BACKENDS:
        raise SystemExit(USAGE)
    agent_root = Path(_setting("AGENT_ROOT")).resolve()
    state_dir = Path(_setting("STATE_DIR")).resolve()
    scenario_dir = (agent_root / "scenarios" / arguments[1]).resolve()
    instructions_file = scenario_dir / "AGENTS.md"
    if not instructions_file.is_file():
        raise SystemExit(f"demo: no AGENTS.md in {scenario_dir}")
    backend = _backend(arguments[0], agent_root, state_dir)
    try:
        backend.check()
    except BackendCheckError as error:
        raise SystemExit(f"demo: {error}") from None

    now_ms = int(time.time() * 1000)
    posts = [
        JobPost("demo-1", now_ms - 60_000, "dm", "hi, could you check the weekly sales dashboard?"),
        JobPost("demo-2", now_ms, "dm", "the total for last week looks off by about 10%"),
    ]
    task = AgentTask(
        scenario_dir=scenario_dir,
        read_dirs=(),
        instructions=instructions_file.read_text(encoding="utf-8"),
        prompt=build_prompt(posts, None, "example.user"),
        timeout_s=float(_setting("AGENT_TIMEOUT_SECONDS")),
    )
    try:
        result = backend.run(task)
        answer = validate_answer(result.output)
    except AgentError as error:
        raise SystemExit(f"demo: the agent run failed: {error.reason}") from None
    print(f"backend={result.backend} duration_s={result.duration_s:.1f} denials={result.denials}")
    print(json.dumps(answer.model_dump(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

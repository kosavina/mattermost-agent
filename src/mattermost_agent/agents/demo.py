"""Run one made-up burst through a backend and print the decision, for trying a backend by hand.

    STATE_DIR=~/agent/state AGENT_ROOT=~/agent RULES_FILE=~/agent/rules.yaml \\
        uv run python -m mattermost_agent.agents.demo codex intake

The processor's settings are read and checked as for `process`; the scenario must be in
scenarios.yaml. No conversation folder or project is attached. Everything is printed to the
terminal only; nothing is logged.
"""

import json
import os
import sys
import time

from mattermost_agent.agents import BACKENDS, AgentError, AgentTask, BackendCheckError
from mattermost_agent.agents.prompt import build_prompt
from mattermost_agent.agents.schema import validate_answer
from mattermost_agent.jobs import safe_text
from mattermost_agent.processor.scenarios import (
    INSTRUCTIONS_FILE,
    ScenariosError,
    load_scenarios,
    scenario_dir,
)
from mattermost_agent.processor.settings import load_processor_settings
from mattermost_agent.settings import SettingsError
from mattermost_agent.store import JobPost

USAGE = "usage: python -m mattermost_agent.agents.demo <codex|claude> <scenario>"


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] not in BACKENDS:
        raise SystemExit(USAGE)
    try:
        settings = load_processor_settings(os.environ)
        scenarios = load_scenarios(settings.scenarios_file)
        if arguments[1] not in scenarios:
            raise SystemExit(f"demo: scenarios.yaml has no scenario {arguments[1]}")
        folder = scenario_dir(settings.roots, scenarios[arguments[1]])
        backend = settings.backends()[arguments[0]]
        backend.check()
    except (SettingsError, ScenariosError, BackendCheckError) as error:
        raise SystemExit(f"demo: {error}") from None

    now_ms = int(time.time() * 1000)
    posts = [
        JobPost("demo-1", now_ms - 60_000, "dm", "hi, could you check the weekly sales dashboard?"),
        JobPost("demo-2", now_ms, "dm", "the total for last week looks off by about 10%"),
    ]
    task = AgentTask(
        scenario_dir=folder,
        read_dirs=(),
        instructions=(folder / INSTRUCTIONS_FILE).read_text(encoding="utf-8"),
        prompt=build_prompt(posts, None, "example.user", mode="draft"),
        timeout_s=settings.timeout_s,
    )
    try:
        result = backend.run(task)
        answer = validate_answer(result.output)
    except AgentError as error:
        raise SystemExit(f"demo: the agent run failed: {error.reason}") from None
    print(f"backend={result.backend} duration_s={result.duration_s:.1f} denials={result.denials}")
    shown = {k: safe_text(v) if isinstance(v, str) else v for k, v in answer.model_dump().items()}
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

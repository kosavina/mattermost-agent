"""Read scenarios.yaml from the scenarios folder, and check a scenario's folder before a run.

A scenario folder holds our instructions (AGENTS.md), trusted. It must not hold CLAUDE.md,
CLAUDE.local.md or .claude/: Claude Code does not load them under our flags today, but one
source of instructions is the rule, and a later version could.
"""

from dataclasses import dataclass
from pathlib import Path

from mattermost_agent.agents import BACKENDS
from mattermost_agent.processor.settings import Roots, inside
from mattermost_agent.rules import FOLDER_NAME, RulesError, _mapping, _text, parse_yaml

TOP_LEVEL_KEYS = frozenset({"scenarios"})
SCENARIO_KEYS = frozenset({"folder", "backend"})
INSTRUCTIONS_FILE = "AGENTS.md"
FORBIDDEN = ("CLAUDE.md", "CLAUDE.local.md", ".claude")


class ScenariosError(Exception):
    """The scenarios cannot be used. The message says where and why, never a piece of the file."""


@dataclass(frozen=True)
class Scenario:
    name: str
    folder: str
    backend: str | None


def _scenario(name: object, value: object) -> Scenario:
    if not isinstance(name, str) or not FOLDER_NAME.fullmatch(name):
        raise ScenariosError("a scenario name must be lower-case letters, digits, - and _")
    where = f"scenario {name}"
    raw = _mapping(value, where, SCENARIO_KEYS)
    folder = _text(raw.get("folder"), f"{where} folder")
    if not FOLDER_NAME.fullmatch(folder):
        raise ScenariosError(f"{where} folder must be a single folder name, no / or .")
    backend = None
    if "backend" in raw:
        backend = _text(raw["backend"], f"{where} backend")
        if backend not in BACKENDS:
            raise ScenariosError(f"{where} backend must be one of {', '.join(sorted(BACKENDS))}")
    return Scenario(name=name, folder=folder, backend=backend)


def parse_scenarios(text: str) -> dict[str, Scenario]:
    """Return the scenarios by name, or raise ScenariosError."""
    try:
        top = _mapping(parse_yaml(text), "the file", TOP_LEVEL_KEYS)
        raw = top.get("scenarios")
        if not isinstance(raw, dict) or not raw:
            raise ScenariosError("scenarios must be a mapping with at least one scenario")
        return {str(name): _scenario(name, value) for name, value in raw.items()}
    except RulesError as error:
        raise ScenariosError(str(error)) from None


def load_scenarios(path: Path) -> dict[str, Scenario]:
    """Read the scenarios file, or raise ScenariosError so that startup stops."""
    try:
        return parse_scenarios(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ScenariosError(f"cannot read the scenarios file {path}: {error.strerror}") from None
    except UnicodeDecodeError:
        raise ScenariosError(f"the scenarios file {path} is not UTF-8 text") from None
    except ScenariosError as error:
        raise ScenariosError(f"the scenarios file {path}: {error}") from None


def scenario_dir(roots: Roots, scenario: Scenario) -> Path:
    """Return the scenario's folder, resolved and checked, or raise ScenariosError."""
    try:
        path = (roots.scenarios / scenario.folder).resolve(strict=True)
    except OSError:
        raise ScenariosError(f"scenario {scenario.name}: its folder does not exist") from None
    if not (path.is_dir() and inside(path, roots.scenarios) and path != roots.scenarios):
        raise ScenariosError(f"scenario {scenario.name}: its folder is not inside scenarios/")
    if not (path / INSTRUCTIONS_FILE).is_file():
        raise ScenariosError(f"scenario {scenario.name}: no {INSTRUCTIONS_FILE} in its folder")
    for name in FORBIDDEN:
        if (path / name).exists() or (path / name).is_symlink():
            raise ScenariosError(f"scenario {scenario.name}: its folder holds {name}; remove it")
    return path

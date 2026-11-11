"""Turn a job's scenario, rule and channel into the folders one agent run may read.

Every folder is resolved (symlinks followed) and must land where it belongs: the scenario inside
scenarios/, the conversation inside conversations/, each project inside projects/ and nowhere
near the other two. The conversation folder is derived from the channel id, never named in
config. A scenario or rule that config does not know means the job fails; nothing runs with
defaults.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mattermost_agent.processor.scenarios import (
    INSTRUCTIONS_FILE,
    Scenario,
    ScenariosError,
    scenario_dir,
)
from mattermost_agent.processor.settings import Roots, inside
from mattermost_agent.rules import RuleSet

# Mattermost ids are 26 characters of a base-32 alphabet. Anything else never becomes a path.
CHANNEL_ID = re.compile(r"[a-z0-9]{26}")

type SetupReason = Literal["unknown_scenario", "unknown_rule", "bad_folder"]


class JobSetupError(Exception):
    """The job cannot run as configured. Not retried. The message is the reason only."""

    def __init__(self, reason: SetupReason) -> None:
        super().__init__(reason)
        self.reason: SetupReason = reason


@dataclass(frozen=True)
class Resolved:
    scenario_dir: Path
    conversation_dir: Path
    read_dirs: tuple[Path, ...]
    backend: str
    instructions: str = field(repr=False)


def conversation_dir(roots: Roots, channel_id: str) -> Path:
    """Return the conversation's folder, created if needed, or raise JobSetupError."""
    if CHANNEL_ID.fullmatch(channel_id) is None:
        raise JobSetupError("bad_folder")
    path = roots.conversations / channel_id
    try:
        if path.is_symlink():
            raise JobSetupError("bad_folder")
        path.mkdir(mode=0o700, exist_ok=True)
        resolved = path.resolve(strict=True)
    except OSError:
        raise JobSetupError("bad_folder") from None
    # The exact folder, not just a folder in conversations/: a link to a sibling would pass that.
    if not (resolved.is_dir() and resolved == path):
        raise JobSetupError("bad_folder")
    return resolved


def project_dir(roots: Roots, name: str) -> Path:
    """Return one attached project folder, resolved, or raise JobSetupError."""
    try:
        resolved = (roots.projects / name).resolve(strict=True)
    except OSError:
        raise JobSetupError("bad_folder") from None
    if not resolved.is_dir() or not inside(resolved, roots.projects) or resolved == roots.projects:
        raise JobSetupError("bad_folder")
    if inside(resolved, roots.conversations) or inside(resolved, roots.scenarios):
        raise JobSetupError("bad_folder")
    return resolved


def resolve(
    *,
    scenario: str,
    rule: str,
    channel_id: str,
    rules: RuleSet,
    scenarios: Mapping[str, Scenario],
    roots: Roots,
    default_backend: str,
) -> Resolved:
    """Return everything an agent run needs from the folders, or raise JobSetupError."""
    known = scenarios.get(scenario)
    if known is None:
        raise JobSetupError("unknown_scenario")
    matched = next((item for item in rules.rules if item.name == rule), None)
    if matched is None or matched.scenario != scenario:
        raise JobSetupError("unknown_rule")
    try:
        folder = scenario_dir(roots, known)
        instructions = (folder / INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    except ScenariosError, OSError, UnicodeDecodeError:
        raise JobSetupError("bad_folder") from None
    conversation = conversation_dir(roots, channel_id)
    projects = tuple(project_dir(roots, name) for name in matched.read)
    return Resolved(
        scenario_dir=folder,
        conversation_dir=conversation,
        read_dirs=(conversation, *projects),
        backend=known.backend or default_backend,
        instructions=instructions,
    )

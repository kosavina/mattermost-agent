"""Read rules.yaml, refuse anything doubtful, and find the first rule that matches a post.

Validation is strict because a mistake here fails silently otherwise: a misspelt `channel:` would
be ignored, leaving a rule that matches far more than intended. Errors give the rule's number or
a line number, never a piece of the file.
"""

import re
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from mattermost_agent.events import Post

TOP_LEVEL_KEYS = frozenset({"rules", "limits", "bursts"})
LIMIT_KEYS = frozenset({"jobs_per_hour"})
BURST_KEYS = ("quiet_seconds", "max_wait_seconds", "max_posts")
RULE_KEYS = frozenset({"name", "scenario", "dm", "channels", "from", "read"})
CONDITION_KEYS = frozenset({"dm", "channels", "from"})

CHANNEL_NAME = re.compile(r"[a-z0-9_-]+", re.IGNORECASE)
USERNAME = re.compile(r"[a-z0-9._-]+", re.IGNORECASE)
# A folder name under the agent root: one path segment, so no `/`, no `..`, not absolute or hidden.
FOLDER_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


class RulesError(Exception):
    """The rules cannot be used. The message says where and why."""


@dataclass(frozen=True)
class Rule:
    """One rule. `dm` False and an empty set mean "any". Channels and senders are in lower case."""

    name: str
    scenario: str
    dm: bool
    channels: frozenset[str]
    senders: frozenset[str]
    read: tuple[str, ...] = ()
    """Project folders the processor attaches read-only, by name under the agent root."""


@dataclass(frozen=True)
class Bursts:
    """When a burst of posts is complete. Whole numbers, at least 1."""

    quiet_seconds: int
    max_wait_seconds: int
    max_posts: int


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    jobs_per_hour: int
    bursts: Bursts

    @property
    def channel_names(self) -> frozenset[str]:
        """Return every channel the rules name, which catch-up reads besides the DMs."""
        return frozenset(name for rule in self.rules for name in rule.channels)


class _StrictLoader(yaml.SafeLoader):
    """Refuse duplicate keys. Plain YAML silently keeps only the last one."""


def _unique_keys(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Hashable, object]:
    seen: set[str] = set()
    for key, _ in node.value:
        if isinstance(key, yaml.ScalarNode):
            if key.value in seen:
                raise RulesError(f"duplicate key on line {key.start_mark.line + 1}")
            seen.add(key.value)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(_StrictLoader.DEFAULT_MAPPING_TAG, _unique_keys)


def parse_yaml(text: str) -> object:
    """Parse YAML strictly (safe loader, no duplicate keys), or raise RulesError."""
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a subclass of SafeLoader
    except yaml.MarkedYAMLError as error:
        line = f" on line {error.problem_mark.line + 1}" if error.problem_mark else ""
        raise RulesError(f"not valid YAML{line}") from None
    except yaml.YAMLError:
        raise RulesError("not valid YAML") from None


def _mapping(value: object, where: str, allowed: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RulesError(f"{where} must be a mapping")
    unknown = [key for key in value if key not in allowed]
    if unknown:
        raise RulesError(f"{where} has an unknown key; allowed: {', '.join(sorted(allowed))}")
    return cast(dict[str, object], value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RulesError(f"{where} must be text, and not empty (put numbers and yes/no in quotes)")
    return value


def _text_list(value: object, where: str, pattern: re.Pattern[str]) -> list[str]:
    """Return a non-empty list of lower-case text, each item matching `pattern`."""
    if not isinstance(value, list) or not value:
        raise RulesError(f"{where} must be a list with at least one item")
    items = [_text(item, f"{where} item").casefold() for item in value]
    if not all(pattern.fullmatch(item) for item in items):
        raise RulesError(f"{where} must hold plain names, with no @, ~ or spaces")
    return items


def folder_names(value: object, where: str) -> tuple[str, ...]:
    """Return a list of distinct folder names, each a single lower-case path segment."""
    if not isinstance(value, list) or not value:
        raise RulesError(f"{where} must be a list with at least one item")
    names = tuple(_text(item, f"{where} item") for item in value)
    if not all(FOLDER_NAME.fullmatch(name) for name in names):
        raise RulesError(
            f"{where} must hold folder names: lower-case letters, digits, - and _, no / or ."
        )
    if len(set(names)) != len(names):
        raise RulesError(f"{where} names a folder twice")
    return names


def _whole_number(value: object, where: str) -> int:
    # `type` and not isinstance: YAML `true` is a bool, and a bool is a kind of int in Python.
    if type(value) is not int or value < 1:
        raise RulesError(f"{where} must be a whole number, at least 1")
    return value


def _rule(value: object, number: int) -> Rule:
    where = f"rule {number}"
    raw = _mapping(value, where, RULE_KEYS)
    if not CONDITION_KEYS & raw.keys():
        raise RulesError(f"{where} needs at least one of: {', '.join(sorted(CONDITION_KEYS))}")
    dm = "dm" in raw
    # `is True` and not truthiness: 1, "true" or [true] must not pass as true.
    if dm and raw["dm"] is not True:
        raise RulesError(f"{where} dm must be true, or left out")
    if dm and "channels" in raw:
        raise RulesError(f"{where} sets both dm and channels, which no post can match")
    return Rule(
        name=_text(raw.get("name"), f"{where} name"),
        scenario=_text(raw.get("scenario"), f"{where} scenario"),
        dm=dm,
        channels=frozenset(
            _text_list(raw["channels"], f"{where} channels", CHANNEL_NAME)
            if "channels" in raw
            else ()
        ),
        senders=frozenset(
            _text_list(raw["from"], f"{where} from", USERNAME) if "from" in raw else ()
        ),
        read=folder_names(raw["read"], f"{where} read") if "read" in raw else (),
    )


def _bursts(value: object) -> Bursts:
    raw = _mapping(value, "bursts", frozenset(BURST_KEYS))
    bursts = Bursts(*(_whole_number(raw.get(key), f"bursts {key}") for key in BURST_KEYS))
    if bursts.quiet_seconds > bursts.max_wait_seconds:
        raise RulesError("bursts quiet_seconds must be at most max_wait_seconds")
    return bursts


def parse_rules(text: str) -> RuleSet:
    """Return the checked rules, or raise RulesError."""
    top = _mapping(parse_yaml(text), "the file", TOP_LEVEL_KEYS)
    raw_rules = top.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RulesError("rules must be a list with at least one rule")
    rules = tuple(_rule(value, number) for number, value in enumerate(raw_rules, start=1))
    if len({rule.name for rule in rules}) != len(rules):
        raise RulesError("two rules have the same name")

    limits = _mapping(top.get("limits"), "limits", LIMIT_KEYS)
    cap = _whole_number(limits.get("jobs_per_hour"), "limits jobs_per_hour")
    return RuleSet(rules=rules, jobs_per_hour=cap, bursts=_bursts(top.get("bursts")))


def load_rules(path: Path) -> RuleSet:
    """Read the rules file, or raise RulesError so that startup stops."""
    try:
        return parse_rules(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RulesError(f"cannot read the rules file {path}: {error.strerror}") from None
    except UnicodeDecodeError:
        raise RulesError(f"the rules file {path} is not UTF-8 text") from None
    except RulesError as error:
        raise RulesError(f"the rules file {path}: {error}") from None


def _matches(rule: Rule, post: Post, sender: str | None, *, own: bool) -> bool:
    """Every condition the rule sets must hold. Within a list, any one item is enough."""
    # A post of mine needs a rule that names me, or `dm: true` would also match the DMs I send.
    if own and not rule.senders:
        return False
    if rule.dm and post.channel_type != "D":
        return False
    if rule.channels and post.channel_name.casefold() not in rule.channels:
        return False
    return not rule.senders or (sender is not None and sender.casefold() in rule.senders)


def first_match(rule_set: RuleSet, post: Post, sender: str | None, *, own: bool) -> Rule | None:
    """Return the first rule that matches, in file order, or None."""
    return next((rule for rule in rule_set.rules if _matches(rule, post, sender, own=own)), None)

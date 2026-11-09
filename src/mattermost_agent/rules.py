"""Read rules.yaml, refuse anything doubtful, and find the first rule that matches a post.

Validation is strict because a mistake here fails silently otherwise: a misspelt `keyword:` would
be ignored, leaving a rule that matches far more than intended. Errors give the rule's number or
a line number, never a piece of the file.
"""

import re
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from mattermost_agent.classify import KINDS, Kind
from mattermost_agent.events import Post

TOP_LEVEL_KEYS = frozenset({"rules", "limits"})
LIMIT_KEYS = frozenset({"matches_per_hour"})
RULE_KEYS = frozenset({"name", "scenario", "kind", "channels", "from", "keywords"})
CONDITION_KEYS = frozenset({"kind", "channels", "from", "keywords"})

CHANNEL_NAME = re.compile(r"[a-z0-9_-]+", re.IGNORECASE)
USERNAME = re.compile(r"[a-z0-9._-]+", re.IGNORECASE)


class RulesError(Exception):
    """The rules cannot be used. The message says where and why."""


@dataclass(frozen=True)
class Rule:
    """One rule. Empty means "any". Channels, senders and keywords are in lower case."""

    name: str
    scenario: str
    kind: Kind | None
    channels: frozenset[str]
    senders: frozenset[str]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]
    matches_per_hour: int

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


def _parse_yaml(text: str) -> object:
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


def _text_list(value: object, where: str, pattern: re.Pattern[str] | None = None) -> list[str]:
    """Return a non-empty list of lower-case text, each item matching `pattern` if given."""
    if not isinstance(value, list) or not value:
        raise RulesError(f"{where} must be a list with at least one item")
    items = [_text(item, f"{where} item").casefold() for item in value]
    if pattern is not None and not all(pattern.fullmatch(item) for item in items):
        raise RulesError(f"{where} must hold plain names, with no @, ~ or spaces")
    return items


def _rule(value: object, number: int) -> Rule:
    where = f"rule {number}"
    raw = _mapping(value, where, RULE_KEYS)
    if not CONDITION_KEYS & raw.keys():
        raise RulesError(f"{where} needs at least one of: {', '.join(sorted(CONDITION_KEYS))}")
    kind = raw.get("kind")
    if "kind" in raw and (not isinstance(kind, str) or kind not in KINDS):
        raise RulesError(f"{where} kind must be one of: {', '.join(sorted(KINDS))}")
    return Rule(
        name=_text(raw.get("name"), f"{where} name"),
        scenario=_text(raw.get("scenario"), f"{where} scenario"),
        kind=cast(Kind | None, kind),
        channels=frozenset(
            _text_list(raw["channels"], f"{where} channels", CHANNEL_NAME)
            if "channels" in raw
            else ()
        ),
        senders=frozenset(
            _text_list(raw["from"], f"{where} from", USERNAME) if "from" in raw else ()
        ),
        keywords=tuple(_text_list(raw["keywords"], f"{where} keywords"))
        if "keywords" in raw
        else (),
    )


def parse_rules(text: str) -> RuleSet:
    """Return the checked rules, or raise RulesError."""
    top = _mapping(_parse_yaml(text), "the file", TOP_LEVEL_KEYS)
    raw_rules = top.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RulesError("rules must be a list with at least one rule")
    rules = tuple(_rule(value, number) for number, value in enumerate(raw_rules, start=1))
    if len({rule.name for rule in rules}) != len(rules):
        raise RulesError("two rules have the same name")

    limits = _mapping(top.get("limits"), "limits", LIMIT_KEYS)
    cap = limits.get("matches_per_hour")
    # `type` and not isinstance: YAML `true` is a bool, and a bool is a kind of int in Python.
    if type(cap) is not int or cap < 1:
        raise RulesError("limits matches_per_hour must be a whole number, at least 1")
    return RuleSet(rules=rules, matches_per_hour=cap)


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


def _matches(rule: Rule, post: Post, kind: Kind, sender: str | None, *, own: bool) -> bool:
    """Every condition the rule sets must hold. Within a list, any one item is enough."""
    # A post of mine needs a rule that names me, or `kind: dm` would also match the DMs I send.
    if own and not rule.senders:
        return False
    if rule.kind is not None and rule.kind != kind:
        return False
    if rule.channels and post.channel_name.casefold() not in rule.channels:
        return False
    if rule.senders and (sender is None or sender.casefold() not in rule.senders):
        return False
    if rule.keywords:
        # Part of a word counts, so that Russian endings match: "дашборд" finds "дашборде".
        text = post.message.casefold()
        return any(keyword in text for keyword in rule.keywords)
    return True


def first_match(
    rule_set: RuleSet, post: Post, kind: Kind, sender: str | None, *, own: bool
) -> Rule | None:
    """Return the first rule that matches, in file order, or None."""
    return next(
        (rule for rule in rule_set.rules if _matches(rule, post, kind, sender, own=own)), None
    )

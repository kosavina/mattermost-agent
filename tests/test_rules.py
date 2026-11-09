from pathlib import Path

import pytest
from helpers import make_post

from mattermost_agent.classify import Kind
from mattermost_agent.rules import RulesError, RuleSet, first_match, load_rules, parse_rules

EXAMPLE = """
rules:
  - name: dm-from-anyone
    kind: dm
    scenario: intake
  - name: mentions-me
    kind: mention
    scenario: intake
  - name: analytics-keywords
    channels: [analytics]
    from: [example.user]
    keywords: [dashboard, metric]
    scenario: intake
limits:
  matches_per_hour: 20
"""


def rules_of(*body: str, cap: int = 20) -> RuleSet:
    """Build a rule set, one rule per argument, each written as YAML flow text."""
    lines = "\n".join(f"  - {{{rule}}}" for rule in body)
    return parse_rules(f"rules:\n{lines}\nlimits:\n  matches_per_hour: {cap}\n")


def matched(
    rule_set: RuleSet,
    kind: Kind = "channel",
    sender: str | None = "example.user",
    **post: object,
) -> str | None:
    rule = first_match(rule_set, make_post(**post), kind, sender)
    return None if rule is None else rule.name


def test_the_documented_example_is_valid() -> None:
    rule_set = parse_rules(EXAMPLE)
    assert [rule.name for rule in rule_set.rules] == [
        "dm-from-anyone",
        "mentions-me",
        "analytics-keywords",
    ]
    assert rule_set.matches_per_hour == 20
    assert rule_set.channel_names == frozenset({"analytics"})


def test_example_dm_and_mention() -> None:
    rule_set = parse_rules(EXAMPLE)
    assert matched(rule_set, kind="dm") == "dm-from-anyone"
    assert matched(rule_set, kind="mention") == "mentions-me"


def test_example_keywords_need_every_condition() -> None:
    rule_set = parse_rules(EXAMPLE)
    assert matched(rule_set, message="new dashboard") == "analytics-keywords"
    assert matched(rule_set, message="nothing relevant") is None
    assert matched(rule_set, message="new dashboard", sender="someone.else") is None
    assert matched(rule_set, message="new dashboard", channel_name="random") is None


def test_first_rule_wins() -> None:
    rule_set = rules_of(
        "name: first, keywords: [hello], scenario: a",
        "name: second, keywords: [hello], scenario: b",
    )
    assert matched(rule_set, message="hello") == "first"


def test_a_later_rule_matches_when_the_first_does_not() -> None:
    rule_set = rules_of(
        "name: first, keywords: [absent], scenario: a",
        "name: second, keywords: [hello], scenario: b",
    )
    assert matched(rule_set, message="hello") == "second"


def test_no_rule_matches() -> None:
    assert matched(rules_of("name: only, kind: dm, scenario: a"), kind="channel") is None


def test_any_item_of_a_list_is_enough() -> None:
    rule_set = rules_of("name: r, keywords: [one, two], scenario: a")
    assert matched(rule_set, message="two") == "r"


@pytest.mark.parametrize(
    ("keyword", "message"),
    [
        ("dashboard", "the DashBoard is down"),
        ("DASHBOARD", "the dashboard is down"),
        ("metric", "new metrics"),
        ("дашборд", "ошибка на ДАШБОРДЕ"),
    ],
)
def test_keywords_ignore_case_and_match_part_of_a_word(keyword: str, message: str) -> None:
    rule_set = rules_of(f"name: r, keywords: [{keyword}], scenario: a")
    assert matched(rule_set, message=message) == "r"


def test_keywords_are_literal_text_and_not_patterns() -> None:
    rule_set = rules_of('name: r, keywords: ["a.c"], scenario: a')
    assert matched(rule_set, message="abc") is None
    assert matched(rule_set, message="a.c") == "r"


def test_channels_and_senders_ignore_case() -> None:
    rule_set = rules_of("name: r, channels: [Analytics], from: [Example.User], scenario: a")
    assert matched(rule_set, sender="EXAMPLE.user", channel_name="ANALYTICS") == "r"


def test_a_sender_rule_does_not_match_an_unknown_sender() -> None:
    assert matched(rules_of("name: r, from: [example.user], scenario: a"), sender=None) is None


@pytest.mark.parametrize(
    "bad_rule",
    [
        "name: r, scenario: a",
        "name: r, keyword: [typo], scenario: a",
        "name: r, kind: dm, scenario: a, extra: 1",
        "kind: dm, scenario: a",
        "name: r, kind: dm",
        'name: "", kind: dm, scenario: a',
        'name: "  ", kind: dm, scenario: a',
        "name: 7, kind: dm, scenario: a",
        "name: r, kind: unknown, scenario: a",
        "name: r, kind: [dm], scenario: a",
        "name: r, kind: null, scenario: a",
        "name: r, channels: [], scenario: a",
        "name: r, channels: analytics, scenario: a",
        "name: r, channels: [~analytics], scenario: a",
        "name: r, channels: [Town Square], scenario: a",
        "name: r, from: [], scenario: a",
        "name: r, from: [@example.user], scenario: a",
        "name: r, keywords: [], scenario: a",
        "name: r, keywords: word, scenario: a",
        'name: r, keywords: [""], scenario: a',
        "name: r, keywords: [on], scenario: a",
        "name: r, keywords: [123], scenario: a",
        "name: r, keywords: null, scenario: a",
    ],
)
def test_bad_rules_are_refused(bad_rule: str) -> None:
    with pytest.raises(RulesError, match=r"rule 1|not valid YAML"):
        rules_of(bad_rule)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "just text",
        "rules: []\nlimits: {matches_per_hour: 1}\n",
        "rules: {}\nlimits: {matches_per_hour: 1}\n",
        "limits: {matches_per_hour: 1}\n",
        "rules:\n  - {name: r, kind: dm, scenario: a}\n",
        "rules:\n  - {name: r, kind: dm, scenario: a}\nlimits: 5\n",
        "rules:\n  - {name: r, kind: dm, scenario: a}\nlimits: {}\n",
        "rules:\n  - {name: r, kind: dm, scenario: a}\nlimits: {matches_per_hour: 1}\nextra: 1\n",
        "rules:\n  - {name: r, kind: dm, scenario: a}\nlimits: {matches_per_hour: 1, other: 1}\n",
        "rules:\n  - not a mapping\nlimits: {matches_per_hour: 1}\n",
    ],
)
def test_bad_files_are_refused(text: str) -> None:
    with pytest.raises(RulesError):
        parse_rules(text)


@pytest.mark.parametrize("cap", ["0", "-1", "true", "1.5", '"20"', "null"])
def test_bad_caps_are_refused(cap: str) -> None:
    text = f"rules:\n  - {{name: r, kind: dm, scenario: a}}\nlimits:\n  matches_per_hour: {cap}\n"
    with pytest.raises(RulesError, match="matches_per_hour"):
        parse_rules(text)


def test_duplicate_rule_names_are_refused() -> None:
    with pytest.raises(RulesError, match="same name"):
        rules_of("name: r, kind: dm, scenario: a", "name: r, kind: mention, scenario: a")


def test_duplicate_keys_are_refused_with_their_line() -> None:
    text = "rules:\n  - name: r\n    kind: dm\n    kind: mention\n    scenario: a\n"
    with pytest.raises(RulesError, match="duplicate key on line 4"):
        parse_rules(text + "limits: {matches_per_hour: 1}\n")


def test_python_objects_in_yaml_are_refused() -> None:
    with pytest.raises(RulesError, match="not valid YAML"):
        parse_rules("!!python/object/apply:os.system [echo hacked]\n")


def test_yaml_errors_do_not_quote_the_file() -> None:
    with pytest.raises(RulesError) as error:
        parse_rules("rules: [sentinel-private-name\n")
    assert "sentinel-private-name" not in str(error.value)
    assert "line" in str(error.value)


def test_unknown_key_is_not_quoted() -> None:
    with pytest.raises(RulesError) as error:
        parse_rules("sentinel-private-key: 1\n")
    assert "sentinel-private-key" not in str(error.value)


def test_rules_are_loaded_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(EXAMPLE, encoding="utf-8")
    assert load_rules(path).matches_per_hour == 20


def test_missing_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"
    with pytest.raises(RulesError, match="cannot read the rules file"):
        load_rules(path)


def test_errors_name_the_file(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(RulesError, match=r"rules\.yaml: the file must be a mapping"):
        load_rules(path)


def test_binary_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "rules.yaml"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(RulesError, match="not UTF-8"):
        load_rules(path)

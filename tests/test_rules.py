from pathlib import Path

import pytest
from helpers import make_post

from mattermost_agent.rules import (
    Bursts,
    RulesError,
    RuleSet,
    first_match,
    load_rules,
    parse_rules,
)

EXAMPLE = """
rules:
  - name: dm-from-anyone
    dm: true
    scenario: intake
  - name: analytics-requests
    channels: [analytics]
    from: [example.user]
    scenario: intake
limits:
  jobs_per_hour: 20
bursts:
  quiet_seconds: 90
  max_wait_seconds: 600
  max_posts: 20
"""
LIMITS = "limits: {jobs_per_hour: 20}\n"
BURSTS = "bursts: {quiet_seconds: 90, max_wait_seconds: 600, max_posts: 20}\n"
ONE_RULE = "rules:\n  - {name: r, dm: true, scenario: a}\n"


def rules_of(*body: str, tail: str = LIMITS + BURSTS) -> RuleSet:
    """Build a rule set, one rule per argument, each written as YAML flow text."""
    lines = "\n".join(f"  - {{{rule}}}" for rule in body)
    return parse_rules(f"rules:\n{lines}\n{tail}")


def matched(
    rule_set: RuleSet, sender: str | None = "example.user", own: bool = False, **post: object
) -> str | None:
    rule = first_match(rule_set, make_post(**post), sender, own=own)
    return None if rule is None else rule.name


def test_my_own_post_skips_a_rule_that_names_no_sender() -> None:
    rule_set = rules_of("name: r, dm: true, scenario: a")
    assert matched(rule_set, "example.me", own=True, channel_type="D") is None


def test_my_own_post_matches_a_rule_that_names_me() -> None:
    rule_set = rules_of("name: r, dm: true, from: [example.me], scenario: a")
    assert matched(rule_set, "example.me", own=True, channel_type="D") == "r"


def test_my_own_post_skips_a_rule_that_names_someone_else() -> None:
    rule_set = rules_of("name: r, dm: true, from: [example.user], scenario: a")
    assert matched(rule_set, "example.me", own=True, channel_type="D") is None


def test_the_documented_example_is_valid() -> None:
    rule_set = parse_rules(EXAMPLE)
    assert [rule.name for rule in rule_set.rules] == ["dm-from-anyone", "analytics-requests"]
    assert rule_set.jobs_per_hour == 20
    assert rule_set.bursts == Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=20)
    assert rule_set.channel_names == frozenset({"analytics"})


def test_example_dm() -> None:
    rule_set = parse_rules(EXAMPLE)
    assert matched(rule_set, channel_type="D", channel_name="a__b") == "dm-from-anyone"
    assert matched(rule_set, channel_type="G", channel_name="abc123") is None


def test_example_channel_and_sender_need_every_condition() -> None:
    rule_set = parse_rules(EXAMPLE)
    assert matched(rule_set, channel_name="analytics") == "analytics-requests"
    assert matched(rule_set, channel_name="analytics", sender="someone.else") is None
    assert matched(rule_set, channel_name="random") is None


def test_dm_matches_only_a_one_to_one_message() -> None:
    rule_set = rules_of("name: r, dm: true, scenario: a")
    assert matched(rule_set, channel_type="D") == "r"
    for channel_type in ("G", "O", "P"):
        assert matched(rule_set, channel_type=channel_type) is None


def test_a_sender_rule_matches_anywhere() -> None:
    rule_set = rules_of("name: r, from: [example.user], scenario: a")
    assert matched(rule_set, channel_type="D") == "r"
    assert matched(rule_set, channel_type="G") == "r"
    assert matched(rule_set, channel_name="anything") == "r"


def test_first_rule_wins() -> None:
    rule_set = rules_of(
        "name: first, from: [example.user], scenario: a",
        "name: second, from: [example.user], scenario: b",
    )
    assert matched(rule_set) == "first"


def test_a_later_rule_matches_when_the_first_does_not() -> None:
    rule_set = rules_of(
        "name: first, channels: [absent], scenario: a",
        "name: second, from: [example.user], scenario: b",
    )
    assert matched(rule_set) == "second"


def test_no_rule_matches() -> None:
    assert matched(rules_of("name: only, dm: true, scenario: a"), channel_type="O") is None


def test_any_item_of_a_list_is_enough() -> None:
    rule_set = rules_of("name: r, channels: [one, two], scenario: a")
    assert matched(rule_set, channel_name="two") == "r"


def test_channels_and_senders_ignore_case() -> None:
    rule_set = rules_of("name: r, channels: [Analytics], from: [Example.User], scenario: a")
    assert matched(rule_set, sender="EXAMPLE.user", channel_name="ANALYTICS") == "r"


def test_a_sender_rule_does_not_match_an_unknown_sender() -> None:
    assert matched(rules_of("name: r, from: [example.user], scenario: a"), sender=None) is None


@pytest.mark.parametrize(
    "bad_rule",
    [
        "name: r, scenario: a",
        "name: r, channel: [typo], scenario: a",
        "name: r, dm: true, scenario: a, extra: 1",
        "dm: true, scenario: a",
        "name: r, dm: true",
        'name: "", dm: true, scenario: a',
        'name: "  ", dm: true, scenario: a',
        "name: 7, dm: true, scenario: a",
        "name: r, dm: false, scenario: a",
        "name: r, dm: 1, scenario: a",
        'name: r, dm: "true", scenario: a',
        "name: r, dm: [true], scenario: a",
        "name: r, dm: null, scenario: a",
        "name: r, dm: true, channels: [analytics], scenario: a",
        "name: r, channels: [], scenario: a",
        "name: r, channels: analytics, scenario: a",
        "name: r, channels: [~analytics], scenario: a",
        "name: r, channels: [Town Square], scenario: a",
        "name: r, from: [], scenario: a",
        "name: r, from: [@example.user], scenario: a",
    ],
)
def test_bad_rules_are_refused(bad_rule: str) -> None:
    with pytest.raises(RulesError, match=r"rule 1|not valid YAML"):
        rules_of(bad_rule)


def test_dm_false_is_refused_with_advice() -> None:
    with pytest.raises(RulesError, match="rule 1 dm must be true, or left out"):
        rules_of("name: r, dm: false, scenario: a")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "just text",
        "rules: []\n" + LIMITS + BURSTS,
        "rules: {}\n" + LIMITS + BURSTS,
        LIMITS + BURSTS,
        ONE_RULE,
        ONE_RULE + BURSTS,
        ONE_RULE + LIMITS,
        ONE_RULE + "limits: 5\n" + BURSTS,
        ONE_RULE + "limits: {}\n" + BURSTS,
        ONE_RULE + LIMITS + BURSTS + "extra: 1\n",
        ONE_RULE + "limits: {jobs_per_hour: 1, other: 1}\n" + BURSTS,
        ONE_RULE + LIMITS + "bursts: 5\n",
        ONE_RULE + LIMITS + "bursts: {}\n",
        ONE_RULE + LIMITS + "bursts: {quiet_seconds: 90, max_wait_seconds: 600}\n",
        ONE_RULE + LIMITS + "bursts: {quiet_seconds: 1, max_wait_seconds: 1, max_posts: 1, x: 1}\n",
        "rules:\n  - not a mapping\n" + LIMITS + BURSTS,
    ],
)
def test_bad_files_are_refused(text: str) -> None:
    with pytest.raises(RulesError):
        parse_rules(text)


BAD_NUMBERS = ["0", "-1", "true", "1.5", '"20"', "null"]


@pytest.mark.parametrize("cap", BAD_NUMBERS)
def test_bad_caps_are_refused(cap: str) -> None:
    with pytest.raises(RulesError, match="limits jobs_per_hour must be a whole number"):
        parse_rules(ONE_RULE + f"limits: {{jobs_per_hour: {cap}}}\n" + BURSTS)


@pytest.mark.parametrize("key", ["quiet_seconds", "max_wait_seconds", "max_posts"])
@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_bad_burst_numbers_are_refused(key: str, value: str) -> None:
    good = {"quiet_seconds": 90, "max_wait_seconds": 600, "max_posts": 20}
    items = ", ".join(f"{k}: {value if k == key else v}" for k, v in good.items())
    with pytest.raises(RulesError, match=f"bursts {key} must be a whole number"):
        parse_rules(ONE_RULE + LIMITS + f"bursts: {{{items}}}\n")


def test_quiet_longer_than_the_wait_is_refused() -> None:
    bursts = "bursts: {quiet_seconds: 601, max_wait_seconds: 600, max_posts: 20}\n"
    with pytest.raises(RulesError, match="quiet_seconds must be at most max_wait_seconds"):
        parse_rules(ONE_RULE + LIMITS + bursts)


def test_quiet_equal_to_the_wait_is_allowed() -> None:
    bursts = "bursts: {quiet_seconds: 600, max_wait_seconds: 600, max_posts: 1}\n"
    assert parse_rules(ONE_RULE + LIMITS + bursts).bursts.max_posts == 1


def test_duplicate_rule_names_are_refused() -> None:
    with pytest.raises(RulesError, match="same name"):
        rules_of("name: r, dm: true, scenario: a", "name: r, channels: [c], scenario: a")


def test_duplicate_keys_are_refused_with_their_line() -> None:
    text = "rules:\n  - name: r\n    dm: true\n    dm: true\n    scenario: a\n"
    with pytest.raises(RulesError, match="duplicate key on line 4"):
        parse_rules(text + LIMITS + BURSTS)


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
    assert load_rules(path).jobs_per_hour == 20


def test_the_shipped_example_file_is_valid() -> None:
    example = Path(__file__).parent.parent / "rules.example.yaml"
    assert load_rules(example).channel_names == frozenset({"analytics"})


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


def test_read_lists_project_folders() -> None:
    rule_set = rules_of("name: r, dm: true, scenario: a, read: [sales-reports, dwh_docs2]")
    assert rule_set.rules[0].read == ("sales-reports", "dwh_docs2")
    assert rules_of("name: r, dm: true, scenario: a").rules[0].read == ()


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("[]", "rule 1 read must be a list"),
        ("sales", "rule 1 read must be a list"),
        ("[Sales]", "rule 1 read must hold folder names"),
        ("[../x]", "rule 1 read must hold folder names"),
        ("[/abs]", "rule 1 read must hold folder names"),
        ("[a/b]", "rule 1 read must hold folder names"),
        ("[.hidden]", "rule 1 read must hold folder names"),
        ("[-dash]", "rule 1 read must hold folder names"),
        ("[a, a]", "rule 1 read names a folder twice"),
        ("[1]", "rule 1 read item must be text"),
    ],
)
def test_bad_read_lists_are_refused(value: str, message: str) -> None:
    with pytest.raises(RulesError, match=message):
        rules_of(f"name: r, dm: true, scenario: a, read: {value}")

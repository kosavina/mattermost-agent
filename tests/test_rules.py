from pathlib import Path

import pytest
from helpers import make_post

from mattermost_agent.rules import (
    Bursts,
    Replies,
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
    rule_set = load_rules(example)
    assert rule_set.channel_names == frozenset({"analytics"})
    # The example spells out the defaults, and shows one rule of each kind of reply it documents.
    assert rule_set.replies == Replies()
    assert [rule_set.reply_mode(rule) for rule in rule_set.rules] == ["draft", "draft", "send"]


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


SEND_RULE = "name: r, dm: true, from: [example.user], scenario: a, reply: send"


def test_without_a_replies_section_every_rule_drafts() -> None:
    rule_set = rules_of("name: r, dm: true, scenario: a")
    assert rule_set.replies == Replies()
    assert rule_set.replies.default == "draft"
    assert rule_set.rules[0].reply is None
    assert rule_set.reply_mode(rule_set.rules[0]) == "draft"


def test_a_rule_can_choose_its_own_reply_mode() -> None:
    rule_set = rules_of(
        "name: quiet, channels: [a], scenario: a, reply: local",
        "name: plain, channels: [b], scenario: a",
        SEND_RULE,
        tail=LIMITS + BURSTS + "replies: {default: draft}\n",
    )
    assert [rule_set.reply_mode(rule) for rule in rule_set.rules] == ["local", "draft", "send"]


def test_the_replies_section_is_read() -> None:
    replies = (
        "replies: {default: local, sends_per_hour: 9, sends_per_chat_per_hour: 3, "
        'max_send_age_minutes: 10, label: "_From my assistant_"}\n'
    )
    rule_set = parse_rules(ONE_RULE + LIMITS + BURSTS + replies)
    assert rule_set.replies == Replies("local", 9, 3, 10, "_From my assistant_")
    assert rule_set.reply_mode(rule_set.rules[0]) == "local"


def test_keys_left_out_of_replies_keep_their_defaults() -> None:
    rule_set = parse_rules(ONE_RULE + LIMITS + BURSTS + "replies: {sends_per_hour: 1}\n")
    assert rule_set.replies == Replies(sends_per_hour=1)


@pytest.mark.parametrize("value", ["post", "Send", "true", "1", "[send]", "null", '""'])
def test_bad_reply_modes_are_refused(value: str) -> None:
    with pytest.raises(RulesError, match="rule 1 reply must be one of: local, draft, send"):
        rules_of(f"name: r, dm: true, from: [example.user], scenario: a, reply: {value}")
    with pytest.raises(RulesError, match="replies default must be one of: local, draft, send"):
        parse_rules(ONE_RULE + LIMITS + BURSTS + f"replies: {{default: {value}}}\n")


def test_a_send_rule_must_name_its_senders() -> None:
    with pytest.raises(
        RulesError, match="rule 2 replies with send, so it must name senders in from"
    ):
        rules_of(
            "name: first, dm: true, scenario: a",
            "name: second, channels: [analytics], scenario: a, reply: send",
        )


def test_a_send_default_needs_senders_on_every_rule_without_its_own_mode() -> None:
    tail = LIMITS + BURSTS + "replies: {default: send}\n"
    with pytest.raises(RulesError, match="rule 1 replies with send"):
        rules_of("name: r, dm: true, scenario: a", tail=tail)
    rule_set = rules_of(
        "name: named, dm: true, from: [example.user], scenario: a",
        "name: drafted, channels: [analytics], scenario: a, reply: draft",
        tail=tail,
    )
    assert [rule_set.reply_mode(rule) for rule in rule_set.rules] == ["send", "draft"]


@pytest.mark.parametrize(
    "key", ["sends_per_hour", "sends_per_chat_per_hour", "max_send_age_minutes"]
)
@pytest.mark.parametrize("value", BAD_NUMBERS)
def test_bad_reply_numbers_are_refused(key: str, value: str) -> None:
    with pytest.raises(RulesError, match=f"replies {key} must be a whole number"):
        parse_rules(ONE_RULE + LIMITS + BURSTS + f"replies: {{{key}: {value}}}\n")


@pytest.mark.parametrize(
    ("label", "message"),
    [
        ('""', "replies label must be text, and not empty"),
        ('"   "', "replies label must be text, and not empty"),
        ("7", "replies label must be text, and not empty"),
        ("null", "replies label must be text, and not empty"),
        (f'"{"x" * 201}"', "replies label must be one line of at most 200 characters"),
        ('"one\\ntwo"', "replies label must be one line of at most 200 characters"),
    ],
)
def test_bad_labels_are_refused(label: str, message: str) -> None:
    with pytest.raises(RulesError, match=message):
        parse_rules(ONE_RULE + LIMITS + BURSTS + f"replies: {{label: {label}}}\n")


def test_the_longest_label_is_allowed() -> None:
    replies = f'replies: {{label: "{"x" * 200}"}}\n'
    assert len(parse_rules(ONE_RULE + LIMITS + BURSTS + replies).replies.label) == 200


@pytest.mark.parametrize("replies", ["replies: 5\n", "replies: []\n", "replies: {other: 1}\n"])
def test_a_bad_replies_section_is_refused(replies: str) -> None:
    with pytest.raises(RulesError, match="replies"):
        parse_rules(ONE_RULE + LIMITS + BURSTS + replies)


def test_reply_errors_do_not_quote_the_file() -> None:
    with pytest.raises(RulesError) as error:
        rules_of("name: r, dm: true, scenario: a, reply: sentinel-private-mode")
    assert "sentinel-private-mode" not in str(error.value)
    with pytest.raises(RulesError) as error:
        parse_rules(ONE_RULE + LIMITS + BURSTS + f'replies: {{label: "{"sentinel" * 30}"}}\n')
    assert "sentinel" not in str(error.value)

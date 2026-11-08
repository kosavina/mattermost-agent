import pytest
from helpers import ME, make_post

from mattermost_agent.classify import classify, is_ignored


def mentioning_me() -> frozenset[str]:
    return frozenset({ME.user_id})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"channel_type": "D"}, "dm"),
        ({"channel_type": "G"}, "group_dm"),
        ({"channel_type": "O", "mentions": mentioning_me()}, "mention"),
        ({"channel_type": "P", "mentions": mentioning_me()}, "mention"),
        ({"channel_type": "O", "root_id": "root-1"}, "thread_reply"),
        ({"channel_type": "O"}, "channel"),
        ({"channel_type": "P"}, "channel"),
    ],
)
def test_kinds(fields: dict[str, object], expected: str) -> None:
    assert classify(make_post(**fields), ME) == expected


def test_a_dm_that_mentions_me_stays_a_dm() -> None:
    assert classify(make_post(channel_type="D", mentions=mentioning_me()), ME) == "dm"


def test_a_group_dm_reply_stays_a_group_dm() -> None:
    assert classify(make_post(channel_type="G", root_id="root-1"), ME) == "group_dm"


def test_a_reply_that_mentions_me_is_a_mention() -> None:
    post = make_post(root_id="root-1", mentions=mentioning_me())
    assert classify(post, ME) == "mention"


def test_someone_elses_mention_is_not_mine() -> None:
    assert classify(make_post(mentions=frozenset({"someone-else"})), ME) == "channel"


def test_the_server_list_wins_over_the_text() -> None:
    assert classify(make_post(message="@example.me hi", mentions=frozenset()), ME) == "channel"


@pytest.mark.parametrize(
    "message",
    [
        "@example.me hi",
        "hi @example.me",
        "hi @example.me, please",
        "hi @example.me.",
        "(@example.me)",
        "@EXAMPLE.ME hi",
        "first line\n@example.me second",
    ],
)
def test_text_mention_during_catch_up(message: str) -> None:
    assert classify(make_post(message=message, mentions=None), ME) == "mention"


@pytest.mark.parametrize(
    "message",
    [
        "no mention here",
        "example.me without the at sign",
        "@example.me2",
        "@example.me-too",
        "@example.me_too",
        "@example.me.too",
        "mail@example.me",
        "@@example.me",
        "@channel please",
        "@exampleXme",
    ],
)
def test_not_a_text_mention(message: str) -> None:
    assert classify(make_post(message=message, mentions=None), ME) == "channel"


def test_own_post_is_ignored() -> None:
    assert is_ignored(make_post(user_id=ME.user_id), ME)


def test_system_post_is_ignored() -> None:
    assert is_ignored(make_post(post_type="system_join_channel"), ME)


def test_deleted_post_is_ignored() -> None:
    assert is_ignored(make_post(deleted=True), ME)


def test_ordinary_post_is_kept() -> None:
    assert not is_ignored(make_post(), ME)


def test_other_post_types_are_kept() -> None:
    assert not is_ignored(make_post(post_type="slack_attachment"), ME)

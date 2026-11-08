import json
from typing import Any

import pytest
from helpers import make_post, posted_event, raw_post

from mattermost_agent.events import (
    Channel,
    MalformedEvent,
    Me,
    is_hello,
    parse_channel,
    parse_channel_list,
    parse_post_page,
    parse_user,
    parse_websocket_message,
)

SECRET = "sentinel-private-message"
CHANNEL = Channel(id="channel-1", name="analytics", type="O", last_post_at=5)


def parse(event: dict[str, Any]) -> Any:
    return parse_websocket_message(json.dumps(event))


def with_post_field(value: object) -> dict[str, Any]:
    """Return a posted event whose data.post is exactly `value`, not packed as JSON text."""
    event = posted_event()
    event["data"]["post"] = value
    return event


def test_posted_event() -> None:
    post = parse(posted_event(raw_post(message="hi", root_id="root-1", create_at=42)))
    assert post.id == "post-1"
    assert post.channel_type == "O"
    assert post.channel_name == "analytics"
    assert post.user_id == "other-id"
    assert post.root_id == "root-1"
    assert post.create_at == 42
    assert post.message == "hi"


def test_post_may_also_be_an_object() -> None:
    assert parse(with_post_field(raw_post())).id == "post-1"


def test_other_events_are_ignored() -> None:
    assert parse({"event": "typing", "data": {}}) is None
    assert parse({"status": "OK", "seq_reply": 1}) is None


def test_missing_mentions_field_means_no_mentions() -> None:
    assert parse(posted_event()).mentions == frozenset()


def test_mentions_as_json_text() -> None:
    assert parse(posted_event(mentions='["a", "b"]')).mentions == frozenset({"a", "b"})


def test_mentions_as_a_real_list() -> None:
    assert parse(posted_event(mentions=["a"])).mentions == frozenset({"a"})


def test_sender_name_is_not_read() -> None:
    assert not hasattr(parse(posted_event()), "sender_name")


@pytest.mark.parametrize(
    "event",
    [
        posted_event(post=raw_post(id="")),
        posted_event(post=raw_post(user_id=None)),
        posted_event(post=raw_post(create_at="soon")),
        posted_event(post=raw_post(create_at=True)),
        posted_event(post=raw_post(message=7)),
        posted_event(channel_type="X"),
        posted_event(channel_type=None),
        posted_event(mentions="not json"),
        posted_event(mentions='{"a": 1}'),
        posted_event(mentions="[1, 2]"),
        with_post_field("not json"),
        with_post_field("[1]"),
        with_post_field(None),
        {"event": "posted"},
        {"event": "posted", "data": []},
    ],
)
def test_malformed_events_are_refused(event: dict[str, Any]) -> None:
    with pytest.raises(MalformedEvent):
        parse(event)


@pytest.mark.parametrize("frame", ["not json", "[1, 2]", "7", "[" * 200_000])
def test_malformed_frames_are_refused(frame: str) -> None:
    with pytest.raises(MalformedEvent):
        parse_websocket_message(frame)


def test_errors_do_not_quote_the_message() -> None:
    with pytest.raises(MalformedEvent) as error:
        parse(posted_event(post=raw_post(id=SECRET, create_at=SECRET)))
    assert SECRET not in str(error.value)
    with pytest.raises(MalformedEvent) as error:
        parse_websocket_message(SECRET)
    assert SECRET not in str(error.value)
    assert error.value.__suppress_context__


def test_repr_leaves_out_the_text() -> None:
    assert SECRET not in repr(make_post(message=SECRET))


def test_user() -> None:
    assert parse_user({"id": "u", "username": "example.user", "email": "x"}) == Me(
        user_id="u", username="example.user"
    )


@pytest.mark.parametrize("body", [{}, [], {"id": "u"}, {"id": "u", "username": ""}])
def test_malformed_user_is_refused(body: object) -> None:
    with pytest.raises(MalformedEvent):
        parse_user(body)


def test_hello_is_recognised() -> None:
    assert is_hello(json.dumps({"event": "hello", "data": {}}))
    assert not is_hello(json.dumps({"event": "posted", "data": {}}))
    assert not is_hello(json.dumps({"status": "OK"}))


def test_channel_list() -> None:
    row = {"id": "c", "name": "n", "type": "D"}
    assert [channel.id for channel in parse_channel_list([row])] == ["c"]


@pytest.mark.parametrize("body", [{}, "text", [[]]])
def test_malformed_channel_list_is_refused(body: object) -> None:
    with pytest.raises(MalformedEvent):
        parse_channel_list(body)


def test_channel_row() -> None:
    row = {"id": "c", "name": "general", "type": "P", "last_post_at": 9, "extra": "ignored"}
    assert parse_channel(row) == Channel(id="c", name="general", type="P", last_post_at=9)


def test_channel_without_posts_has_time_zero() -> None:
    assert parse_channel({"id": "c", "name": "n", "type": "D"}).last_post_at == 0


@pytest.mark.parametrize("row", [{}, [], {"id": "c", "name": "n", "type": "X"}])
def test_malformed_channel_is_refused(row: object) -> None:
    with pytest.raises(MalformedEvent):
        parse_channel(row)


def test_rest_page_keeps_the_server_order_and_takes_the_channel() -> None:
    body = {
        "order": ["new", "old"],
        "posts": {"old": raw_post(id="old"), "new": raw_post(id="new")},
    }
    posts = parse_post_page(body, CHANNEL)
    assert [post.id for post in posts] == ["new", "old"]
    assert posts[0].channel_name == "analytics"
    assert posts[0].channel_type == "O"
    assert posts[0].mentions is None


def test_empty_rest_page() -> None:
    assert parse_post_page({"order": [], "posts": {}}, CHANNEL) == []


def test_deleted_rest_post_is_marked() -> None:
    body = {"order": ["a"], "posts": {"a": raw_post(id="a", delete_at=5)}}
    assert parse_post_page(body, CHANNEL)[0].deleted


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"order": ["a"], "posts": {}},
        {"order": "a", "posts": {"a": raw_post()}},
        {"order": [1], "posts": {}},
        {"posts": {}},
        {"order": []},
    ],
)
def test_malformed_rest_page_is_refused(body: object) -> None:
    with pytest.raises(MalformedEvent):
        parse_post_page(body, CHANNEL)

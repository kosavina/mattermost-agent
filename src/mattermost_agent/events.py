"""Turn what Mattermost sends into one Post, whichever way it arrived.

A live post comes over the WebSocket, packed as JSON text inside the event, with the channel's
name and type and the list of mentioned users alongside it. A post fetched during catch-up comes
from REST without any of those, so that parser takes the channel as an argument.

Everything here comes from the server, so nothing is trusted, and errors name the field without
quoting its value: the value could be somebody's message.

The event's sender_name is ignored on purpose. A webhook can set it to any name, so the sender is
looked up from the post's user_id instead.

Of a post's props, one thing is kept: whether they carry the marker our own code puts on a reply
it posts. The props themselves are never stored, so nothing of them can reach a log.
"""

import json
from dataclasses import dataclass, field
from typing import cast

CHANNEL_TYPES = frozenset({"D", "G", "O", "P"})
# The prop on every post our code creates. Anyone can set a prop, so it counts only on a post of
# the listener's own account.
AGENT_PROP = "from_mattermost_agent"


class MalformedEvent(Exception):
    """The server sent something unexpected. The message names the field only."""


class UnknownChannelType(MalformedEvent):
    """A channel of a type this code does not know, perhaps from a newer server."""


@dataclass(frozen=True)
class Me:
    """The account the listener runs as."""

    user_id: str
    username: str


@dataclass(frozen=True)
class Channel:
    """One row of the channel listing. Type D is a DM and G a group DM, which have no team."""

    id: str
    name: str
    type: str
    last_post_at: int
    team_id: str = ""


@dataclass(frozen=True)
class ChatDraft:
    """A draft in a message box, as the server lists it. repr() leaves out the text."""

    channel_id: str
    root_id: str
    message: str = field(repr=False)


@dataclass(frozen=True)
class Post:
    """One post. Times are Unix milliseconds. repr() leaves out the text.

    `mentions` holds the mentioned user ids, or None when the server did not say, as in REST.
    `from_agent` says that the props carry our marker; whose post it is, is a separate question.
    """

    id: str
    channel_id: str
    channel_type: str
    channel_name: str
    user_id: str
    root_id: str
    create_at: int
    post_type: str
    deleted: bool
    message: str = field(repr=False)
    mentions: frozenset[str] | None
    from_agent: bool = False


def _decoded(value: object, what: str) -> object:
    """Unpack a field that Mattermost sends as JSON text. Anything else is returned as it is."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError, RecursionError:
        raise MalformedEvent(f"{what} is not valid JSON") from None


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MalformedEvent(f"{what} is not an object")
    return cast(dict[str, object], value)


def _text_list(value: object, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MalformedEvent(f"{what} is not a list of text")
    return cast(list[str], value)


def _text(container: dict[str, object], key: str, default: str | None = None) -> str:
    """Return a text field. Without a default, it must be present and not empty."""
    value = container.get(key, default)
    if not isinstance(value, str) or (default is None and not value):
        raise MalformedEvent(f"{key} is missing or not text")
    return value


def _number(container: dict[str, object], key: str, default: int | None = None) -> int:
    value = container.get(key, default)
    # bool is a kind of int in Python, and JSON true must not pass as a time.
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedEvent(f"{key} is missing or not a number")
    return value


def dm_partner(channel_name: str, my_id: str) -> str | None:
    """Return the other user's id from a DM's name, `id__id`, or None if the name is unusual.

    In a DM to myself, both ids are mine.
    """
    ids = channel_name.split("__")
    if len(ids) != 2 or not all(ids) or my_id not in ids:
        return None
    return ids[1] if ids[0] == my_id else ids[0]


def _channel_type(container: dict[str, object], key: str) -> str:
    value = _text(container, key)
    if value not in CHANNEL_TYPES:
        raise UnknownChannelType(f"{key} is not a known channel type")
    return value


def _has_marker(props: object) -> bool:
    """Say whether a post's props carry our marker: the JSON value true, and nothing like it."""
    return isinstance(props, dict) and props.get(AGENT_PROP) is True


def _post(
    raw: dict[str, object],
    channel_type: str,
    channel_name: str,
    mentions: frozenset[str] | None,
) -> Post:
    return Post(
        id=_text(raw, "id"),
        channel_id=_text(raw, "channel_id"),
        channel_type=channel_type,
        channel_name=channel_name,
        user_id=_text(raw, "user_id"),
        root_id=_text(raw, "root_id", ""),
        create_at=_number(raw, "create_at"),
        post_type=_text(raw, "type", ""),
        deleted=_number(raw, "delete_at", 0) != 0,
        message=_text(raw, "message", ""),
        mentions=mentions,
        from_agent=_has_marker(raw.get("props")),
    )


def _mentions(data: dict[str, object]) -> frozenset[str]:
    """Return the mentioned user ids. The server leaves the field out when there are none."""
    raw = data.get("mentions")
    if raw is None:
        return frozenset()
    return frozenset(_text_list(_decoded(raw, "mentions"), "mentions"))


def parse_websocket_message(frame: object) -> Post | None:
    """Return the post of a `posted` event, None for any other event, or raise MalformedEvent."""
    event = _mapping(_decoded(frame, "event"), "event")
    if event.get("event") != "posted":
        return None
    data = _mapping(event.get("data"), "data")
    return _post(
        _mapping(_decoded(data.get("post"), "post"), "post"),
        channel_type=_channel_type(data, "channel_type"),
        channel_name=_text(data, "channel_name", ""),
        mentions=_mentions(data),
    )


def parse_channel(raw: object) -> Channel:
    """Return one row of the channel listing."""
    channel = _mapping(raw, "channel")
    return Channel(
        id=_text(channel, "id"),
        name=_text(channel, "name"),
        type=_channel_type(channel, "type"),
        last_post_at=_number(channel, "last_post_at", 0),
        team_id=_text(channel, "team_id", ""),
    )


def parse_channel_list(body: object) -> list[Channel]:
    """Return the channel listing, leaving out channels of a type this code does not know."""
    if not isinstance(body, list):
        raise MalformedEvent("channel list is not a list")
    channels: list[Channel] = []
    for row in body:
        try:
            channels.append(parse_channel(row))
        except UnknownChannelType:
            continue
    return channels


def parse_user(body: object) -> Me:
    """Return the id and username of one user."""
    user = _mapping(body, "user")
    return Me(user_id=_text(user, "id"), username=_text(user, "username"))


def is_hello(frame: object) -> bool:
    """Say whether a WebSocket frame is the server's greeting."""
    return _mapping(_decoded(frame, "event"), "event").get("event") == "hello"


def parse_post_page(body: object, channel: Channel) -> list[Post]:
    """Return the posts of one REST page, in the server's order: newest first."""
    page = _mapping(body, "page")
    posts = _mapping(page.get("posts"), "posts")
    order = _text_list(page.get("order"), "order")
    if any(post_id not in posts for post_id in order):
        raise MalformedEvent("order names a post that is not in posts")
    return [
        _post(
            _mapping(posts[post_id], "post"),
            channel_type=channel.type,
            channel_name=channel.name,
            mentions=None,
        )
        for post_id in order
    ]


def parse_drafts(body: object) -> list[ChatDraft]:
    """Return the drafts of one listing. The server answers `null` when there are none."""
    if body is None:
        return []
    if not isinstance(body, list):
        raise MalformedEvent("draft list is not a list")
    drafts: list[ChatDraft] = []
    for row in body:
        draft = _mapping(row, "draft")
        drafts.append(
            ChatDraft(
                channel_id=_text(draft, "channel_id"),
                root_id=_text(draft, "root_id", ""),
                message=_text(draft, "message", ""),
            )
        )
    return drafts


def parse_created_post(body: object) -> str:
    """Return the id of the post the server says it created."""
    return _text(_mapping(body, "post"), "id")

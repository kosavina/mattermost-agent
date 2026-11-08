"""Fake Mattermost data for the tests. All names and ids are made up."""

import json
from typing import Any

from mattermost_agent.events import Me, Post

ME = Me(user_id="me-id", username="example.me")


def raw_post(**fields: Any) -> dict[str, Any]:
    """Return a post as the server sends it, with the fields that matter here."""
    return {
        "id": "post-1",
        "channel_id": "channel-1",
        "user_id": "other-id",
        "root_id": "",
        "create_at": 1_000,
        "delete_at": 0,
        "type": "",
        "message": "hello",
        **fields,
    }


def posted_event(post: dict[str, Any] | None = None, **data: Any) -> dict[str, Any]:
    """Return a `posted` event, with the post packed as JSON text as the server does it."""
    return {
        "event": "posted",
        "seq": 7,
        "broadcast": {"channel_id": "channel-1"},
        "data": {
            "channel_type": "O",
            "channel_name": "analytics",
            "sender_name": "@spoofable",
            "post": json.dumps(raw_post() if post is None else post),
            **data,
        },
    }


def make_post(**fields: Any) -> Post:
    values: dict[str, Any] = {
        "id": "post-1",
        "channel_id": "channel-1",
        "channel_type": "O",
        "channel_name": "analytics",
        "user_id": "other-id",
        "root_id": "",
        "create_at": 1_000,
        "post_type": "",
        "deleted": False,
        "message": "hello",
        "mentions": frozenset(),
        **fields,
    }
    return Post(**values)

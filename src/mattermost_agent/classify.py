"""Decide which posts to look at, and what kind each one is."""

import re
from typing import Literal, get_args

from mattermost_agent.events import Me, Post

type Kind = Literal["dm", "group_dm", "mention", "thread_reply", "channel"]
KINDS: frozenset[str] = frozenset(get_args(Kind.__value__))


def is_ignored(post: Post, me: Me) -> bool:
    """Skip my own posts, posts already deleted, and "X joined the channel" notices."""
    return post.user_id == me.user_id or post.deleted or post.post_type.startswith("system_")


def _text_mentions(message: str, username: str) -> bool:
    """Look for @username in the text. Usernames may hold . - and _, so a longer name differs."""
    pattern = rf"(?<![\w.@-])@{re.escape(username)}(?![\w-]|\.\w)"
    return re.search(pattern, message, re.IGNORECASE) is not None


def _mentions_me(post: Post, me: Me) -> bool:
    """Trust the server's list when there is one. Catch-up has none, so it reads the text.

    The text check finds only @username, not @channel, @all or personal mention keywords.
    """
    if post.mentions is None:
        return _text_mentions(post.message, me.username)
    return me.user_id in post.mentions


def classify(post: Post, me: Me) -> Kind:
    """Return the post's kind. A DM stays a DM, and a reply that mentions me is a mention."""
    if post.channel_type == "D":
        return "dm"
    if post.channel_type == "G":
        return "group_dm"
    if _mentions_me(post, me):
        return "mention"
    if post.root_id:
        return "thread_reply"
    return "channel"

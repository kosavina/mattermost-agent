"""Decide how one reply may be delivered: our checks on the agent's text, as pure functions.

The agent only returns text. This code decides where the text goes and whether it may be posted
without Kirill reading it first. Config sets the mode; nothing in a message or in the agent's
answer can move it towards `send`, only towards `draft`. The processor plans with these checks
before it queues a reply, and the listener runs the content checks again before it posts, so a
reply is posted directly only if both agree.

A `local` reply never comes here: it waits on the terminal and reaches no message box.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from mattermost_agent.rules import DeliveryMode
from mattermost_agent.store import Job, JobPost

MAX_SEND_CHARS = 1_500
# Kinds of a burst's first post that mean a DM or a group DM, where a reply stays in place.
DIRECT_KINDS = frozenset({"dm", "group_dm"})
EVERYONE = frozenset({"channel", "all", "here"})
# Markdown links and images, any address with a scheme, and bare web addresses.
LINK_MARKS = ("://", "www.", "](", "![")
# An @ and the username characters after it. A sentence's full stop is not part of the name.
MENTION = re.compile(r"@([a-z0-9._-]*)(.?)", re.IGNORECASE | re.DOTALL)

type Downgrade = Literal["needs_review", "length", "mention", "link"]


@dataclass(frozen=True)
class Plan:
    """What to do with one reply. repr() leaves out the text.

    `root_id` is the thread the reply goes into, or empty for the chat itself. `refused` means
    the text must reach no message box at all; it stays readable with the `drafts` command.
    """

    mode: DeliveryMode
    root_id: str
    message: str = field(repr=False)
    downgrade: Downgrade | None = None
    refused: bool = False


def is_command(message: str) -> bool:
    """Say whether pressing Enter on this text in a message box would run a slash command."""
    return message.lstrip().startswith("/")


def _mentions_someone_else(message: str, sender: str | None) -> bool:
    allowed = None if sender is None else sender.casefold()
    for match in MENTION.finditer(message):
        name = match[1].rstrip("._-").casefold()
        if not name:
            # `@` before a letter outside the username alphabet: not a name we can compare.
            if match[2].isalnum():
                return True
            continue
        if name in EVERYONE or name != allowed:
            return True
    return False


def content_downgrade(message: str, sender: str | None) -> Downgrade | None:
    """Return why this text must not be posted without review, or None if it may be.

    Too long, an @mention of anyone but the burst's sender (so also @channel, @all and @here), or
    anything that looks like a link or an image.
    """
    if len(message) > MAX_SEND_CHARS:
        return "length"
    if _mentions_someone_else(message, sender):
        return "mention"
    lowered = message.casefold()
    if any(mark in lowered for mark in LINK_MARKS):
        return "link"
    return None


def plan_reply(
    mode: DeliveryMode, job: Job, first: JobPost, draft: str, *, needs_review: bool
) -> Plan:
    """Return where the agent's draft goes and whether it may still be posted directly.

    In a channel the reply always goes into a thread: the burst's own, or one started on the
    burst's first post. In a DM or group DM it goes where the burst was.
    """
    root_id = job.root_id if first.kind in DIRECT_KINDS else job.root_id or first.post_id
    if is_command(draft):
        return Plan("draft", root_id, draft, refused=True)
    if mode == "draft":
        return Plan("draft", root_id, draft)
    downgrade: Downgrade | None = (
        "needs_review" if needs_review else content_downgrade(draft, job.sender)
    )
    return Plan("send" if downgrade is None else "draft", root_id, draft, downgrade)

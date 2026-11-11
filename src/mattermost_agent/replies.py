"""Decide how one reply may be delivered: our checks on the agent's text, as pure functions.

The agent only returns text. This code decides where the text goes and whether it may be posted
without Kirill reading it first. Config sets the mode; nothing in a message or in the agent's
answer can move it towards `send`, only towards `draft`. The processor plans with these checks
before it queues a reply, and the listener runs the content checks again before it posts, so a
reply is posted directly only if both agree.

A `local` reply never comes here: it waits on the terminal and reaches no message box.

The content checks allow little and ask no questions about what Mattermost would make of the
rest. Mattermost reads a message as markdown, splits words in its own way to find mentions, and
may decode or drop characters first, so "this is not a mention" cannot be decided from here.
What can be decided is "this is plain text with at most the sender's own name after an @". A
text that is anything else is not wrong; it becomes a draft, which costs one press of Enter.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from mattermost_agent.rules import DeliveryMode
from mattermost_agent.store import Job, JobPost

MAX_SEND_CHARS = 1_500
# Kinds of a burst's first post that mean a DM or a group DM, where a reply stays in place.
DIRECT_KINDS = frozenset({"dm", "group_dm"})
# The characters of a Mattermost username, as many as follow an @.
NAME = re.compile(r"[a-z0-9._-]*", re.IGNORECASE)
# What a link or an image needs: an address with a scheme or without one (`//host`), a bare web
# address, or the brackets of markdown links (inline and by reference) and of autolinks.
LINK_MARKS = ("//", "www.", "[", "]", "<", ">")
# What could make Mattermost read something else than these checks do: a character reference
# such as &#64; for @, a backslash escape, and a code fence, which would swallow the label.
ENTITY = re.compile(r"&#?\w+;")
MARKUP_MARKS = ("\\", "```", "~~~")
# Characters nobody sees: format and control characters, private use and unassigned ones.
INVISIBLE = frozenset({"Cf", "Cc", "Co", "Cn"})
KEPT_CONTROLS = frozenset("\n\t")

type Downgrade = Literal["needs_review", "length", "mention", "link", "markup"]


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


def _is_invisible(character: str) -> bool:
    return unicodedata.category(character) in INVISIBLE and character not in KEPT_CONTROLS


def is_command(message: str) -> bool:
    """Say whether pressing Enter on this text in a message box would run a slash command.

    The first character that shows decides, in case the server or the app drops what does not.
    """
    for character in message:
        if character.isspace() or _is_invisible(character):
            continue
        return character == "/"
    return False


def _has_another_at(message: str, sender: str | None) -> bool:
    """Say whether any @ in the text is something else than exactly `@sender`.

    Every @ is looked at by itself. The name is all the username characters after it, with
    nothing taken off: `@ivan.` and `@ivan_` are other names than `@ivan`. Right after the name
    there may be no letter, digit or further @, which Mattermost could join to it or split off.
    """
    allowed = sender.casefold() if sender else None
    for at in re.finditer("@", message):
        name = NAME.match(message, at.end())
        end = at.end() if name is None else name.end()
        following = message[end : end + 1]
        if allowed is None or message[at.end() : end].casefold() != allowed:
            return True
        if following == "@" or following.isalnum():
            return True
    return False


def _has_markup(message: str) -> bool:
    if any(mark in message for mark in MARKUP_MARKS) or ENTITY.search(message) is not None:
        return True
    return any(_is_invisible(character) for character in message)


def content_downgrade(message: str, sender: str | None) -> Downgrade | None:
    """Return why this text must not be posted without review, or None if it may be.

    Too long; an @ that is not exactly the burst's sender's name (so also @channel, @all, @here
    and every e-mail address); anything a link or an image needs; or markup and invisible
    characters that could make Mattermost read the text differently than these checks do.
    """
    if len(message) > MAX_SEND_CHARS:
        return "length"
    if _has_another_at(message, sender):
        return "mention"
    lowered = message.casefold()
    if any(mark in lowered for mark in LINK_MARKS):
        return "link"
    if _has_markup(message):
        return "markup"
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

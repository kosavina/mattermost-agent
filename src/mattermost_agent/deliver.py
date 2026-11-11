"""Deliver the replies in the outbox to Mattermost: a draft in a message box, or a labelled post.

This is the only module that decides to write to Mattermost, and the only one that imports the
writer. The processor queued each reply with its text and a mode; nothing here trusts that row
further than it must. Every doubt ends in a draft, never in silence and never in a post.

A direct reply (`send`) must pass, in this order: the listener's own rules still mark the job's
rule as `send`; the content checks, run again; the burst is not Kirill's own; the kill switch
(`ALLOW_SEND=1`); the burst is not too old; the caps. The first check that fails turns it into a
draft and is recorded as `downgrade`. Then, in both modes: if Kirill has already answered there,
nothing is written.

A draft is never written over another one. An API upsert replaces what is in the message box at
once, so a non-empty draft in that place makes the reply wait, a minute at a time, for up to
three days.

At most one POST is ever started per reply. The store marks it first (begin_send, a
compare-and-set), and the request is not retried. If the answer never arrives, the reply stays
`delivering` and only reading the chat settles it: our post is there (`sent`), or it is not, and
then the reply becomes a draft. A crash at any point therefore cannot post twice.

An outage only delays: the reply goes back to wait. Only a rejected token or a broken store ends
the listener from here.
"""

import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus

from mattermost_agent.app import Runtime, is_passing
from mattermost_agent.events import Channel, ChatDraft, Me
from mattermost_agent.logs import (
    log_draft_busy,
    log_lifecycle,
    log_reply_delivered,
    log_reply_waiting,
)
from mattermost_agent.mattermost import MattermostError
from mattermost_agent.mattermost_write import MattermostWriter, WriteRefused
from mattermost_agent.replies import content_downgrade, is_command
from mattermost_agent.store import Delivery, Outgoing

DELIVER_SECONDS = 3.0
# How long a reply waits before the next try: its message box is busy, or the server is not there.
BUSY_RETRY_SECONDS = 60.0
OUTAGE_RETRY_SECONDS = 20.0
# A POST without an answer may still arrive at the server. Look for its post only after this long.
CONFIRM_AFTER_SECONDS = 60.0
# The burst's time is the server's and "now" is ours. A burst from further ahead than this is
# not believed, and its reply is not sent directly.
FUTURE_SLACK_MS = 5 * 60 * 1000
MS_PER_MINUTE = 60_000


@dataclass
class _Reply:
    """One reply while it is handled: what the row said, and what the checks have made of it."""

    item: Outgoing
    mode: str
    downgrade: str | None


@dataclass
class Deliverer:
    """Hands the outbox's replies to Mattermost. `allow_send` is the kill switch."""

    writer: MattermostWriter
    allow_send: bool
    channels: dict[str, Channel] = field(default_factory=dict)

    async def aclose(self) -> None:
        await self.writer.aclose()

    async def run(self, runtime: Runtime, me: Me) -> None:
        """Deliver until cancelled, every due reply, oldest first, then look again shortly."""
        while True:
            while await self.once(runtime, me):
                # One reply is several requests. Give the socket and the heartbeat a turn.
                await asyncio.sleep(0)
            await asyncio.sleep(DELIVER_SECONDS)

    async def once(self, runtime: Runtime, me: Me) -> bool:
        """Settle what earlier runs left open, then deliver one reply. Say whether one was due."""
        store, now = runtime.store, runtime.clock()
        for row in store.expire_outbox(now):
            log_reply_delivered(
                job_id=row.job_id,
                mode=row.mode,
                outcome="expired",
                downgrade=row.downgrade,
                reason=row.reason,
                channel_id=row.channel_id,
            )
        store.release_outbox(now)
        for item in store.unconfirmed_sends(now - CONFIRM_AFTER_SECONDS):
            await self._confirm(runtime, me, item)

        item = store.take_outbox(now)
        if item is None:
            return False
        reply = _Reply(item, item.row.mode, item.row.downgrade)
        try:
            await self._deliver(runtime, me, reply)
        except MattermostError as error:
            if error.reason == "auth_rejected":
                raise
            self._after_error(runtime, reply, error)
        except Exception as error:
            # A bug must not stop the listener, and must not be tried on this reply for ever.
            log_lifecycle("internal_error", error=error)
            self._finish(runtime, reply, "failed", reason="internal_error")
        return True

    async def _deliver(self, runtime: Runtime, me: Me, reply: _Reply) -> None:
        row = reply.item.row
        if is_command(reply.item.text):
            # The processor never queues one. If one is here all the same, it reaches no box.
            self._finish(runtime, reply, "refused_command")
            return
        if reply.mode == "send":
            self._downgrade(runtime, reply, self._send_check(runtime, me, reply.item))
        channel = await self._channel(runtime, row.channel_id)
        if channel is None:
            self._finish(runtime, reply, "failed", reason="no_channel")
            return
        answered = await self._answered(runtime, me, channel, reply.item)
        if answered:
            self._finish(runtime, reply, "skipped_answered")
            return
        if answered is None and reply.mode == "send":
            # The chat could not be read to its end, so "not answered" is not known.
            self._downgrade(runtime, reply, "stale")
        if reply.mode == "send" and await self._post(runtime, reply):
            return
        await self._draft(runtime, me, channel, reply)

    def _send_check(self, runtime: Runtime, me: Me, item: Outgoing) -> str | None:
        """Return the first reason not to post this reply directly, or None."""
        row, rules = item.row, runtime.rules
        rule = next((known for known in rules.rules if known.name == item.rule), None)
        if rule is None or rules.reply_mode(rule) != "send":
            return "rule"
        if row.channel_id != item.job_channel_id:
            return "rule"
        content = content_downgrade(item.text, item.sender)
        if content is not None:
            return content
        if item.sender_id == me.user_id:
            return "own_burst"
        if not self.allow_send:
            return "kill_switch"
        age_ms = int(runtime.clock() * 1000) - row.last_post_at
        if age_ms > rules.replies.max_send_age_minutes * MS_PER_MINUTE or age_ms < -FUTURE_SLACK_MS:
            return "stale"
        return None

    def _downgrade(self, runtime: Runtime, reply: _Reply, downgrade: str | None) -> None:
        if downgrade is None:
            return
        runtime.store.downgrade_outbox(reply.item.row.job_id, downgrade, runtime.clock())
        reply.mode, reply.downgrade = "draft", downgrade

    async def _channel(self, runtime: Runtime, channel_id: str) -> Channel | None:
        """Return the channel as the server lists it, asking again if it is not known yet."""
        if channel_id not in self.channels:
            listed = await runtime.client.list_channels()
            self.channels = {channel.id: channel for channel in listed}
        return self.channels.get(channel_id)

    async def _answered(
        self, runtime: Runtime, me: Me, channel: Channel, item: Outgoing
    ) -> bool | None:
        """Say whether Kirill has posted in that place since the burst. None: it is not known.

        The place is the reply's thread, or the whole chat for a reply that goes into the chat
        itself. A post of ours with the marker is not Kirill's answer.
        """
        row = item.row
        result = await runtime.client.posts_since(channel, row.last_post_at)
        for post in result.posts:
            mine = post.user_id == me.user_id and not post.from_agent and not post.deleted
            if mine and (not row.root_id or post.root_id == row.root_id):
                return True
        return None if result.truncated else False

    async def _post(self, runtime: Runtime, reply: _Reply) -> bool:
        """Start the reply's one POST. Return False if the reply became a draft instead."""
        row, replies = reply.item.row, runtime.rules.replies
        verdict = runtime.store.begin_send(
            row.job_id,
            now=runtime.clock(),
            per_hour=replies.sends_per_hour,
            per_chat=replies.sends_per_chat_per_hour,
        )
        if verdict != "go":
            self._downgrade(runtime, reply, verdict)
            return False
        try:
            # The label is added here and nowhere else, so that no draft ever carries it.
            message = f"{reply.item.text}\n\n{replies.label}"
            post_id = await self.writer.create_post(row.channel_id, row.root_id, message)
        except WriteRefused:
            self._downgrade(runtime, reply, "kill_switch")
            return False
        except MattermostError as error:
            if error.reason == "auth_rejected":
                raise
            refused = (
                HTTPStatus.BAD_REQUEST <= (error.status or 0) < HTTPStatus.INTERNAL_SERVER_ERROR
            )
            if refused:
                # The server answered, and created nothing.
                self._downgrade(runtime, reply, "refused")
                return False
            # No answer, or a broken one: the post may exist. It is never tried again; the row
            # stays `delivering` until reading the chat settles it.
            log_reply_waiting(
                job_id=row.job_id,
                reason="unconfirmed",
                status=error.status,
                channel_id=row.channel_id,
            )
            return True
        self._finish(runtime, reply, "sent", post_id=post_id)
        return True

    async def _draft(self, runtime: Runtime, me: Me, channel: Channel, reply: _Reply) -> None:
        """Put the reply into the message box, unless a draft is already there."""
        row, text = reply.item.row, reply.item.text
        # A DM has no team, and the server lists its drafts under the user's teams.
        teams = [channel.team_id] if channel.team_id else self._teams()
        if not teams:
            self._wait(runtime, reply, "no_team", None, BUSY_RETRY_SECONDS)
            return
        existing: ChatDraft | None = None
        for team_id in teams:
            for draft in await self.writer.list_drafts(me.user_id, team_id):
                same_place = (draft.channel_id, draft.root_id) == (row.channel_id, row.root_id)
                if same_place and not draft.is_empty:
                    existing = draft
            if existing is not None:
                break
        if existing is not None and (existing.message != text or existing.has_files):
            self._wait(runtime, reply, "draft_busy", None, BUSY_RETRY_SECONDS)
            return
        if existing is None:
            now_ms = int(runtime.clock() * 1000)
            await self.writer.upsert_draft(row.channel_id, row.root_id, text, now_ms)
        # Otherwise our own draft is already there: a crash came between writing and recording.
        self._finish(runtime, reply, "drafted")

    def _teams(self) -> list[str]:
        return sorted({channel.team_id for channel in self.channels.values() if channel.team_id})

    async def _confirm(self, runtime: Runtime, me: Me, item: Outgoing) -> None:
        """Settle a direct reply whose POST was started and never answered, by reading the chat."""
        row, now = item.row, runtime.clock()
        reply = _Reply(item, "send", row.downgrade)
        try:
            channel = await self._channel(runtime, row.channel_id)
            if channel is None:
                self._finish(runtime, reply, "failed", reason="unconfirmed")
                return
            result = await runtime.client.posts_since(channel, row.last_post_at)
        except MattermostError as error:
            if error.reason == "auth_rejected":
                raise
            return
        ours = item.text.strip()
        for post in result.posts:
            marked = post.from_agent and post.user_id == me.user_id
            if marked and post.root_id == row.root_id and post.message.strip().startswith(ours):
                self._finish(runtime, reply, "sent", post_id=post.id)
                return
        if result.truncated:
            return
        runtime.store.downgrade_outbox(row.job_id, "unconfirmed", now, requeue=True)

    def _after_error(self, runtime: Runtime, reply: _Reply, error: MattermostError) -> None:
        if error.status == HTTPStatus.NOT_IMPLEMENTED:
            # The server has synced drafts switched off. Waiting would not change that.
            self._finish(runtime, reply, "failed", reason="drafts_disabled")
        elif is_passing(error):
            self._wait(runtime, reply, "unreachable", error.status, OUTAGE_RETRY_SECONDS)
        else:
            self._finish(runtime, reply, "failed", reason=error.reason)

    def _wait(
        self, runtime: Runtime, reply: _Reply, reason: str, status: int | None, seconds: float
    ) -> None:
        row, now = reply.item.row, runtime.clock()
        news = runtime.store.postpone_outbox(
            row.job_id, reason=reason, retry_at=now + seconds, now=now
        )
        if not news:
            return
        if reason == "draft_busy":
            log_draft_busy(job_id=row.job_id, channel_id=row.channel_id)
        else:
            log_reply_waiting(
                job_id=row.job_id, reason=reason, status=status, channel_id=row.channel_id
            )

    def _finish(
        self,
        runtime: Runtime,
        reply: _Reply,
        outcome: Delivery,
        *,
        post_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        row = reply.item.row
        runtime.store.finish_outbox(
            row.job_id, outcome=outcome, now=runtime.clock(), post_id=post_id, reason=reason
        )
        log_reply_delivered(
            job_id=row.job_id,
            mode=reply.mode,
            outcome=outcome,
            downgrade=reply.downgrade,
            reason=reason,
            channel_id=row.channel_id,
        )


def build(base_url: str, token: str, *, allow_send: bool) -> Deliverer:
    """Return the deliverer for the listener. The kill switch is set twice from the one setting:
    on the checks here, and on the writer, which cannot create a post without it."""
    writer = MattermostWriter(base_url, token, allow_post=allow_send)
    return Deliverer(writer, allow_send=allow_send)

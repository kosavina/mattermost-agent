"""The deliver loop against a fake Mattermost: a real store, fake REST transports, one pass at a
time. Every test starts from an outbox row written the way the processor writes it."""

import asyncio
import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from helpers import ME, captured_logs, make_post, squeezed

from mattermost_agent import app, deliver
from mattermost_agent.deliver import (
    BUSY_RETRY_SECONDS,
    CONFIRM_AFTER_SECONDS,
    OUTAGE_RETRY_SECONDS,
    Deliverer,
)
from mattermost_agent.events import AGENT_PROP
from mattermost_agent.mattermost import MattermostClient, MattermostError
from mattermost_agent.mattermost_write import MattermostWriter
from mattermost_agent.replies import plan_reply
from mattermost_agent.rules import Bursts, DeliveryMode, parse_rules
from mattermost_agent.store import (
    DRAFT_RETENTION_SECONDS,
    FILE_NAME,
    OutboxRow,
    Store,
    read_jobs,
    read_outbox,
    read_undelivered,
)

URL = "https://chat.example.com"
TOKEN = "abcdefghijklmnopqrstuvwxyz"
NOW = 2_000_000_000.0
NOW_MS = 2_000_000_000_000
TEAM = "team-1"
DM = "dm-with-ivan"
TOWN = "town-channel"
LABEL = "_Auto-reply_"
CANARY = "reply-canary-KESTRELFERN"
RULES = f"""
rules:
  - {{name: dm, dm: true, scenario: intake}}
  - {{name: trusted, dm: true, from: [example.user, example.me], scenario: intake, reply: send}}
limits: {{jobs_per_hour: 100}}
bursts: {{quiet_seconds: 90, max_wait_seconds: 600, max_posts: 1}}
replies:
  sends_per_hour: 3
  sends_per_chat_per_hour: 2
  max_send_age_minutes: 30
  label: "{LABEL}"
"""
ONE_POST = Bursts(quiet_seconds=90, max_wait_seconds=600, max_posts=1)


class LostAnswer(Exception):
    """The server did what was asked, and the answer never arrived."""


class Server:
    """A fake Mattermost: channels, posts and drafts in memory, and every request remembered.

    `failures` holds, per kind of call, what to answer instead, in turn: a status, an exception
    to raise before anything is done, or LostAnswer to do it and then drop the connection.
    """

    def __init__(self) -> None:
        self.channels: list[dict[str, Any]] = [
            {"id": DM, "name": "me-id__other-id", "type": "D", "last_post_at": 0, "team_id": ""},
            {"id": TOWN, "name": "town", "type": "O", "last_post_at": 0, "team_id": TEAM},
        ]
        self.posts: dict[str, list[dict[str, Any]]] = {}
        self.drafts: list[dict[str, Any]] = []
        self.requests: list[httpx2.Request] = []
        self.failures: dict[str, list[int | type[Exception]]] = {}
        self.created = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v4")
        match request.method, path.strip("/").split("/"):
            case "GET", ["users", "me", "channels"]:
                return self._answer("channels", request, lambda: (200, self.channels))
            case "GET", ["channels", channel_id, "posts"]:
                return self._answer(
                    "posts", request, lambda: (200, self._page(channel_id, request))
                )
            case "GET", ["users", _, "teams", _, "drafts"]:
                # The real server answers `null`, not an empty list, when there are no drafts.
                return self._answer("drafts", request, lambda: (200, self.drafts or None))
            case "POST", ["drafts"]:
                return self._answer("upsert", request, lambda: (201, self._upsert(request)))
            case "POST", ["posts"]:
                return self._answer("post", request, lambda: (201, self._create(request)))
        return httpx2.Response(404, json={})

    def _answer(self, kind: str, request: httpx2.Request, do: Any) -> httpx2.Response:
        queue = self.failures.get(kind, [])
        failure = queue.pop(0) if queue else None
        if isinstance(failure, int):
            return httpx2.Response(failure, content=b"sentinel-server-reply")
        if failure is LostAnswer:
            do()
            raise httpx2.ReadTimeout("no answer", request=request)
        if failure is not None:
            raise httpx2.ConnectError("refused", request=request)
        status, body = do()
        # Written out by hand: httpx2 sends no body at all for json=None, and the server sends null.
        content = json.dumps(body).encode()
        return httpx2.Response(
            status, content=content, headers={"Content-Type": "application/json"}
        )

    def _page(self, channel_id: str, request: httpx2.Request) -> dict[str, Any]:
        posts = sorted(self.posts.get(channel_id, []), key=lambda p: p["create_at"], reverse=True)
        page, size = int(request.url.params["page"]), int(request.url.params["per_page"])
        chosen = posts[page * size : (page + 1) * size]
        return {"order": [p["id"] for p in chosen], "posts": {p["id"]: p for p in chosen}}

    def _upsert(self, request: httpx2.Request) -> dict[str, Any]:
        body = json.loads(request.content)
        place = (body["channel_id"], body["root_id"])
        self.drafts = [d for d in self.drafts if (d["channel_id"], d["root_id"]) != place]
        if body["message"]:
            self.drafts.append(body)
        return body

    def _create(self, request: httpx2.Request) -> dict[str, Any]:
        body = json.loads(request.content)
        self.created += 1
        post = {
            "id": f"created-{self.created}",
            "user_id": ME.user_id,
            "create_at": NOW_MS + self.created,
            "delete_at": 0,
            "type": "",
            **body,
        }
        self.posts.setdefault(body["channel_id"], []).append(post)
        return post

    def said(self, channel_id: str, post_id: str, create_at: int, **fields: Any) -> None:
        """Add a post that someone made in a chat."""
        post = {
            "id": post_id,
            "channel_id": channel_id,
            "user_id": ME.user_id,
            "root_id": "",
            "create_at": create_at,
            "delete_at": 0,
            "type": "",
            "message": "hello",
            **fields,
        }
        self.posts.setdefault(channel_id, []).append(post)

    def draft(self, channel_id: str, message: str, root_id: str = "") -> None:
        """Put a draft into a message box, as typing there does."""
        self.drafts.append({"channel_id": channel_id, "root_id": root_id, "message": message})

    def sent(self, method: str, suffix: str) -> list[dict[str, Any]]:
        """Return the bodies of the requests made with `method` to a path ending in `suffix`."""
        return [
            json.loads(r.content) if r.content else {}
            for r in self.requests
            if r.method == method and r.url.path.endswith(suffix)
        ]


class World:
    """The store, the fake server and a deliverer, with a clock the test moves."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store = Store(tmp_path / "state" / FILE_NAME)
        self.server = Server()
        self.now = NOW
        self.rules_text = RULES
        self.allow_send = True
        self.channels: dict[str, Any] = {}
        self.posts = 0

    def runtime(self, client: MattermostClient) -> app.Runtime:
        async def no_events() -> Any:
            raise AssertionError("the deliver loop opens no WebSocket")

        return app.Runtime(
            client=client,
            store=self.store,
            rules=parse_rules(self.rules_text),
            connect_events=no_events,
            heartbeat_file=self.tmp_path / "state" / app.HEARTBEAT_FILE,
            clock=lambda: self.now,
        )

    def queue(
        self,
        text: str = "Yes, the filter is right.",
        mode: DeliveryMode = "draft",
        *,
        channel: str = DM,
        root_id: str = "",
        kind: Any = "dm",
        sender: str | None = "example.user",
        sender_id: str = "other-id",
        age_seconds: float = 60.0,
        planned: bool = True,
    ) -> int:
        """Open a one-post job and queue its reply, as the listener and the processor do.

        `planned` off writes the row as given, without the processor's checks.
        """
        self.posts += 1
        post = make_post(
            id=f"asked-{self.posts}",
            channel_id=channel,
            channel_type="D" if kind == "dm" else "O",
            user_id=sender_id,
            root_id=root_id,
            create_at=int((self.now - age_seconds) * 1000),
            message="a question",
        )
        rules = parse_rules(self.rules_text)
        rule = rules.rules[1] if mode == "send" else rules.rules[0]
        self.store.claim(
            post,
            kind=kind,
            rule=rule,
            place="@example.user",
            sender=sender,
            live=True,
            now=self.now,
            cap=100,
            bursts=ONE_POST,
        )
        job = self.store.take_ready(self.now)
        assert job is not None
        plan = plan_reply(mode, job, self.store.posts(job.id)[0], text, needs_review=False)
        assert self.store.finish_with_reply(
            job.id,
            mode=plan.mode if planned else mode,
            root_id=plan.root_id,
            message=text,
            downgrade=plan.downgrade if planned else None,
            refused=plan.refused if planned else False,
            backend="fake",
            decision="act",
            now=self.now,
        )
        return job.id

    def once(self, restart: bool = False) -> bool:
        """Run one pass of the deliver loop. `restart` forgets what the process had in memory."""
        if restart:
            self.channels = {}

        async def main() -> bool:
            transport = httpx2.MockTransport(self.server)
            client = MattermostClient(URL, TOKEN, transport=transport)
            writer = MattermostWriter(URL, TOKEN, allow_post=self.allow_send, transport=transport)
            deliverer = Deliverer(writer, allow_send=self.allow_send, channels=self.channels)
            try:
                return await deliverer.once(self.runtime(client), ME)
            finally:
                self.channels = deliverer.channels
                await client.aclose()
                await deliverer.aclose()

        return asyncio.run(main())

    def row(self, job_id: int) -> OutboxRow:
        return next(row for row in read_outbox(self.store.path) if row.job_id == job_id)

    def what(self, job_id: int) -> tuple[str, str, str | None, str | None, str | None]:
        row = self.row(job_id)
        return (row.mode, row.state, row.outcome, row.downgrade, row.reason)

    def text(self, job_id: int) -> str | None:
        connection = sqlite3.connect(self.store.path)
        try:
            return connection.execute(
                "SELECT message FROM outbox WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        finally:
            connection.close()

    def delivery(self, job_id: int) -> tuple[str | None, str | None]:
        job = next(job for job in read_jobs(self.store.path) if job.id == job_id)
        return (job.reply_mode, job.delivery)

    def files(self) -> bytes:
        folder = self.store.path.parent
        return b"".join(file.read_bytes() for file in folder.iterdir() if file.is_file())


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def log() -> Generator[Any]:
    with captured_logs("DEBUG") as stream:
        yield stream


# --- drafts -----------------------------------------------------------------------------------


def test_a_draft_reply_goes_into_the_message_box(world: World, log: Any) -> None:
    job = world.queue("Yes, the filter is right.")
    assert world.once()
    assert world.server.sent("POST", "/drafts") == [
        {
            "channel_id": DM,
            "root_id": "",
            "message": "Yes, the filter is right.",
            "props": {},
            "create_at": NOW_MS,
            "update_at": NOW_MS,
        }
    ]
    assert world.server.sent("POST", "/posts") == []
    assert world.what(job) == ("draft", "done", "drafted", None, None)
    assert world.delivery(job) == ("draft", "drafted")
    assert world.text(job) is None
    assert (
        f"event=reply_delivered job={job} mode=draft outcome=drafted downgrade=null reason=null "
        f'channel="{DM}"' in squeezed(log.getvalue())
    )
    assert not world.once()  # nothing left to do


def test_a_draft_for_a_thread_goes_into_that_thread(world: World) -> None:
    job = world.queue(channel=TOWN, kind="channel")
    world.once()
    (body,) = world.server.sent("POST", "/drafts")
    assert (body["channel_id"], body["root_id"]) == (TOWN, "asked-1")
    assert world.what(job)[2] == "drafted"


def test_the_drafts_of_a_channel_are_listed_under_its_team_and_of_a_dm_under_mine(
    world: World,
) -> None:
    world.queue(channel=TOWN, kind="channel")
    world.queue()
    world.once()
    world.once()
    listings = [r.url.path for r in world.server.requests if r.url.path.endswith("/drafts")]
    assert listings.count(f"/api/v4/users/{ME.user_id}/teams/{TEAM}/drafts") == 2


def test_a_draft_that_is_already_there_is_never_overwritten(world: World, log: Any) -> None:
    world.server.draft(DM, "Kirill is typing this")
    job = world.queue()
    assert world.once()
    assert world.server.sent("POST", "/drafts") == []
    assert world.what(job) == ("draft", "pending", None, None, "draft_busy")
    assert world.row(job).retry_at == NOW + BUSY_RETRY_SECONDS
    assert world.delivery(job) == ("draft", "queued")
    assert world.text(job) is not None

    assert not world.once()  # not due yet
    world.now += BUSY_RETRY_SECONDS + 1
    assert world.once()  # tried again, still busy
    assert world.server.sent("POST", "/drafts") == []
    assert squeezed(log.getvalue()).count(f'event=draft_busy job={job} channel="{DM}"') == 1

    world.server.drafts.clear()  # Kirill sent or deleted his text
    world.now += BUSY_RETRY_SECONDS + 1
    assert world.once()
    assert [body["message"] for body in world.server.sent("POST", "/drafts")] == [
        "Yes, the filter is right."
    ]
    assert world.what(job) == ("draft", "done", "drafted", None, None)
    assert world.server.drafts[0]["message"] == "Yes, the filter is right."


def test_a_busy_message_box_does_not_hold_up_other_chats(world: World) -> None:
    world.server.draft(DM, "Kirill is typing this")
    busy = world.queue()
    other = world.queue(channel=TOWN, kind="channel")
    world.once()
    world.once()
    assert world.what(busy)[1] == "pending" and world.what(other)[2] == "drafted"


def test_a_reply_that_waited_three_days_expires_and_keeps_its_text(world: World, log: Any) -> None:
    world.server.draft(DM, "Kirill never sends this")
    job = world.queue(CANARY)
    world.once()
    world.now += DRAFT_RETENTION_SECONDS + 1
    assert not world.once()
    assert world.what(job) == ("draft", "done", "expired", None, "draft_busy")
    assert world.delivery(job) == ("draft", "expired")
    assert [reply.text for reply in read_undelivered(world.store.path)] == [CANARY]
    assert world.server.sent("POST", "/drafts") == []
    assert f"event=reply_delivered job={job} mode=draft outcome=expired" in squeezed(log.getvalue())


@pytest.mark.parametrize("message", ["", "   ", "\n"])
def test_an_empty_draft_does_not_block(world: World, message: str) -> None:
    world.server.draft(DM, message)
    job = world.queue()
    world.once()
    assert world.what(job)[2] == "drafted"


def test_a_draft_elsewhere_does_not_block(world: World) -> None:
    world.server.draft(DM, "a draft in a thread of this chat", root_id="some-thread")
    world.server.draft(TOWN, "a draft in another chat")
    job = world.queue()
    world.once()
    assert world.what(job)[2] == "drafted"
    assert len(world.server.drafts) == 3


def test_our_own_draft_from_before_a_crash_is_recorded_not_written_again(world: World) -> None:
    job = world.queue("Yes.")
    world.server.draft(DM, "Yes.")  # written, and the process died before recording it
    world.once()
    assert world.server.sent("POST", "/drafts") == []
    assert world.what(job) == ("draft", "done", "drafted", None, None)


def test_a_crash_while_delivering_a_draft_delivers_it_at_the_next_start(world: World) -> None:
    job = world.queue()
    assert world.store.take_outbox(NOW) is not None  # taken, and the process died
    assert world.what(job)[1] == "delivering"
    assert world.once(restart=True)
    assert world.what(job)[2] == "drafted"
    assert len(world.server.sent("POST", "/drafts")) == 1


def test_synced_drafts_switched_off_fails_the_reply_and_keeps_its_text(
    world: World, log: Any
) -> None:
    world.server.failures["drafts"] = [501]
    job = world.queue(CANARY)
    world.once()
    assert world.what(job) == ("draft", "done", "failed", None, "drafts_disabled")
    assert world.delivery(job) == ("draft", "failed")
    assert [reply.text for reply in read_undelivered(world.store.path)] == [CANARY]
    assert "outcome=failed downgrade=null reason=drafts_disabled" in squeezed(log.getvalue())


@pytest.mark.parametrize("kind", ["channels", "posts", "drafts", "upsert"])
@pytest.mark.parametrize("failure", [ConnectionError, 503, 429, 500])
def test_an_outage_only_delays_a_draft(
    world: World, log: Any, kind: str, failure: int | type[Exception]
) -> None:
    world.server.failures[kind] = [failure, failure]
    job = world.queue()
    assert world.once()
    assert world.what(job) == ("draft", "pending", None, None, "unreachable")
    assert world.row(job).retry_at == NOW + OUTAGE_RETRY_SECONDS
    assert not world.once()
    world.now += OUTAGE_RETRY_SECONDS + 1
    assert world.once()  # still down
    assert world.what(job)[1] == "pending"
    assert squeezed(log.getvalue()).count(f"event=reply_waiting job={job} reason=unreachable") == 1
    world.now += OUTAGE_RETRY_SECONDS + 1
    assert world.once()
    assert world.what(job) == ("draft", "done", "drafted", None, None)
    # One upsert in the end; when the upsert itself was what failed, the two that failed as well.
    assert len(world.server.sent("POST", "/drafts")) == (3 if kind == "upsert" else 1)
    assert world.server.drafts[0]["message"] == "Yes, the filter is right."


def test_a_refusal_that_waiting_will_not_change_fails_the_reply(world: World) -> None:
    world.server.failures["posts"] = [403]
    job = world.queue(CANARY)
    world.once()
    assert world.what(job) == ("draft", "done", "failed", None, "server_error")
    assert world.text(job) == CANARY


def test_a_chat_the_server_no_longer_lists_fails_the_reply(world: World) -> None:
    job = world.queue(channel="left-this-channel", kind="channel")
    world.once()
    assert world.what(job) == ("draft", "done", "failed", None, "no_channel")
    assert world.server.sent("POST", "/drafts") == []


def test_a_dm_draft_waits_while_no_team_is_known(world: World) -> None:
    world.server.channels = [world.server.channels[0]]  # only the DM, which has no team
    job = world.queue()
    world.once()
    assert world.what(job) == ("draft", "pending", None, None, "no_team")
    assert world.server.sent("POST", "/drafts") == []


def test_a_rejected_token_ends_the_loop_and_loses_nothing(world: World) -> None:
    world.server.failures["posts"] = [401]
    job = world.queue()
    with pytest.raises(MattermostError) as error:
        world.once()
    assert error.value.reason == "auth_rejected"
    assert world.what(job)[1] == "delivering"
    assert world.once(restart=True)  # with a good token again
    assert world.what(job)[2] == "drafted"


def test_a_command_in_the_outbox_reaches_no_message_box(world: World) -> None:
    """The processor refuses these itself. The listener does not rely on that."""
    for mode in ("draft", "send"):
        job = world.queue("/away", mode, planned=False)
        world.once()
        assert world.what(job)[2] == "refused_command"
        assert world.text(job) == "/away"
    assert world.server.sent("POST", "/drafts") == [] and world.server.sent("POST", "/posts") == []


# --- already answered -------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["draft", "send"])
def test_nothing_is_written_once_kirill_has_answered(
    world: World, log: Any, mode: DeliveryMode
) -> None:
    job = world.queue(CANARY, mode)
    world.server.said(DM, "my-answer", NOW_MS - 30_000)
    world.once()
    assert world.what(job)[1:3] == ("done", "skipped_answered")
    assert world.delivery(job)[1] == "skipped_answered"
    assert world.text(job) is None
    assert world.server.sent("POST", "/drafts") == [] and world.server.sent("POST", "/posts") == []
    assert "outcome=skipped_answered" in squeezed(log.getvalue())


def test_an_answer_in_the_thread_counts_for_a_thread_reply(world: World) -> None:
    job = world.queue(channel=TOWN, kind="channel")
    world.server.said(TOWN, "my-answer", NOW_MS - 30_000, root_id="asked-1")
    world.once()
    assert world.what(job)[2] == "skipped_answered"


@pytest.mark.parametrize(
    "fields",
    [
        {"root_id": "another-thread"},  # mine, in another thread of that channel
        {"root_id": ""},  # mine, in the channel itself
        {"root_id": "asked-1", "user_id": "other-id"},  # someone else's, in the thread
        {"root_id": "asked-1", "props": {AGENT_PROP: True}},  # a reply our code posted
        {"root_id": "asked-1", "delete_at": 5},  # mine, and deleted
    ],
)
def test_what_does_not_count_as_an_answer_to_a_thread(world: World, fields: dict[str, Any]) -> None:
    job = world.queue(channel=TOWN, kind="channel")
    world.server.said(TOWN, "not-an-answer", NOW_MS - 30_000, **fields)
    world.once()
    assert world.what(job)[2] == "drafted"


def test_a_post_of_mine_from_before_the_burst_is_not_an_answer(world: World) -> None:
    job = world.queue(age_seconds=60)
    world.server.said(DM, "earlier", NOW_MS - 61_000)
    world.once()
    assert world.what(job)[2] == "drafted"


# --- direct replies ---------------------------------------------------------------------------


def test_a_send_reply_is_posted_with_the_label_and_our_marker(world: World, log: Any) -> None:
    job = world.queue("Thanks @example.user, it is fixed.", "send")
    assert world.once()
    assert world.server.sent("POST", "/posts") == [
        {
            "channel_id": DM,
            "root_id": "",
            "message": f"Thanks @example.user, it is fixed.\n\n{LABEL}",
            "props": {AGENT_PROP: True},
        }
    ]
    assert world.server.sent("POST", "/drafts") == []
    assert world.what(job) == ("send", "done", "sent", None, None)
    assert world.row(job).post_id == "created-1" and world.row(job).attempts == 1
    assert world.delivery(job) == ("send", "sent")
    assert world.text(job) is None
    assert (
        f"event=reply_delivered job={job} mode=send outcome=sent downgrade=null reason=null "
        f'channel="{DM}"' in squeezed(log.getvalue())
    )


def test_a_send_reply_in_a_channel_goes_into_the_thread(world: World) -> None:
    world.queue("Yes.", "send", channel=TOWN, kind="channel")
    world.once()
    (body,) = world.server.sent("POST", "/posts")
    assert (body["channel_id"], body["root_id"]) == (TOWN, "asked-1")


def test_without_the_kill_switch_a_send_becomes_a_draft_without_the_label(
    world: World, log: Any
) -> None:
    world.allow_send = False
    job = world.queue("Yes.", "send")
    world.once()
    assert world.server.sent("POST", "/posts") == []
    assert [body["message"] for body in world.server.sent("POST", "/drafts")] == ["Yes."]
    assert LABEL not in world.server.drafts[0]["message"]
    assert world.what(job) == ("draft", "done", "drafted", "kill_switch", None)
    assert world.delivery(job) == ("draft", "drafted")
    assert "mode=draft outcome=drafted downgrade=kill_switch" in squeezed(log.getvalue())


@pytest.mark.parametrize(
    ("age_seconds", "downgrade"),
    [
        (30 * 60 - 5, None),
        (30 * 60 + 5, "stale"),
        (-4 * 60, None),  # the server's clock is a little ahead of ours
        (-6 * 60, "stale"),  # a burst from the future is not believed
    ],
)
def test_a_burst_too_old_or_from_the_future_is_not_answered_directly(
    world: World, age_seconds: float, downgrade: str | None
) -> None:
    job = world.queue("Yes.", "send", age_seconds=age_seconds)
    world.once()
    assert world.what(job)[3] == downgrade
    assert len(world.server.sent("POST", "/posts")) == (1 if downgrade is None else 0)
    assert len(world.server.sent("POST", "/drafts")) == (0 if downgrade is None else 1)


def test_the_cap_per_chat_and_the_cap_in_total(world: World) -> None:
    first, second, third = (world.queue("Yes.", "send") for _ in range(3))
    for _ in range(3):
        world.once()
    assert [world.what(job)[2:4] for job in (first, second, third)] == [
        ("sent", None),
        ("sent", None),
        ("drafted", "chat_cap"),
    ]
    world.server.drafts.clear()
    fourth = world.queue("Yes.", "send", channel=TOWN, kind="channel")
    fifth = world.queue("Yes.", "send", channel=TOWN, kind="channel", root_id="another-thread")
    world.once()
    world.once()
    assert world.what(fourth)[2:4] == ("sent", None)
    assert world.what(fifth)[2:4] == ("drafted", "cap")  # three in the hour, in total
    assert len(world.server.sent("POST", "/posts")) == 3

    world.server.drafts.clear()
    world.now += 3601
    later = world.queue("Yes.", "send")
    world.once()
    assert world.what(later)[2:4] == ("sent", None)


def test_kirills_own_burst_is_never_answered_directly(world: World) -> None:
    job = world.queue("Noted.", "send", sender="example.me", sender_id=ME.user_id)
    world.once()
    assert world.what(job) == ("draft", "done", "drafted", "own_burst", None)
    assert world.server.sent("POST", "/posts") == []


def test_a_rule_that_is_no_longer_send_in_the_listeners_rules_is_not_obeyed(world: World) -> None:
    job = world.queue("Yes.", "send")
    world.rules_text = RULES.replace(", reply: send", "")
    world.once()
    assert world.what(job) == ("draft", "done", "drafted", "rule", None)
    other = world.queue("Yes.", "send")
    world.rules_text = RULES.replace("name: trusted", "name: renamed")
    world.server.drafts.clear()
    world.once()
    assert world.what(other)[3] == "rule"
    assert world.server.sent("POST", "/posts") == []


@pytest.mark.parametrize(
    ("text", "downgrade"),
    [
        ("See https://example.com/docs", "link"),
        ("Ask @petr about it", "mention"),
        ("Hello @channel", "mention"),
        ("x" * 1501, "length"),
    ],
)
def test_the_listener_runs_the_content_checks_again(
    world: World, text: str, downgrade: str
) -> None:
    """A row that says `send` is not believed: the text must pass here too."""
    job = world.queue(text, "send", planned=False)
    world.once()
    assert world.what(job) == ("draft", "done", "drafted", downgrade, None)
    assert world.server.sent("POST", "/posts") == []


def test_a_row_for_another_chat_than_its_jobs_is_not_posted(world: World) -> None:
    job = world.queue("Yes.", "send")
    connection = sqlite3.connect(world.store.path)
    connection.execute("UPDATE outbox SET channel_id = ?", (TOWN,))
    connection.commit()
    connection.close()
    world.once()
    assert world.what(job)[3] == "rule"
    assert world.server.sent("POST", "/posts") == []


@pytest.mark.parametrize("status", [400, 403, 404, 413, 429])
def test_a_post_the_server_refuses_becomes_a_draft(world: World, status: int) -> None:
    world.server.failures["post"] = [status]
    job = world.queue("Yes.", "send")
    world.once()
    assert world.what(job) == ("draft", "done", "drafted", "refused", None)
    assert len(world.server.sent("POST", "/posts")) == 1
    assert [body["message"] for body in world.server.sent("POST", "/drafts")] == ["Yes."]


# --- at most one POST per reply ---------------------------------------------------------------


def test_a_post_whose_answer_was_lost_is_found_and_not_posted_again(world: World, log: Any) -> None:
    world.server.failures["post"] = [LostAnswer]
    job = world.queue("Yes.", "send")
    assert world.once()
    assert world.what(job) == ("send", "delivering", None, None, None)
    assert world.row(job).attempts == 1
    assert f"event=reply_waiting job={job} reason=unconfirmed" in squeezed(log.getvalue())

    world.now += CONFIRM_AFTER_SECONDS - 1
    assert not world.once(restart=True)  # too soon to look: the post may still be on its way
    assert world.what(job)[1] == "delivering"
    world.now += 2
    assert not world.once()
    assert world.what(job) == ("send", "done", "sent", None, None)
    assert world.row(job).post_id == "created-1"
    assert len(world.server.sent("POST", "/posts")) == 1
    assert world.server.sent("POST", "/drafts") == []


@pytest.mark.parametrize("failure", [ConnectionError, 500, 502, 503])
def test_a_post_that_never_arrived_becomes_a_draft_and_is_not_posted_again(
    world: World, failure: int | type[Exception]
) -> None:
    world.server.failures["post"] = [failure]
    job = world.queue("Yes.", "send")
    world.once()
    assert world.what(job)[1] == "delivering"
    world.now += CONFIRM_AFTER_SECONDS + 1
    assert world.once()  # the chat was read, no post of ours is there: a draft, at once
    assert world.what(job) == ("draft", "done", "drafted", "unconfirmed", None)
    assert len(world.server.sent("POST", "/posts")) == 1
    assert [body["message"] for body in world.server.sent("POST", "/drafts")] == ["Yes."]


def test_an_unconfirmed_post_waits_through_an_outage_and_is_never_repeated(world: World) -> None:
    world.server.failures["post"] = [LostAnswer]
    job = world.queue("Yes.", "send")
    world.once()
    for _ in range(5):
        world.server.failures["posts"] = [ConnectionError]
        world.now += CONFIRM_AFTER_SECONDS + 1
        assert not world.once(restart=True)
        assert world.what(job)[1] == "delivering"
    world.once()
    assert world.what(job)[2] == "sent"
    assert len(world.server.sent("POST", "/posts")) == 1


def test_a_crash_before_the_post_was_started_posts_once_at_the_next_start(world: World) -> None:
    job = world.queue("Yes.", "send")
    assert world.store.take_outbox(NOW) is not None  # taken, and the process died
    assert world.once(restart=True)
    assert world.what(job)[2] == "sent"
    assert len(world.server.sent("POST", "/posts")) == 1


def test_a_crash_after_the_post_was_started_never_posts_again(world: World) -> None:
    """The process died between begin_send and recording the answer. Was the post made?"""
    made = world.queue("It was made.", "send")
    not_made = world.queue("It was not.", "send", channel=TOWN, kind="channel")
    for _ in range(2):
        taken = world.store.take_outbox(NOW)
        assert taken is not None
        assert world.store.begin_send(taken.row.job_id, now=NOW, per_hour=9, per_chat=9) == "go"
    world.server.said(
        DM, "it-arrived", NOW_MS + 5, message=f"It was made.\n\n{LABEL}", props={AGENT_PROP: True}
    )

    world.now += CONFIRM_AFTER_SECONDS + 1
    world.once(restart=True)
    world.once()
    assert world.what(made) == ("send", "done", "sent", None, None)
    assert world.row(made).post_id == "it-arrived"
    assert world.what(not_made) == ("draft", "done", "drafted", "unconfirmed", None)
    assert world.server.sent("POST", "/posts") == []


def test_another_replys_post_in_the_same_chat_does_not_confirm_this_one(world: World) -> None:
    world.queue("The first answer.", "send")
    world.once()
    assert len(world.server.sent("POST", "/posts")) == 1
    second = world.queue("The second answer.", "send")
    taken = world.store.take_outbox(world.now)
    assert taken is not None and taken.row.job_id == second
    assert world.store.begin_send(second, now=world.now, per_hour=9, per_chat=9) == "go"
    world.now += CONFIRM_AFTER_SECONDS + 1
    world.once(restart=True)
    assert world.what(second)[3] == "unconfirmed"
    assert len(world.server.sent("POST", "/posts")) == 1


def test_begin_send_says_go_once_per_reply(world: World) -> None:
    job = world.queue("Yes.", "send")
    assert (
        world.store.begin_send(job, now=NOW, per_hour=9, per_chat=9) == "unconfirmed"
    )  # not taken
    world.store.take_outbox(NOW)
    assert world.store.begin_send(job, now=NOW, per_hour=9, per_chat=9) == "go"
    assert world.store.begin_send(job, now=NOW, per_hour=9, per_chat=9) == "unconfirmed"
    draft = world.queue("Yes.")
    world.store.release_outbox(NOW)
    assert world.store.take_outbox(NOW) is not None
    assert world.store.begin_send(draft, now=NOW, per_hour=9, per_chat=9) == "unconfirmed"


def test_an_unconfirmed_post_counts_against_the_caps(world: World) -> None:
    world.server.failures["post"] = [LostAnswer, LostAnswer]
    first, second, third = (world.queue("Yes.", "send") for _ in range(3))
    for _ in range(3):
        world.once()
    assert [world.what(job)[1] for job in (first, second)] == ["delivering", "delivering"]
    assert world.what(third)[2:4] == ("drafted", "chat_cap")


def test_a_draft_is_never_turned_back_into_a_send(world: World) -> None:
    world.allow_send = False
    world.server.draft(DM, "Kirill is typing this")
    job = world.queue("Yes.", "send")
    world.once()
    assert world.what(job) == ("draft", "pending", None, "kill_switch", "draft_busy")
    world.allow_send = True  # the listener was restarted with ALLOW_SEND=1
    world.server.drafts.clear()
    world.now += BUSY_RETRY_SECONDS + 1
    world.once(restart=True)
    assert world.what(job) == ("draft", "done", "drafted", "kill_switch", None)
    assert world.server.sent("POST", "/posts") == []


def test_with_the_kill_switch_off_no_path_posts(world: World) -> None:
    world.allow_send = False
    world.server.failures["upsert"] = [503]
    # Each in a place of its own: a second reply to one place would wait behind the first's draft.
    world.queue("Yes.", "send")
    world.queue("See https://example.com", "send", channel=TOWN, kind="channel", planned=False)
    world.queue("Yes.", "send", channel=TOWN, kind="channel")
    world.queue("Yes.", channel=TOWN, kind="channel")
    sent = world.queue("Unconfirmed.", "send", channel=TOWN, kind="channel", root_id="thread-2")
    connection = sqlite3.connect(world.store.path)
    connection.execute(
        "UPDATE outbox SET state = 'delivering', attempts = 1 WHERE job_id = ?", (sent,)
    )
    connection.commit()
    connection.close()
    for _ in range(12):
        world.once(restart=True)
        world.now += CONFIRM_AFTER_SECONDS + 1
    assert world.server.sent("POST", "/posts") == []
    assert {row.state for row in read_outbox(world.store.path)} == {"done"}
    assert {row.outcome for row in read_outbox(world.store.path)} == {"drafted"}
    assert all(LABEL not in draft["message"] for draft in world.server.drafts)


# --- text at rest, and the log ----------------------------------------------------------------


@pytest.mark.parametrize("mode", ["draft", "send"])
def test_a_delivered_reply_leaves_the_state_files(world: World, mode: DeliveryMode) -> None:
    job = world.queue(CANARY, mode)
    assert CANARY.encode() in world.files()
    world.once()
    assert world.what(job)[2] in {"drafted", "sent"}
    assert CANARY.encode() not in world.files()


def test_no_text_reaches_the_log_or_an_error(world: World, log: Any) -> None:
    world.server.draft(TOWN, f"typing {CANARY}", root_id="asked-2")
    world.server.failures.update({"post": [LostAnswer], "upsert": [503], "drafts": [501]})
    world.queue(f"sent {CANARY}", "send")
    world.queue(f"busy {CANARY}", channel=TOWN, kind="channel")
    world.queue(f"drafted {CANARY}", channel=TOWN, kind="channel", root_id="thread-3")
    world.queue(f"/command {CANARY}", planned=False)
    world.server.said(DM, "answered", NOW_MS - 1, message=f"answer {CANARY}")
    world.queue(f"answered {CANARY}")
    for _ in range(12):
        world.once()
        world.now += CONFIRM_AFTER_SECONDS + 1
    world.server.failures["posts"] = [401]
    world.queue(f"late {CANARY}")
    with pytest.raises(MattermostError) as error:
        world.once()
    assert CANARY not in log.getvalue() and "sentinel-server-reply" not in log.getvalue()
    assert "a question" not in log.getvalue() and TOKEN not in log.getvalue()
    assert CANARY not in str(error.value) and CANARY not in repr(error.value)
    assert "event=reply_delivered" in log.getvalue()


def test_a_bug_while_delivering_fails_that_reply_and_keeps_the_loop_alive(
    world: World, log: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError(CANARY)

    job = world.queue(CANARY)
    other = world.queue("Yes.", channel=TOWN, kind="channel")
    monkeypatch.setattr(deliver, "is_command", broken)
    assert world.once()
    assert world.what(job) == ("draft", "done", "failed", None, "internal_error")
    assert world.text(job) == CANARY
    assert "event=internal_error" in squeezed(log.getvalue()) and "RuntimeError" in log.getvalue()
    assert CANARY not in log.getvalue()
    monkeypatch.undo()
    world.once()
    assert world.what(other)[2] == "drafted"


# --- the loop ---------------------------------------------------------------------------------


def test_the_loop_delivers_everything_that_is_due_and_keeps_looking(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deliver, "DELIVER_SECONDS", 0.01)
    first = world.queue(channel=TOWN, kind="channel")
    second = world.queue(channel=TOWN, kind="channel", root_id="thread-2")

    async def main() -> None:
        transport = httpx2.MockTransport(world.server)
        client = MattermostClient(URL, TOKEN, transport=transport)
        deliverer = Deliverer(
            MattermostWriter(URL, TOKEN, allow_post=False, transport=transport), allow_send=False
        )
        task = asyncio.create_task(deliverer.run(world.runtime(client), ME))
        try:
            async with asyncio.timeout(5):
                while len(world.server.drafts) < 2:
                    await asyncio.sleep(0.005)
                third = world.queue()  # arrives while the loop is idle
                while world.what(third)[1] != "done":
                    await asyncio.sleep(0.005)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
            await deliverer.aclose()

    asyncio.run(main())
    assert world.what(first)[2] == world.what(second)[2] == "drafted"
    assert len(world.server.drafts) == 3


def test_build_sets_the_kill_switch_on_the_checks_and_on_the_writer() -> None:
    async def main() -> None:
        for allow in (False, True):
            deliverer = deliver.build(URL, TOKEN, allow_send=allow)
            assert deliverer.allow_send is allow
            assert deliverer.writer._allow_post is allow
            await deliverer.aclose()

    asyncio.run(main())


def test_a_second_reply_to_the_same_place_waits_behind_the_first_draft(world: World) -> None:
    """Our own earlier draft is a draft like any other: it is not overwritten either."""
    first = world.queue("The first answer.")
    second = world.queue("The second answer.")
    world.once()
    world.once()
    assert world.what(first)[2] == "drafted"
    assert world.what(second) == ("draft", "pending", None, None, "draft_busy")
    assert [draft["message"] for draft in world.server.drafts] == ["The first answer."]

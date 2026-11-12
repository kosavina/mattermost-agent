import pytest

from mattermost_agent.replies import MAX_SEND_CHARS, content_downgrade, is_command, plan_reply
from mattermost_agent.rules import DeliveryMode
from mattermost_agent.store import Job, JobPost

TEXT_CANARY = "reply-canary-HERONLAMP"


def job(root_id: str = "", sender: str | None = "ivan") -> Job:
    return Job(
        id=7,
        status="running",
        scenario="intake",
        rule="dm",
        place="@ivan",
        post_count=1,
        opened_at=0.0,
        ready_at=0.0,
        deadline_at=0.0,
        channel_id="channel-1",
        sender=sender,
        root_id=root_id,
    )


def first(kind: str = "dm", post_id: str = "first-post") -> JobPost:
    return JobPost(post_id, 1_000, kind, "a question")


@pytest.mark.parametrize(
    ("draft", "needs_review", "downgrade"),
    [
        ("Yes, the filter is right.", False, None),
        ("Thanks @ivan, it is fixed.", False, None),
        ("@Ivan. Done", False, None),
        ("x" * MAX_SEND_CHARS, False, None),
        ("Fine, but please check.", True, "needs_review"),
        ("x" * (MAX_SEND_CHARS + 1), False, "length"),
        ("Hello @channel", False, "mention"),
        ("Hello @ALL", False, "mention"),
        ("hello @here.", False, "mention"),
        ("Ask @petr about it", False, "mention"),
        ("Ask @ivan and @petr", False, "mention"),
        ("Ask @ivan.petrov about it", False, "mention"),
        ("write to ivan@example.com", False, "mention"),
        ("ask @Пётр", False, "mention"),
        ("See http://example.com", False, "link"),
        ("See HTTPS://example.com", False, "link"),
        ("See ftp://example.com/file", False, "link"),
        ("See www.example.com", False, "link"),
        ("See [the docs](docs)", False, "link"),
        ("![chart](chart.png)", False, "link"),
        # The first check that fires is the one recorded.
        ("@channel " + "x" * MAX_SEND_CHARS, False, "length"),
        ("@petr see http://example.com", False, "mention"),
        ("@petr see http://example.com", True, "needs_review"),
    ],
)
def test_a_send_reply_is_downgraded_by_the_first_check_that_fires(
    draft: str, needs_review: bool, downgrade: str | None
) -> None:
    plan = plan_reply("send", job(), first(), draft, needs_review=needs_review)
    assert plan.downgrade == downgrade
    assert plan.mode == ("send" if downgrade is None else "draft")
    assert plan.message == draft
    assert not plan.refused


def test_a_lone_at_sign_is_not_a_mention() -> None:
    assert content_downgrade("meet @ 5 pm, cost 3 @ 10", "ivan") is None
    assert content_downgrade("the end @", "ivan") is None


def test_without_a_known_sender_every_mention_downgrades() -> None:
    assert content_downgrade("Thanks @ivan", None) == "mention"
    plan = plan_reply("send", job(sender=None), first(), "Thanks @ivan", needs_review=False)
    assert (plan.mode, plan.downgrade) == ("draft", "mention")


@pytest.mark.parametrize("mode", ["draft", "send"])
@pytest.mark.parametrize("draft", ["/away", "  /leave", "\n/kick @ivan", "/"])
def test_a_slash_command_is_refused_in_every_mode(mode: DeliveryMode, draft: str) -> None:
    plan = plan_reply(mode, job(), first(), draft, needs_review=False)
    assert plan.refused and plan.mode == "draft" and plan.downgrade is None
    assert plan.message == draft
    assert is_command(draft)


def test_a_slash_inside_the_text_is_not_a_command() -> None:
    assert not is_command("either/or, and 1/2")
    plan = plan_reply("send", job(), first(), "either/or", needs_review=False)
    assert plan.mode == "send" and not plan.refused


@pytest.mark.parametrize(
    "draft",
    [
        "x" * (MAX_SEND_CHARS + 1),
        "@channel see http://example.com",
        "Fine.",
    ],
)
@pytest.mark.parametrize("needs_review", [False, True])
def test_a_draft_never_becomes_a_send(draft: str, needs_review: bool) -> None:
    plan = plan_reply("draft", job(), first(), draft, needs_review=needs_review)
    assert plan.mode == "draft"
    # In draft mode Kirill reads the text anyway, so no check is recorded against it.
    assert plan.downgrade is None and not plan.refused


def test_the_plan_never_adds_to_the_text() -> None:
    for mode in ("draft", "send"):
        plan = plan_reply(mode, job(), first(), "Yes.", needs_review=False)
        assert plan.message == "Yes."


@pytest.mark.parametrize(
    ("kind", "job_root", "expected"),
    [
        # A channel reply always goes into a thread.
        ("channel", "", "first-post"),
        ("mention", "", "first-post"),
        ("thread_reply", "thread-root", "thread-root"),
        ("mention", "thread-root", "thread-root"),
        # A DM or group DM reply stays where the burst was.
        ("dm", "", ""),
        ("group_dm", "", ""),
        ("dm", "thread-root", "thread-root"),
        ("group_dm", "thread-root", "thread-root"),
        # A kind this code does not know is treated as a channel.
        ("something_new", "", "first-post"),
    ],
)
@pytest.mark.parametrize("mode", ["draft", "send"])
def test_where_the_reply_goes(mode: DeliveryMode, kind: str, job_root: str, expected: str) -> None:
    plan = plan_reply(mode, job(root_id=job_root), first(kind), "Yes.", needs_review=False)
    assert plan.root_id == expected


def test_a_refused_reply_still_knows_its_place() -> None:
    plan = plan_reply("send", job(), first("channel"), "/away", needs_review=False)
    assert plan.refused and plan.root_id == "first-post"


def test_repr_leaves_out_the_text() -> None:
    plan = plan_reply("send", job(), first(), TEXT_CANARY, needs_review=False)
    assert TEXT_CANARY not in repr(plan)

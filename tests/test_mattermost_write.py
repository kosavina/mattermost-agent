"""The writer against a fake REST transport: three calls, each exactly what Task 0 verified."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

from mattermost_agent.events import AGENT_PROP, ChatDraft
from mattermost_agent.mattermost import MattermostError
from mattermost_agent.mattermost_write import MattermostWriter, WriteRefused

URL = "https://chat.example.com"
TOKEN = "abcdefghijklmnopqrstuvwxyz"
SECRET = "sentinel-private-reply"

type Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeServer:
    """Answer with `handler` and remember every request."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)


def run[T](
    server: FakeServer,
    call: Callable[[MattermostWriter], Awaitable[T]],
    *,
    allow_post: bool = True,
) -> T:
    async def main() -> T:
        transport = httpx2.MockTransport(server)
        writer = MattermostWriter(URL, TOKEN, allow_post=allow_post, transport=transport)
        try:
            return await call(writer)
        finally:
            await writer.aclose()

    return asyncio.run(main())


def reply(body: Any, status: int = 200) -> Handler:
    return lambda _: httpx2.Response(status, json=body)


def body_of(request: httpx2.Request) -> dict[str, Any]:
    return json.loads(request.content)


def upsert(writer: MattermostWriter) -> Awaitable[None]:
    return writer.upsert_draft("channel-1", "root-1", "Yes.", 1_700_000_000_000)


def post(writer: MattermostWriter) -> Awaitable[str]:
    return writer.create_post("channel-1", "root-1", "Yes.")


def drafts(writer: MattermostWriter) -> Awaitable[list[ChatDraft]]:
    return writer.list_drafts("me-id", "team-1")


def test_the_writer_has_three_calls_and_no_others() -> None:
    public = {name for name in vars(MattermostWriter) if not name.startswith("_")}
    assert public == {"aclose", "list_drafts", "upsert_draft", "create_post"}


def test_a_draft_is_upserted_with_both_times_and_no_props() -> None:
    server = FakeServer(reply({}, 201))
    run(server, upsert)
    (request,) = server.requests
    assert (request.method, request.url.path) == ("POST", "/api/v4/drafts")
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert body_of(request) == {
        "channel_id": "channel-1",
        "root_id": "root-1",
        "message": "Yes.",
        "props": {},
        "create_at": 1_700_000_000_000,
        "update_at": 1_700_000_000_000,
    }


@pytest.mark.parametrize("status", [200, 201])
def test_both_success_answers_to_an_upsert_are_accepted(status: int) -> None:
    run(FakeServer(reply({}, status)), upsert)


@pytest.mark.parametrize("message", ["", " ", "\n\t "])
def test_a_blank_draft_is_refused_before_any_request(message: str) -> None:
    """The server deletes the draft that is there when it gets an empty one."""
    server = FakeServer(reply({}, 201))
    with pytest.raises(WriteRefused, match="blank_draft"):
        run(server, lambda writer: writer.upsert_draft("channel-1", "", message, 1))
    assert server.requests == []


def test_a_post_carries_our_marker_and_goes_into_its_thread() -> None:
    server = FakeServer(reply({"id": "new-post", "message": "Yes."}, 201))
    assert run(server, post) == "new-post"
    (request,) = server.requests
    assert (request.method, request.url.path) == ("POST", "/api/v4/posts")
    assert body_of(request) == {
        "channel_id": "channel-1",
        "root_id": "root-1",
        "message": "Yes.",
        "props": {AGENT_PROP: True},
    }


def test_without_the_kill_switch_no_post_can_be_created() -> None:
    server = FakeServer(reply({"id": "new-post"}, 201))
    with pytest.raises(WriteRefused, match="posting_is_off"):
        run(server, post, allow_post=False)
    assert server.requests == []
    # Drafts are not posts: they still work.
    run(server, upsert, allow_post=False)
    assert [request.url.path for request in server.requests] == ["/api/v4/drafts"]


def test_a_blank_post_is_refused_before_any_request() -> None:
    server = FakeServer(reply({"id": "new-post"}, 201))
    with pytest.raises(WriteRefused, match="blank_post"):
        run(server, lambda writer: writer.create_post("channel-1", "", "  "))
    assert server.requests == []


def test_a_post_answered_with_anything_but_created_is_an_error() -> None:
    for status in (200, 202, 400, 403, 500):
        with pytest.raises(MattermostError) as error:
            run(FakeServer(reply({"id": "new-post"}, status)), post)
        assert (error.value.reason, error.value.status) == ("server_error", status)


@pytest.mark.parametrize(
    "response",
    [httpx2.Response(201, content=b"not json"), httpx2.Response(201, json={"id": ""})],
)
def test_a_created_post_without_a_readable_id_is_a_protocol_error(
    response: httpx2.Response,
) -> None:
    with pytest.raises(MattermostError) as error:
        run(FakeServer(lambda _: response), post)
    assert error.value.reason == "protocol_error"


def test_the_draft_listing() -> None:
    rows = [{"channel_id": "c", "root_id": "", "message": "mine"}]
    server = FakeServer(reply(rows))
    assert run(server, drafts) == [ChatDraft("c", "", "mine")]
    (request,) = server.requests
    assert (request.method, request.url.path) == (
        "GET",
        "/api/v4/users/me-id/teams/team-1/drafts",
    )


def test_no_drafts_is_answered_with_null() -> None:
    assert run(FakeServer(lambda _: httpx2.Response(200, content=b"null\n")), drafts) == []


def test_ids_cannot_change_the_path() -> None:
    server = FakeServer(reply([]))
    run(server, lambda writer: writer.list_drafts("../../admin", "a/b"))
    assert server.requests[0].url.raw_path == b"/api/v4/users/..%2F..%2Fadmin/teams/a%2Fb/drafts"


@pytest.mark.parametrize("call", [upsert, post, drafts])
def test_a_rejected_token_is_named(call: Callable[[MattermostWriter], Awaitable[object]]) -> None:
    with pytest.raises(MattermostError) as error:
        run(FakeServer(reply({}, 401)), call)
    assert (error.value.reason, error.value.status) == ("auth_rejected", 401)


@pytest.mark.parametrize("call", [upsert, drafts])
def test_synced_drafts_switched_off_answer_501(
    call: Callable[[MattermostWriter], Awaitable[object]],
) -> None:
    with pytest.raises(MattermostError) as error:
        run(FakeServer(reply({}, 501)), call)
    assert (error.value.reason, error.value.status) == ("server_error", 501)


@pytest.mark.parametrize("call", [upsert, post, drafts])
def test_no_connection_is_unreachable(
    call: Callable[[MattermostWriter], Awaitable[object]],
) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(MattermostError) as error:
        run(FakeServer(refuse), call)
    assert error.value.reason == "unreachable"


@pytest.mark.parametrize("call", [upsert, post, drafts])
def test_a_redirect_is_not_followed(call: Callable[[MattermostWriter], Awaitable[object]]) -> None:
    server = FakeServer(
        lambda _: httpx2.Response(307, headers={"Location": "https://evil.example.com/"})
    )
    with pytest.raises(MattermostError) as error:
        run(server, call)
    assert (error.value.reason, error.value.status) == ("server_error", 307)
    assert len(server.requests) == 1
    assert server.requests[0].url.host == "chat.example.com"


def test_error_text_quotes_neither_the_server_nor_the_reply() -> None:
    server = FakeServer(lambda _: httpx2.Response(400, content=b"sentinel-server-reply"))
    with pytest.raises(MattermostError) as error:
        run(server, lambda writer: writer.create_post("channel-1", "", SECRET))
    assert "sentinel-server-reply" not in str(error.value)
    assert SECRET not in str(error.value) and SECRET not in repr(error.value)
    with pytest.raises(MattermostError) as error:
        run(
            FakeServer(lambda _: httpx2.Response(201, content=b"sentinel-server-reply")),
            lambda writer: writer.create_post("channel-1", "", SECRET),
        )
    assert "sentinel-server-reply" not in str(error.value) and SECRET not in str(error.value)

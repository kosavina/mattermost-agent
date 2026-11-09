"""The client against fake servers: a fake REST transport and real WebSockets on localhost."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, cast

import httpx2
import pytest
from helpers import raw_post
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from mattermost_agent import mattermost
from mattermost_agent.events import Channel
from mattermost_agent.mattermost import (
    PAGE_SIZE,
    MattermostClient,
    MattermostError,
    open_events,
    websocket_url,
)

URL = "https://chat.example.com"
TOKEN = "abcdefghijklmnopqrstuvwxyz"
CHANNEL = Channel(id="channel-1", name="analytics", type="O", last_post_at=5)
USER = {"id": "user-id", "username": "example.user"}

type Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeServer:
    """Answer with `handler` and remember every request."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)


def run[T](server: FakeServer, call: Callable[[MattermostClient], Awaitable[T]]) -> T:
    async def main() -> T:
        client = MattermostClient(URL, TOKEN, transport=httpx2.MockTransport(server))
        try:
            return await call(client)
        finally:
            await client.aclose()

    return asyncio.run(main())


def reply(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    return lambda _: httpx2.Response(status, json=body, headers=headers)


def failure(call: Callable[[MattermostClient], Awaitable[object]], handler: Handler) -> Any:
    with pytest.raises(MattermostError) as error:
        run(FakeServer(handler), call)
    return error.value


def posts_body(*posts: tuple[str, int]) -> dict[str, Any]:
    """Return a REST page, newest first, from (id, create_at) pairs."""
    return {
        "order": [post_id for post_id, _ in posts],
        "posts": {post_id: raw_post(id=post_id, create_at=at) for post_id, at in posts},
    }


def get_me(client: MattermostClient) -> Awaitable[object]:
    return client.get_me()


def test_get_me_sends_the_token_and_reads_the_user() -> None:
    date = {"Date": "Sun, 08 Nov 2026 12:00:00 GMT"}
    server = FakeServer(reply(USER, headers=date))
    identity = run(server, lambda client: client.get_me())
    assert identity.me.user_id == "user-id"
    assert identity.me.username == "example.user"
    assert identity.server_time_ms == datetime(2026, 11, 8, 12, tzinfo=UTC).timestamp() * 1000
    assert server.requests[0].url.path == "/api/v4/users/me"
    assert server.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("headers", [None, {"Date": "not a date"}])
def test_server_time_is_optional(headers: dict[str, str] | None) -> None:
    identity = run(FakeServer(reply(USER, headers=headers)), lambda client: client.get_me())
    assert identity.server_time_ms is None


def test_a_server_under_a_path_keeps_it() -> None:
    server = FakeServer(reply(USER))

    async def main() -> None:
        client = MattermostClient(f"{URL}/chat", TOKEN, transport=httpx2.MockTransport(server))
        await client.get_me()
        await client.aclose()

    asyncio.run(main())
    assert server.requests[0].url.path == "/chat/api/v4/users/me"


def test_a_rejected_token_is_reported() -> None:
    error = failure(get_me, reply({"message": TOKEN}, status=401))
    assert error.reason == "auth_rejected"
    assert error.status == 401
    assert TOKEN not in str(error)


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_other_statuses_are_server_errors(status: int) -> None:
    error = failure(get_me, reply({}, status=status))
    assert error.reason == "server_error"
    assert error.status == status


def test_a_network_failure_is_unreachable() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    error = failure(get_me, refuse)
    assert error.reason == "unreachable"
    assert type(error.__cause__).__name__ == "ConnectError"


def test_a_redirect_is_not_followed() -> None:
    server = FakeServer(
        lambda _: httpx2.Response(302, headers={"Location": "https://evil.example.com/"})
    )
    with pytest.raises(MattermostError) as error:
        run(server, lambda client: client.get_me())
    assert error.value.reason == "server_error"
    assert error.value.status == 302
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "response",
    [httpx2.Response(200, content=b"not json"), httpx2.Response(200, json={"id": ""})],
)
def test_a_bad_reply_is_a_protocol_error(response: httpx2.Response) -> None:
    assert failure(get_me, lambda _: response).reason == "protocol_error"


def test_error_text_does_not_quote_the_server() -> None:
    error = failure(get_me, lambda _: httpx2.Response(200, content=b"sentinel-reply"))
    assert "sentinel-reply" not in str(error)


def test_the_channel_listing() -> None:
    row = {"id": "c", "name": "general", "type": "O", "last_post_at": 9}
    server = FakeServer(reply([row]))
    channels = run(server, lambda client: client.list_channels())
    assert channels == [Channel(id="c", name="general", type="O", last_post_at=9)]
    assert server.requests[0].url.path == "/api/v4/users/me/channels"


def test_a_page_reaching_the_floor_is_the_last() -> None:
    server = FakeServer(reply(posts_body(("c", 300), ("b", 200), ("a", 100))))
    result = run(server, lambda client: client.posts_since(CHANNEL, 150))
    assert [post.id for post in result.posts] == ["b", "c"]
    assert not result.truncated
    assert len(server.requests) == 1
    assert server.requests[0].url.path == "/api/v4/channels/channel-1/posts"
    assert server.requests[0].url.params["per_page"] == str(PAGE_SIZE)
    assert server.requests[0].url.params["page"] == "0"


def test_a_post_exactly_at_the_floor_is_left_out() -> None:
    server = FakeServer(reply(posts_body(("b", 200), ("a", 100))))
    result = run(server, lambda client: client.posts_since(CHANNEL, 100))
    assert [post.id for post in result.posts] == ["b"]


def test_pages_are_read_until_the_floor() -> None:
    pages = [
        posts_body(("f", 600), ("e", 500)),
        posts_body(("d", 400), ("c", 300)),
        posts_body(("b", 200), ("a", 100)),
    ]
    server = FakeServer(
        lambda request: httpx2.Response(200, json=pages[int(request.url.params["page"])])
    )
    result = run(server, lambda client: client.posts_since(CHANNEL, 150))
    assert [post.id for post in result.posts] == ["b", "c", "d", "e", "f"]
    assert len(server.requests) == 3


def test_a_short_page_does_not_end_the_reading() -> None:
    pages = [posts_body(("c", 300)), posts_body(("b", 200)), posts_body()]
    server = FakeServer(
        lambda request: httpx2.Response(200, json=pages[int(request.url.params["page"])])
    )
    result = run(server, lambda client: client.posts_since(CHANNEL, 50))
    assert [post.id for post in result.posts] == ["b", "c"]
    assert not result.truncated


def test_a_post_on_two_pages_counts_once() -> None:
    pages = [posts_body(("c", 300), ("b", 200)), posts_body(("b", 200), ("a", 100)), posts_body()]
    server = FakeServer(
        lambda request: httpx2.Response(200, json=pages[int(request.url.params["page"])])
    )
    result = run(server, lambda client: client.posts_since(CHANNEL, 50))
    assert [post.id for post in result.posts] == ["a", "b", "c"]


def test_an_endless_channel_is_reported_as_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mattermost, "MAX_PAGES", 3)

    def endless(request: httpx2.Request) -> httpx2.Response:
        page = int(request.url.params["page"])
        return httpx2.Response(200, json=posts_body((f"post-{page}", 1_000 - page)))

    server = FakeServer(endless)
    result = run(server, lambda client: client.posts_since(CHANNEL, 0))
    assert result.truncated
    assert len(server.requests) == 3
    assert [post.id for post in result.posts] == ["post-2", "post-1", "post-0"]


def test_usernames_are_cached() -> None:
    server = FakeServer(reply(USER))

    async def twice(client: MattermostClient) -> tuple[str | None, str | None]:
        return await client.username("user-id"), await client.username("user-id")

    assert run(server, twice) == ("example.user", "example.user")
    assert len(server.requests) == 1


def test_the_username_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mattermost, "MAX_CACHED_USERS", 2)
    server = FakeServer(reply(USER))

    async def scenario(client: MattermostClient) -> None:
        for user_id in ("a", "b", "c", "a"):
            await client.username(user_id)

    run(server, scenario)
    assert [request.url.path.rsplit("/", 1)[1] for request in server.requests] == [
        "a",
        "b",
        "c",
        "a",
    ]


def test_unknown_user_has_no_username() -> None:
    server = FakeServer(reply({}, status=404))
    assert run(server, lambda client: client.username("gone")) is None


def test_a_failed_lookup_is_not_cached() -> None:
    answers = [httpx2.Response(500, json={}), httpx2.Response(200, json=USER)]
    server = FakeServer(lambda _: answers.pop(0))

    async def scenario(client: MattermostClient) -> str | None:
        with pytest.raises(MattermostError):
            await client.username("user-id")
        return await client.username("user-id")

    assert run(server, scenario) == "example.user"


def test_ids_cannot_change_the_path() -> None:
    server = FakeServer(reply(USER))
    run(server, lambda client: client.username("../../admin"))
    assert server.requests[0].url.raw_path.endswith(b"/users/..%2F..%2Fadmin")


def test_every_call_is_a_get() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/channels"):
            return httpx2.Response(200, json=[])
        if request.url.path.endswith("/posts"):
            return httpx2.Response(200, json=posts_body())
        return httpx2.Response(200, json=USER)

    server = FakeServer(respond)

    async def everything(client: MattermostClient) -> None:
        await client.get_me()
        await client.list_channels()
        await client.username("user-id")
        await client.posts_since(CHANNEL, 0)

    run(server, everything)
    assert len(server.requests) == 4
    assert {request.method for request in server.requests} == {"GET"}


def test_the_client_has_no_way_to_write() -> None:
    public = {name for name in vars(MattermostClient) if not name.startswith("_")}
    assert public == {"aclose", "get_me", "list_channels", "username", "posts_since"}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://chat.example.com", "wss://chat.example.com/api/v4/websocket"),
        ("https://chat.example.com:8443", "wss://chat.example.com:8443/api/v4/websocket"),
        ("https://example.com/chat", "wss://example.com/chat/api/v4/websocket"),
    ],
)
def test_websocket_url(given: str, expected: str) -> None:
    assert websocket_url(given) == expected


def port_of(server: Server) -> int:
    return next(iter(server.sockets)).getsockname()[1]


def address_of(server: Server) -> str:
    return f"ws://127.0.0.1:{port_of(server)}/api/v4/websocket"


async def greet(connection: ServerConnection) -> None:
    await connection.send(json.dumps({"event": "hello", "data": {}, "seq": 0}))
    await connection.wait_closed()


async def stay_silent(connection: ServerConnection) -> None:
    await connection.wait_closed()


def test_events_connection_sends_the_token_and_waits_for_hello() -> None:
    seen: list[str | None] = []

    def record(_: ServerConnection, request: Request) -> None:
        seen.append(request.headers.get("Authorization"))

    async def main() -> None:
        async with serve(greet, "127.0.0.1", 0, process_request=record) as server:
            socket = await open_events(address_of(server), TOKEN)
            await socket.close()

    asyncio.run(main())
    assert seen == [f"Bearer {TOKEN}"]


def test_frames_before_the_hello_are_skipped() -> None:
    async def noisy(connection: ServerConnection) -> None:
        await connection.send(json.dumps({"status": "OK"}))
        await greet(connection)

    async def main() -> None:
        async with serve(noisy, "127.0.0.1", 0) as server:
            socket = await open_events(address_of(server), TOKEN)
            await socket.close()

    asyncio.run(main())


def websocket_failure(
    handler: Callable[[ServerConnection], Awaitable[None]],
    process_request: Any = None,
    hello_timeout: float = 5.0,
) -> MattermostError:
    async def main() -> None:
        async with serve(
            cast(Any, handler), "127.0.0.1", 0, process_request=process_request
        ) as server:
            await open_events(address_of(server), TOKEN, hello_timeout=hello_timeout)

    with pytest.raises(MattermostError) as error:
        asyncio.run(main())
    return error.value


def refuse_with(status: HTTPStatus) -> Callable[[ServerConnection, Request], Response]:
    return lambda connection, _: connection.respond(status, "refused\n")


def test_a_silent_server_is_a_protocol_error() -> None:
    assert websocket_failure(stay_silent, hello_timeout=0.05).reason == "protocol_error"


def test_a_server_that_hangs_up_is_unreachable() -> None:
    async def hang_up(connection: ServerConnection) -> None:
        await connection.close()

    assert websocket_failure(hang_up).reason == "unreachable"


def test_garbage_instead_of_a_hello_is_a_protocol_error() -> None:
    async def garbage(connection: ServerConnection) -> None:
        await connection.send("not json")
        await connection.wait_closed()

    assert websocket_failure(garbage).reason == "protocol_error"


def test_a_rejected_upgrade_is_reported() -> None:
    error = websocket_failure(greet, process_request=refuse_with(HTTPStatus.UNAUTHORIZED))
    assert error.reason == "auth_rejected"
    assert error.status == 401


def test_another_refused_upgrade_is_a_server_error() -> None:
    error = websocket_failure(greet, process_request=refuse_with(HTTPStatus.BAD_GATEWAY))
    assert error.reason == "server_error"
    assert error.status == 502


def test_a_redirect_on_the_upgrade_is_refused_and_the_token_stays() -> None:
    reached: list[str | None] = []

    def record(_: ServerConnection, request: Request) -> None:
        reached.append(request.headers.get("Authorization"))

    async def main() -> MattermostError:
        async with serve(greet, "127.0.0.1", 0, process_request=record) as target:
            location = address_of(target)

            def redirect(connection: ServerConnection, _: Request) -> Response:
                response = connection.respond(HTTPStatus.FOUND, "moved\n")
                response.headers = Headers(Location=location)
                return response

            async with serve(greet, "127.0.0.1", 0, process_request=redirect) as front:
                with pytest.raises(MattermostError) as error:
                    await open_events(address_of(front), TOKEN)
                return error.value

    error = asyncio.run(main())
    assert error.status == 302
    assert reached == []


def test_nothing_listening_is_unreachable() -> None:
    async def main() -> MattermostError:
        async with serve(greet, "127.0.0.1", 0) as server:
            address = address_of(server)
        with pytest.raises(MattermostError) as error:
            await open_events(address, TOKEN)
        return error.value

    assert asyncio.run(main()).reason == "unreachable"

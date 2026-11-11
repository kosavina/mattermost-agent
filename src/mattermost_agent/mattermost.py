"""The code that reads from the Mattermost server.

Every REST call here is a GET, and the WebSocket is only ever read from, so nothing in this
module can post. The few calls that write are in mattermost_write.py, which only deliver.py may
import. The token goes only to the configured server: redirects are refused on both paths,
because following one could carry the token to another host, and proxy settings in the
environment are ignored.

Failures become MattermostError, whose text is written here and never quotes a server's reply.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx2
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from mattermost_agent.events import (
    Channel,
    MalformedEvent,
    Me,
    Post,
    is_hello,
    parse_channel_list,
    parse_post_page,
    parse_user,
)
from mattermost_agent.logs import Reason

API = "/api/v4"
REQUEST_TIMEOUT_SECONDS = 10.0
REQUEST_DEADLINE_SECONDS = 30.0
OPEN_TIMEOUT_SECONDS = 15.0
HELLO_TIMEOUT_SECONDS = 10.0
PING_SECONDS = 20.0
CLOSE_TIMEOUT_SECONDS = 2.0
# websockets stops reading at 16 waiting frames, and that would also stop it noticing the replies
# to its keep-alive pings while catch-up runs. Live events wait here until catch-up has finished.
MAX_QUEUED_FRAMES = 1024
PAGE_SIZE = 200
MAX_PAGES = 500
MAX_CACHED_USERS = 1024


def _describe(reason: Reason, status: int | None) -> str:
    match reason:
        case "auth_rejected":
            return f"Mattermost rejected the token (HTTP {status}): check the personal access token"
        case "unreachable":
            return "cannot reach the Mattermost server"
        case "protocol_error":
            return "the Mattermost server sent an unexpected reply"
        case _:
            return f"the Mattermost server answered HTTP {status}"


class MattermostError(Exception):
    """A call failed. `reason` and `status` are safe to log, and the text is safe to show."""

    def __init__(self, reason: Reason, status: int | None = None) -> None:
        super().__init__(_describe(reason, status))
        self.reason: Reason = reason
        self.status = status


@dataclass(frozen=True)
class Identity:
    """Who the token belongs to, and the server's clock (Unix ms) if it told us."""

    me: Me
    server_time_ms: int | None


@dataclass(frozen=True)
class PostsSince:
    """Posts newer than a time, oldest first. `truncated` means the page limit was reached."""

    posts: list[Post]
    truncated: bool


def _server_time_ms(response: httpx2.Response) -> int | None:
    raw = response.headers.get("Date")
    if raw is None:
        return None
    try:
        return int(parsedate_to_datetime(raw).timestamp() * 1000)
    except TypeError, ValueError:
        return None


def _parsed[T](response: httpx2.Response, parse: Callable[[Any], T]) -> T:
    try:
        return parse(response.json())
    except (ValueError, RecursionError, MalformedEvent) as error:
        raise MattermostError("protocol_error") from error


class MattermostClient:
    """Read-only REST calls. Pass `transport` to replace the network in tests."""

    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._usernames: OrderedDict[str, str] = OrderedDict()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, int] | None = None) -> httpx2.Response:
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                response = await self._client.get(f"{API}{path}", params=params)
        except (httpx2.HTTPError, TimeoutError) as error:
            raise MattermostError("unreachable") from error
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            raise MattermostError("auth_rejected", response.status_code)
        if response.status_code != HTTPStatus.OK:
            raise MattermostError("server_error", response.status_code)
        return response

    async def get_me(self) -> Identity:
        response = await self._get("/users/me")
        return Identity(_parsed(response, parse_user), _server_time_ms(response))

    async def list_channels(self) -> list[Channel]:
        """Return every channel the user is in, across all teams, with DMs and group DMs."""
        return _parsed(await self._get("/users/me/channels"), parse_channel_list)

    async def username(self, user_id: str) -> str | None:
        """Return a user's username, or None if the server has no such user."""
        if user_id in self._usernames:
            self._usernames.move_to_end(user_id)
            return self._usernames[user_id]
        try:
            response = await self._get(f"/users/{quote(user_id, safe='')}")
        except MattermostError as error:
            if error.status == HTTPStatus.NOT_FOUND:
                return None
            raise
        username = _parsed(response, parse_user).username
        self._usernames[user_id] = username
        if len(self._usernames) > MAX_CACHED_USERS:
            self._usernames.popitem(last=False)
        return username

    async def posts_since(self, channel: Channel, floor: int) -> PostsSince:
        """Return the channel's posts created after `floor`, by reading back page by page.

        Reading stops only at an empty page or a post at or before `floor`, and not at a short
        page, because a page can come back shorter than asked while older posts remain.
        """
        path = f"/channels/{quote(channel.id, safe='')}/posts"
        collected: dict[str, Post] = {}
        for page_number in range(MAX_PAGES):
            params = {"page": page_number, "per_page": PAGE_SIZE}
            response = await self._get(path, params)
            page = _parsed(response, lambda body: parse_post_page(body, channel))
            collected.update({post.id: post for post in page if post.create_at > floor})
            if not page or min(post.create_at for post in page) <= floor:
                truncated = False
                break
        else:
            truncated = True
        return PostsSince(sorted(collected.values(), key=lambda post: post.create_at), truncated)


class _NoRedirectConnect(connect):
    """Refuse redirects on the upgrade, so that the token cannot be carried to another host."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


def websocket_url(base_url: str) -> str:
    """Return the WebSocket address of the checked https address in the settings."""
    parts = urlsplit(base_url)
    return urlunsplit(("wss", parts.netloc, f"{parts.path}{API}/websocket", "", ""))


async def _wait_for_hello(socket: ClientConnection, timeout: float) -> None:
    try:
        async with asyncio.timeout(timeout):
            while not is_hello(await socket.recv()):
                pass
    except (TimeoutError, MalformedEvent) as error:
        raise MattermostError("protocol_error") from error
    except ConnectionClosed as error:
        raise MattermostError("unreachable") from error


async def open_events(
    url: str,
    token: str,
    *,
    hello_timeout: float = HELLO_TIMEOUT_SECONDS,
) -> ClientConnection:
    """Open the WebSocket, wait for the server's hello, and return the connection."""
    try:
        socket = await _NoRedirectConnect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            proxy=None,
            open_timeout=OPEN_TIMEOUT_SECONDS,
            ping_interval=PING_SECONDS,
            ping_timeout=PING_SECONDS,
            close_timeout=CLOSE_TIMEOUT_SECONDS,
            max_queue=MAX_QUEUED_FRAMES,
        )
    except InvalidStatus as error:
        status = error.response.status_code
        reason: Reason = "auth_rejected" if status == HTTPStatus.UNAUTHORIZED else "server_error"
        raise MattermostError(reason, status) from error
    except (OSError, TimeoutError, WebSocketException) as error:
        raise MattermostError("unreachable") from error
    try:
        await _wait_for_hello(socket, hello_timeout)
    except BaseException:
        await socket.close()
        raise
    return socket

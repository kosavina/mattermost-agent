"""The only code that writes to Mattermost: one draft, or one post, per call.

Three calls and no others: put a draft into a message box, list the drafts that are already
there, and create a post. deliver.py is the only module that may import this one, and a test
checks that. The reading client in mattermost.py stays as it was, GET only.

The same care as there: the token goes only to the configured server, redirects are refused,
proxy settings in the environment are ignored, and a failure becomes MattermostError, whose text
never quotes the server's reply. Two more locks sit here, under the checks in deliver.py: without
`allow_post` no post can be created whatever the caller does, and a blank draft is refused,
because the server treats an empty draft as "delete the draft that is there".
"""

import asyncio
from collections.abc import Callable
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx2

from mattermost_agent.events import (
    AGENT_PROP,
    ChatDraft,
    MalformedEvent,
    parse_created_post,
    parse_drafts,
)
from mattermost_agent.mattermost import (
    API,
    REQUEST_DEADLINE_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    MattermostError,
)


class WriteRefused(Exception):
    """Our own code asked for a write that must never happen. The message is the reason only."""


def _parsed[T](response: httpx2.Response, parse: Callable[[Any], T]) -> T:
    try:
        return parse(response.json())
    except (ValueError, RecursionError, MalformedEvent) as error:
        raise MattermostError("protocol_error") from error


class MattermostWriter:
    """The three writing calls. Pass `transport` to replace the network in tests."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        allow_post: bool,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_post = allow_post
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self, method: str, path: str, body: dict[str, object] | None, expected: frozenset[int]
    ) -> httpx2.Response:
        try:
            async with asyncio.timeout(REQUEST_DEADLINE_SECONDS):
                response = await self._client.request(method, f"{API}{path}", json=body)
        except (httpx2.HTTPError, TimeoutError) as error:
            raise MattermostError("unreachable") from error
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            raise MattermostError("auth_rejected", response.status_code)
        if response.status_code not in expected:
            raise MattermostError("server_error", response.status_code)
        return response

    async def list_drafts(self, user_id: str, team_id: str) -> list[ChatDraft]:
        """Return the user's drafts that the server lists under one team."""
        path = f"/users/{quote(user_id, safe='')}/teams/{quote(team_id, safe='')}/drafts"
        response = await self._send("GET", path, None, frozenset({HTTPStatus.OK}))
        return _parsed(response, parse_drafts)

    async def upsert_draft(self, channel_id: str, root_id: str, message: str, now_ms: int) -> None:
        """Put `message` into the message box of a chat, or of a thread if `root_id` is set.

        It replaces whatever draft is there, so the caller looks first. The draft has no props:
        the post Kirill makes of it must not look like one of ours.
        """
        if not message.strip():
            raise WriteRefused("blank_draft")
        body: dict[str, object] = {
            "channel_id": channel_id,
            "root_id": root_id,
            "message": message,
            "props": {},
            # The server answers 500 without both times.
            "create_at": now_ms,
            "update_at": now_ms,
        }
        await self._send("POST", "/drafts", body, frozenset({HTTPStatus.OK, HTTPStatus.CREATED}))

    async def create_post(self, channel_id: str, root_id: str, message: str) -> str:
        """Post `message` as the token's user, with our marker, and return the new post's id."""
        if not self._allow_post:
            raise WriteRefused("posting_is_off")
        if not message.strip():
            raise WriteRefused("blank_post")
        body: dict[str, object] = {
            "channel_id": channel_id,
            "root_id": root_id,
            "message": message,
            "props": {AGENT_PROP: True},
        }
        response = await self._send("POST", "/posts", body, frozenset({HTTPStatus.CREATED}))
        return _parsed(response, parse_created_post)

"""One plain log line per event.

The parameters of log_post and log_lifecycle are the allow-list of what can be logged. Neither
has a parameter for message text, a token or a header, so calling code has no way to pass them.
"""

import json
import logging
import sys
from typing import Literal, TextIO

LOGGER_NAME = "mattermost_agent"
# At DEBUG these print request headers and WebSocket frames, so LOG_LEVEL must not reach them.
LIBRARY_LOGGERS = ("websockets", "httpx2", "httpcore2")
MAX_LOGGED_TEXT = 253
NO_VALUE = "null"

logger = logging.getLogger(LOGGER_NAME)

type PostEvent = Literal["post", "cap_hit"]
type LifecycleEvent = Literal[
    "startup",
    "connected",
    "disconnected",
    "reconnect_wait",
    "catchup_start",
    "catchup_done",
    "catchup_truncated",
    "frame_skipped",
    "shutdown",
    "stopped",
    "internal_error",
    "fatal",
]
type Reason = Literal[
    "auth_rejected", "unreachable", "server_error", "protocol_error", "store_error", "signal"
]


class OneLineFormatter(logging.Formatter):
    """Escape line breaks, so that a traceback stays one event in `docker logs` and grep."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace("\r", "\\r").replace("\n", "\\n")


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Print one plain line per event (default stdout). `level` applies to this app only."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(OneLineFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    levels = {LOGGER_NAME: level, **dict.fromkeys(LIBRARY_LOGGERS, "WARNING")}
    for name, logger_level in levels.items():
        configured = logging.getLogger(name)
        configured.handlers[:] = [handler]
        configured.setLevel(logger_level)
        configured.propagate = False


def _quoted(value: str | None) -> str:
    """Bound and quote text that others control, so that it cannot break or forge a log line."""
    return NO_VALUE if value is None else json.dumps(value[:MAX_LOGGED_TEXT])


def log_post(
    event: PostEvent,
    *,
    kind: str,
    channel: str | None,
    sender: str | None,
    length: int,
    rule: str | None,
    scenario: str | None,
) -> None:
    """Write the one line for a post. `length` is the only thing logged about its text."""
    logger.info(
        # Padded so that columns line up. A longer value only shifts the rest of its own line.
        "event=%-7s kind=%-12s channel=%-26s sender=%-20s length=%-5d rule=%-18s scenario=%s",
        event,
        kind,
        _quoted(channel),
        _quoted(sender),
        length,
        _quoted(rule),
        _quoted(scenario),
    )


def log_lifecycle(
    event: LifecycleEvent,
    *,
    reason: Reason | None = None,
    error: BaseException | None = None,
    status: int | None = None,
    count: int | None = None,
    delay_seconds: float | None = None,
) -> None:
    """Write a line about the app itself. Of `error`, only the class name is ever logged."""
    logger.info(
        "event=%-17s reason=%-14s error=%-20s status=%-4s count=%-6s delay_s=%s",
        event,
        reason or NO_VALUE,
        NO_VALUE if error is None else type(error).__name__,
        NO_VALUE if status is None else status,
        NO_VALUE if count is None else count,
        NO_VALUE if delay_seconds is None else f"{delay_seconds:.1f}",
    )

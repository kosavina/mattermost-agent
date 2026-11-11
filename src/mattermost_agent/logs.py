"""One plain log line per event.

The parameters of the log_* functions are the allow-list of what can be logged. None has a
parameter for message text, a memory, a draft, a prompt, a token or a header, so calling code has
no way to pass them. Of an agent run, only the backend, the decision, the duration, the attempt
and the number of refused tool calls are logged; of a reply, only how it will be delivered and
its length.
"""

import json
import logging
import sys
from typing import Literal, TextIO

from mattermost_agent.store import FailReason, JobStatus

LOGGER_NAME = "mattermost_agent"
# At DEBUG these print request headers and WebSocket frames, so LOG_LEVEL must not reach them.
# langgraph logs a retried node's exception text at INFO; WARNING keeps that out as well.
LIBRARY_LOGGERS = ("websockets", "httpx2", "httpcore2", "langgraph", "langchain_core")
MAX_LOGGED_TEXT = 253
NO_VALUE = "null"

logger = logging.getLogger(LOGGER_NAME)

type PostEvent = Literal["post", "cap_hit"]
type JobEvent = Literal["job_opened", "job_joined", "job_ready"]
type LifecycleEvent = Literal[
    "startup",
    "connected",
    "disconnected",
    "reconnect_wait",
    "catchup_start",
    "catchup_done",
    "catchup_truncated",
    "channel_skipped",
    "lookup_failed",
    "frame_skipped",
    "jobs_pruned",
    "drafts_expired",
    "threads_pruned",
    "shutdown",
    "stopped",
    "internal_error",
    "fatal",
]
type Reason = Literal[
    "auth_rejected", "unreachable", "server_error", "protocol_error", "store_error", "signal"
]
type AgentErrorReason = Literal[
    "exit", "timeout", "invalid_json", "invalid_output", "denied_startup"
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


def log_job(
    event: JobEvent,
    *,
    job_id: int,
    status: JobStatus,
    scenario: str,
    rule: str,
    place: str | None,
    post_count: int,
) -> None:
    """Write the one line for a change to a job. Nothing of its posts is logged but their number."""
    logger.info(
        "event=%-10s job=%-5d status=%-10s scenario=%-10s rule=%-18s place=%-26s posts=%d",
        event,
        job_id,
        status,
        _quoted(scenario),
        _quoted(rule),
        _quoted(place),
        post_count,
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


def log_agent_run(
    *,
    job_id: int,
    backend: str,
    scenario: str,
    decision: str,
    duration_s: float,
    attempt: int,
    denials: int,
) -> None:
    """Write the one line for a finished agent run. Nothing of its answer but the decision."""
    logger.info(
        "event=%-10s job=%-5d backend=%-7s scenario=%-10s decision=%-9s duration_s=%-6.1f "
        "attempt=%d denials=%d",
        "agent_run",
        job_id,
        backend,
        _quoted(scenario),
        decision,
        duration_s,
        attempt,
        denials,
    )


def log_agent_error(
    *,
    job_id: int,
    backend: str,
    reason: AgentErrorReason,
    exit_code: int | None,
    attempt: int,
) -> None:
    """Write the one line for a failed agent run: the reason word and the exit code, no output."""
    logger.info(
        "event=%-10s job=%-5d backend=%-7s reason=%-14s exit_code=%-4s attempt=%d",
        "agent_error",
        job_id,
        backend,
        reason,
        NO_VALUE if exit_code is None else exit_code,
        attempt,
    )


def log_memory_saved(*, job_id: int, channel_id: str, chars: int, version: int) -> None:
    """Write the one line for a new memory version: its length and number, never its text."""
    logger.info(
        "event=%-10s job=%-5d channel=%-26s chars=%-5d version=%d",
        "memory_saved",
        job_id,
        _quoted(channel_id),
        chars,
        version,
    )


def log_job_done(*, job_id: int, status: JobStatus, fail_reason: FailReason | None) -> None:
    """Write the one line for a job leaving the processor's hands."""
    logger.info(
        "event=%-10s job=%-5d status=%-16s fail_reason=%s",
        "job_done",
        job_id,
        status,
        fail_reason or NO_VALUE,
    )


def log_reply_planned(
    *, job_id: int, mode: str, downgrade: str | None, outcome: str, chars: int
) -> None:
    """Write the one line for a reply handed to the outbox: how it goes out, and its length.

    `downgrade` names the check that turned a direct reply into a draft, and `outcome` is
    `queued`, or `refused_command` for a reply that must reach no message box.
    """
    logger.info(
        "event=%-10s job=%-5d mode=%-5s downgrade=%-12s outcome=%-16s chars=%d",
        "reply_planned",
        job_id,
        mode,
        downgrade or NO_VALUE,
        outcome,
        chars,
    )

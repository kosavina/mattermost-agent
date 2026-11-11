"""The `jobs` and `outbox` commands: show the queues on the terminal, without the logger.

The listings name each job and each reply without its text. One job's posts are shown with their
text, which is why it goes to the terminal and never to the log. Everything shown was written by
Mattermost users, so control characters are escaped first: a message could otherwise carry an
escape sequence that moves the cursor, changes the title, or worse.
"""

import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from mattermost_agent.settings import DEFAULT_STATE_DIR
from mattermost_agent.store import (
    FILE_NAME,
    Job,
    JobPost,
    OutboxRow,
    StoreError,
    read_job,
    read_jobs,
    read_outbox,
)

# C0 and C1 control characters and DEL, except newline and tab, which keep a message readable.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
NO_PLACE = "-"
NO_VALUE = "-"
COLUMNS = (
    f"{'id':>5}  {'status':<16} {'scenario':<12} {'rule':<20} {'place':<26} {'posts':>5}  "
    f"{'opened':<19}  {'ready':<19}  {'backend':<7} {'decision':<8} {'reply':<5} "
    f"{'delivery':<16} fail_reason"
)
OUTBOX_COLUMNS = (
    f"{'job':>5}  {'mode':<5} {'place':<26} {'state':<10} {'outcome':<16} {'downgrade':<12} "
    f"{'reason':<15} age"
)


def safe_text(text: str) -> str:
    """Escape control characters other than newline and tab, so text cannot drive the terminal."""
    return CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _when(seconds: float) -> str:
    """Return a time in this machine's local time zone, or the raw number if it is out of range."""
    try:
        return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M:%S")
    except OverflowError, OSError, ValueError:
        return f"{seconds:.0f}"


def age(seconds: float) -> str:
    """Return a length of time as hours and minutes."""
    minutes = max(0, int(seconds // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _line(job: Job) -> str:
    place = NO_PLACE if job.place is None else safe_text(job.place)
    return (
        f"{job.id:>5}  {job.status:<16} {safe_text(job.scenario):<12} {safe_text(job.rule):<20} "
        f"{place:<26} {job.post_count:>5}  {_when(job.opened_at)}  {_when(job.ready_at)}  "
        f"{job.backend or NO_VALUE:<7} {job.decision or NO_VALUE:<8} "
        f"{job.reply_mode or NO_VALUE:<5} {job.delivery or NO_VALUE:<16} "
        f"{job.fail_reason or NO_VALUE}"
    )


def format_jobs(jobs: list[Job]) -> str:
    """Return one line per job, as given, with no message text."""
    if not jobs:
        return "no jobs yet\n"
    return "\n".join([COLUMNS, *(_line(job) for job in jobs)]) + "\n"


def format_job(job: Job, posts: list[JobPost]) -> str:
    """Return the job's line, then each of its posts with its text."""
    parts = [COLUMNS, _line(job), ""]
    for post in posts:
        heading = f"--- {_when(post.create_at / 1000)} kind={post.kind}"
        parts += [heading, safe_text(post.message), ""]
    return "\n".join(parts) + "\n"


def format_outbox(rows: list[OutboxRow], now: float) -> str:
    """Return one line per reply queued for Mattermost, as given, with no text."""
    if not rows:
        return "no replies yet\n"
    lines = [OUTBOX_COLUMNS]
    for row in rows:
        place = NO_PLACE if row.place is None else safe_text(row.place)
        lines.append(
            f"{row.job_id:>5}  {row.mode:<5} {place:<26} {row.state:<10} "
            f"{row.outcome or NO_VALUE:<16} {row.downgrade or NO_VALUE:<12} "
            f"{row.reason or NO_VALUE:<15} {age(now - row.created_at)}"
        )
    return "\n".join(lines) + "\n"


def show_outbox(env: Mapping[str, str]) -> int:
    """Print the replies queued for Mattermost to stdout. Return the exit code."""
    path = Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR) / FILE_NAME
    try:
        text = format_outbox(read_outbox(path), now=time.time())
    except StoreError as error:
        raise SystemExit(f"mattermost-agent: {error}") from None
    sys.stdout.write(text)
    return 0


def show_jobs(env: Mapping[str, str], job_id: int | None) -> int:
    """Print the queue, or one job with its posts, to stdout. Return the exit code."""
    path = Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR) / FILE_NAME
    try:
        if job_id is None:
            text = format_jobs(read_jobs(path))
        else:
            found = read_job(path, job_id)
            if found is None:
                raise SystemExit(f"mattermost-agent: no job {job_id}")
            text = format_job(*found)
    except StoreError as error:
        raise SystemExit(f"mattermost-agent: {error}") from None
    sys.stdout.write(text)
    return 0

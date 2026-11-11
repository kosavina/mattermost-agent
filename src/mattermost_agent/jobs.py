"""The `jobs` command: show the queue on the terminal, without going through the logger.

The listing names each job without its text. One job's posts are shown with their text, which is
why it goes to the terminal and never to the log. Everything shown was written by Mattermost
users, so control characters are escaped first: a message could otherwise carry an escape
sequence that moves the cursor, changes the title, or worse.
"""

import re
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from mattermost_agent.settings import DEFAULT_STATE_DIR
from mattermost_agent.store import FILE_NAME, Job, JobPost, StoreError, read_job, read_jobs

# C0 and C1 control characters and DEL, except newline and tab, which keep a message readable.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
NO_PLACE = "-"
NO_VALUE = "-"
COLUMNS = (
    f"{'id':>5}  {'status':<16} {'scenario':<12} {'rule':<20} {'place':<26} {'posts':>5}  "
    f"{'opened':<19}  {'ready':<19}  {'backend':<7} {'decision':<8} fail_reason"
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


def _line(job: Job) -> str:
    place = NO_PLACE if job.place is None else safe_text(job.place)
    return (
        f"{job.id:>5}  {job.status:<16} {safe_text(job.scenario):<12} {safe_text(job.rule):<20} "
        f"{place:<26} {job.post_count:>5}  {_when(job.opened_at)}  {_when(job.ready_at)}  "
        f"{job.backend or NO_VALUE:<7} {job.decision or NO_VALUE:<8} {job.fail_reason or NO_VALUE}"
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

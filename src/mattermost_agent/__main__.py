"""The command line: the listener by default, the processor with `process`, and the commands that
show or change the queue on the terminal.

The processor's modules are imported only by the commands that need them: the listener's image
does not ship LangGraph, and the processor's own modules never import the Mattermost client.
The listener is the only process that holds the token, so it is also the one that delivers the
processor's replies. Errors are shown as one line. A traceback could quote the data that caused
it.
"""

import asyncio
import os
import re
import sys

from mattermost_agent.app import HEARTBEAT_FILE, Runtime, healthcheck, serve
from mattermost_agent.deliver import Deliverer, build
from mattermost_agent.jobs import show_jobs, show_outbox
from mattermost_agent.logs import configure_logging, log_lifecycle
from mattermost_agent.mattermost import (
    MattermostClient,
    MattermostError,
    open_events,
    websocket_url,
)
from mattermost_agent.rules import RulesError, load_rules
from mattermost_agent.settings import SettingsError, load_settings
from mattermost_agent.store import FILE_NAME, Store, StoreError

USAGE = (
    "usage: mattermost-agent [healthcheck | jobs [id] | outbox | process | drafts | "
    "approve <id> | reject <id> | memory <channel_id> | memory forget <channel_id>]"
)
JOB_ID = re.compile(r"[0-9]{1,18}")
CHANNEL_ID = re.compile(r"[a-z0-9]{26}")


def _command(arguments: list[str]) -> int | None:
    """Run a terminal command and return its exit code, or None if it is the listener's turn."""
    match arguments:
        case ["healthcheck"]:
            return healthcheck(os.environ)
        case ["jobs"]:
            return show_jobs(os.environ, None)
        case ["jobs", job_id] if JOB_ID.fullmatch(job_id):
            return show_jobs(os.environ, int(job_id))
        case ["outbox"]:
            return show_outbox(os.environ)
        case ["process"]:
            from mattermost_agent.processor.loop import main as process

            return process(os.environ)
        case ["drafts"]:
            from mattermost_agent.processor.cli import show_drafts

            return show_drafts(os.environ)
        case ["approve" | "reject" as verb, job_id] if JOB_ID.fullmatch(job_id):
            from mattermost_agent.processor.cli import decide_draft

            return decide_draft(os.environ, int(job_id), approved=verb == "approve")
        case ["memory", channel_id] if CHANNEL_ID.fullmatch(channel_id):
            from mattermost_agent.processor.cli import show_memory

            return show_memory(os.environ, channel_id)
        case ["memory", "forget", channel_id] if CHANNEL_ID.fullmatch(channel_id):
            from mattermost_agent.processor.cli import forget_memory

            return forget_memory(os.environ, channel_id)
        case []:
            return None
    raise SystemExit(USAGE)


async def _serve(runtime: Runtime, deliverer: Deliverer) -> None:
    try:
        await serve(runtime)
    finally:
        await deliverer.aclose()


def _listen() -> None:
    try:
        settings = load_settings(os.environ)
        configure_logging(settings.log_level)
        rules = load_rules(settings.rules_file)
        store = Store(settings.state_dir / FILE_NAME)
    except (SettingsError, RulesError, StoreError) as error:
        raise SystemExit(f"mattermost-agent: {error}") from None

    events_url = websocket_url(settings.mm_url)
    deliverer = build(settings.mm_url, settings.token, allow_send=settings.allow_send)
    runtime = Runtime(
        client=MattermostClient(settings.mm_url, settings.token),
        store=store,
        rules=rules,
        connect_events=lambda: open_events(events_url, settings.token),
        heartbeat_file=settings.state_dir / HEARTBEAT_FILE,
        deliver=deliverer.run,
    )
    try:
        asyncio.run(_serve(runtime, deliverer))
    except MattermostError as error:
        log_lifecycle("fatal", reason=error.reason, status=error.status, error=error.__cause__)
        raise SystemExit(f"mattermost-agent: {error}") from None
    except StoreError as error:
        log_lifecycle("fatal", reason="store_error", error=error.__cause__)
        raise SystemExit(f"mattermost-agent: {error}") from None
    except Exception as error:
        log_lifecycle("fatal", error=error)
        raise SystemExit(f"mattermost-agent: stopped by {type(error).__name__}") from None


def main() -> None:
    code = _command(sys.argv[1:])
    if code is not None:
        sys.exit(code)
    _listen()


if __name__ == "__main__":
    main()

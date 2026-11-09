"""Start the listener, or with `healthcheck`, report whether a running one is alive.

Errors are shown as one line. A traceback could quote the data that caused it.
"""

import asyncio
import os
import sys

from mattermost_agent.app import HEARTBEAT_FILE, Runtime, healthcheck, serve
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


def main() -> None:
    arguments = sys.argv[1:]
    if arguments == ["healthcheck"]:
        sys.exit(healthcheck(os.environ))
    if arguments:
        raise SystemExit("usage: mattermost-agent [healthcheck]")

    try:
        settings = load_settings(os.environ)
        configure_logging(settings.log_level)
        rules = load_rules(settings.rules_file)
        store = Store(settings.state_dir / FILE_NAME)
    except (SettingsError, RulesError, StoreError) as error:
        raise SystemExit(f"mattermost-agent: {error}") from None

    events_url = websocket_url(settings.mm_url)
    runtime = Runtime(
        client=MattermostClient(settings.mm_url, settings.token),
        store=store,
        rules=rules,
        connect_events=lambda: open_events(events_url, settings.token),
        heartbeat_file=settings.state_dir / HEARTBEAT_FILE,
    )
    try:
        asyncio.run(serve(runtime))
    except MattermostError as error:
        log_lifecycle("fatal", reason=error.reason, status=error.status, error=error.__cause__)
        raise SystemExit(f"mattermost-agent: {error}") from None
    except StoreError as error:
        log_lifecycle("fatal", reason="store_error", error=error.__cause__)
        raise SystemExit(f"mattermost-agent: {error}") from None
    except Exception as error:
        log_lifecycle("fatal", error=error)
        raise SystemExit(f"mattermost-agent: stopped by {type(error).__name__}") from None


if __name__ == "__main__":
    main()

import inspect
import io
import logging
import re
from collections.abc import Generator

import pytest
from helpers import squeezed

from mattermost_agent import logs

SECRET = "sentinel-secret-value"
TIMESTAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"


@pytest.fixture
def output() -> Generator[io.StringIO]:
    """Configure logging like production, then put the loggers back for the other tests."""
    loggers = [logging.getLogger(name) for name in (logs.LOGGER_NAME, *logs.LIBRARY_LOGGERS)]
    saved = [(logger, logger.handlers[:], logger.level, logger.propagate) for logger in loggers]
    stream = io.StringIO()
    logs.configure_logging("DEBUG", stream)
    yield stream
    for logger, handlers, level, propagate in saved:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_post_line_has_exactly_the_allowed_fields(output: io.StringIO) -> None:
    logs.log_post(
        "post",
        kind="mention",
        channel="analytics",
        sender="example.user",
        length=42,
        rule="mentions-me",
        scenario="intake",
    )
    assert re.fullmatch(
        TIMESTAMP + r" INFO mattermost_agent event=post kind=mention channel="
        r'"analytics" sender="example\.user" length=42 rule="mentions-me" scenario="intake"\n',
        squeezed(output.getvalue()),
    )


def test_post_columns_line_up(output: io.StringIO) -> None:
    for channel, sender in (("a", "b"), ("a-longer-channel", "a.longer.user")):
        logs.log_post(
            "post", kind="dm", channel=channel, sender=sender, length=1, rule=None, scenario=None
        )
    first, second = output.getvalue().splitlines()
    for column in ("kind=", "channel=", "sender=", "length=", "rule=", "scenario="):
        assert first.index(column) == second.index(column)


def test_lifecycle_columns_line_up(output: io.StringIO) -> None:
    logs.log_lifecycle("startup")
    logs.log_lifecycle("catchup_truncated", reason="unreachable", status=503, count=20)
    first, second = output.getvalue().splitlines()
    for column in ("reason=", "error=", "status=", "count=", "delay_s="):
        assert first.index(column) == second.index(column)


def test_a_value_wider_than_its_column_is_kept_whole(output: io.StringIO) -> None:
    channel = "x" * 64
    logs.log_post(
        "post", kind="dm", channel=channel, sender=None, length=1, rule=None, scenario=None
    )
    assert f'channel="{channel}" sender=' in output.getvalue()


def test_post_without_a_rule_shows_null(output: io.StringIO) -> None:
    logs.log_post("post", kind="dm", channel=None, sender=None, length=0, rule=None, scenario=None)
    assert "channel=null sender=null length=0 rule=null scenario=null\n" in squeezed(
        output.getvalue()
    )


def test_text_null_stays_distinct_from_no_value(output: io.StringIO) -> None:
    logs.log_post(
        "post", kind="dm", channel="null", sender=None, length=0, rule=None, scenario=None
    )
    assert 'channel="null" sender=null' in squeezed(output.getvalue())


def test_text_cannot_add_a_log_line(output: io.StringIO) -> None:
    forged = 'x"\n2026-01-01 00:00:00,000 INFO mattermost_agent event=forged'
    logs.log_post(
        "post", kind="dm", channel=forged, sender=None, length=1, rule=None, scenario=None
    )
    assert output.getvalue().count("\n") == 1


def test_long_text_is_cut(output: io.StringIO) -> None:
    logs.log_post(
        "post", kind="dm", channel="x" * 5000, sender=None, length=1, rule=None, scenario=None
    )
    assert "x" * logs.MAX_LOGGED_TEXT in output.getvalue()
    assert "x" * (logs.MAX_LOGGED_TEXT + 1) not in output.getvalue()


@pytest.mark.parametrize("function", [logs.log_post, logs.log_lifecycle])
def test_no_parameter_can_carry_text_or_secrets(function: object) -> None:
    assert callable(function)
    allowed = {
        "event",
        "kind",
        "channel",
        "sender",
        "length",
        "rule",
        "scenario",
        "reason",
        "error",
        "status",
        "count",
        "delay_seconds",
    }
    assert set(inspect.signature(function).parameters) <= allowed


def test_lifecycle_line(output: io.StringIO) -> None:
    logs.log_lifecycle("reconnect_wait", reason="unreachable", status=503, count=2, delay_seconds=4)
    assert (
        "event=reconnect_wait reason=unreachable error=null status=503 count=2 delay_s=4.0\n"
        in squeezed(output.getvalue())
    )


def test_lifecycle_without_details_shows_null(output: io.StringIO) -> None:
    logs.log_lifecycle("startup")
    assert "event=startup reason=null error=null status=null count=null delay_s=null\n" in (
        squeezed(output.getvalue())
    )


def test_lifecycle_logs_only_the_class_of_an_error(output: io.StringIO) -> None:
    logs.log_lifecycle("disconnected", error=ValueError(SECRET))
    assert "error=ValueError" in output.getvalue()
    assert SECRET not in output.getvalue()


def test_app_debug_lines_are_printed_at_debug(output: io.StringIO) -> None:
    logs.logger.debug("detail")
    assert "DEBUG mattermost_agent detail" in output.getvalue()


@pytest.mark.parametrize("library", ["websockets.client", "httpx2", "httpcore2.http11"])
def test_debug_level_does_not_reach_the_libraries(output: io.StringIO, library: str) -> None:
    logger = logging.getLogger(library)
    logger.debug("> Authorization: Bearer %s", SECRET)
    logger.info("> Authorization: Bearer %s", SECRET)
    assert SECRET not in output.getvalue()


def test_a_traceback_stays_on_one_line(output: io.StringIO) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logs.logger.exception("failed")
    assert output.getvalue().count("\n") == 1
    assert "\\nRuntimeError: boom" in output.getvalue()

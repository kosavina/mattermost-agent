"""The settings, read from environment variables and checked once at startup.

Environment variables:

- MM_URL: the address of the Mattermost server. Must be https.
- MM_TOKEN_FILE: the file holding the personal access token. The container uses this one.
- MM_TOKEN: the token itself, for running without a container. Set one of the two, not both.
- RULES_FILE: the rules file. Default /config/rules.yaml.
- STATE_DIR: the folder of the state database and the heartbeat. Default /data.
- LOG_LEVEL: DEBUG, INFO, WARNING or ERROR. Default INFO.
- ALLOW_SEND: the kill switch for direct sending. Only `1` lets the listener post a reply of a
  `send` rule; with anything else, or nothing, every such reply becomes a draft.

Error messages name the variable but never quote its value: a mistyped MM_URL may hold a
password, and a mistyped token is still most of a token.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RULES_FILE = "/config/rules.yaml"
DEFAULT_STATE_DIR = "/data"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
MAX_PORT = 65535

# Allowed characters only, so that a user name, query or fragment cannot get through.
HTTPS_URL = re.compile(r"https://[a-zA-Z0-9.-]+(?::(?P<port>[0-9]{1,5}))?(?:/[a-zA-Z0-9._~/-]*)?")
# The token goes into a request header, where a line break or space could do damage.
SAFE_TOKEN = re.compile(r"[a-zA-Z0-9_-]{16,256}")


class SettingsError(Exception):
    """A setting is missing or invalid. The message says which one, without its value."""


@dataclass(frozen=True)
class Settings:
    """The checked settings. repr() leaves out the token."""

    mm_url: str
    token: str = field(repr=False)
    rules_file: Path
    state_dir: Path
    log_level: str
    allow_send: bool = False


def _check_url(url: str | None) -> str:
    if not url:
        raise SettingsError("set MM_URL")
    match = HTTPS_URL.fullmatch(url)
    if match is None or int(match["port"] or 1) not in range(1, MAX_PORT + 1):
        raise SettingsError(
            "MM_URL must be an https:// address, with no user name, password, query or fragment"
        )
    return url.rstrip("/")


def _read_token_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="ascii")
    except UnicodeDecodeError:
        raise SettingsError(f"MM_TOKEN_FILE {path} does not hold a valid token") from None
    except OSError as error:
        raise SettingsError(f"cannot read MM_TOKEN_FILE {path}: {error.strerror}") from None


def _load_token(env: Mapping[str, str]) -> str:
    token_file = env.get("MM_TOKEN_FILE")
    inline = env.get("MM_TOKEN")
    if token_file and inline:
        raise SettingsError("set only one of MM_TOKEN_FILE and MM_TOKEN")
    if token_file:
        source, raw = "MM_TOKEN_FILE", _read_token_file(token_file)
    elif inline:
        source, raw = "MM_TOKEN", inline
    else:
        raise SettingsError("set MM_TOKEN_FILE or MM_TOKEN")
    token = raw.strip()
    if SAFE_TOKEN.fullmatch(token) is None:
        raise SettingsError(f"{source} does not hold a valid token")
    return token


def _check_log_level(level: str | None) -> str:
    normalised = (level or "INFO").upper()
    if normalised not in LOG_LEVELS:
        raise SettingsError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    return normalised


def load_settings(env: Mapping[str, str]) -> Settings:
    """Return the checked settings, or raise SettingsError so that startup stops."""
    return Settings(
        mm_url=_check_url(env.get("MM_URL")),
        token=_load_token(env),
        rules_file=Path(env.get("RULES_FILE") or DEFAULT_RULES_FILE),
        state_dir=Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR),
        log_level=_check_log_level(env.get("LOG_LEVEL")),
        allow_send=env.get("ALLOW_SEND") == "1",
    )

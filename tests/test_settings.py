from pathlib import Path

import pytest

from mattermost_agent.settings import SettingsError, load_settings

URL = "https://chat.example.com"
TOKEN = "abcdefghijklmnopqrstuvwxyz"


def env(**values: str) -> dict[str, str]:
    return {"MM_URL": URL, "MM_TOKEN": TOKEN, **values}


def test_defaults() -> None:
    settings = load_settings(env())
    assert settings.mm_url == URL
    assert settings.token == TOKEN
    assert settings.rules_file == Path("/config/rules.yaml")
    assert settings.state_dir == Path("/data")
    assert settings.log_level == "INFO"


def test_values_are_read() -> None:
    settings = load_settings(env(RULES_FILE="/r.yaml", STATE_DIR="/s", LOG_LEVEL="debug"))
    assert settings.rules_file == Path("/r.yaml")
    assert settings.state_dir == Path("/s")
    assert settings.log_level == "DEBUG"


def test_empty_counts_as_unset() -> None:
    settings = load_settings(env(RULES_FILE="", STATE_DIR="", LOG_LEVEL="", MM_TOKEN_FILE=""))
    assert settings.rules_file == Path("/config/rules.yaml")
    assert settings.log_level == "INFO"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://chat.example.com/", "https://chat.example.com"),
        ("https://chat.example.com:8443", "https://chat.example.com:8443"),
        ("https://example.com/mattermost/", "https://example.com/mattermost"),
    ],
)
def test_accepted_urls(given: str, expected: str) -> None:
    assert load_settings(env(MM_URL=given)).mm_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://chat.example.com",
        "ws://chat.example.com",
        "wss://chat.example.com",
        "chat.example.com",
        "https://",
        "https://secret-user:secret-pass@chat.example.com",
        "https://chat.example.com?secret-query=1",
        "https://chat.example.com#secret-fragment",
        "https://chat.example.com/a b",
        "https://chat.example.com\n.evil.com",
        "https://chat.example.com:0",
        "https://chat.example.com:70000",
    ],
)
def test_refused_urls_are_not_quoted(url: str) -> None:
    with pytest.raises(SettingsError) as error:
        load_settings(env(MM_URL=url))
    assert "MM_URL" in str(error.value)
    assert "secret" not in str(error.value)


def test_url_is_required() -> None:
    with pytest.raises(SettingsError, match="set MM_URL"):
        load_settings({"MM_TOKEN": TOKEN})


def test_token_from_file_loses_its_line_break(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(f"{TOKEN}\n")
    settings = load_settings({"MM_URL": URL, "MM_TOKEN_FILE": str(path)})
    assert settings.token == TOKEN


def test_token_sources_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="only one"):
        load_settings(env(MM_TOKEN_FILE=str(tmp_path / "token")))


def test_a_token_is_required() -> None:
    with pytest.raises(SettingsError, match="set MM_TOKEN_FILE or MM_TOKEN"):
        load_settings({"MM_URL": URL})


def test_missing_token_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "missing"
    with pytest.raises(SettingsError, match="cannot read MM_TOKEN_FILE"):
        load_settings({"MM_URL": URL, "MM_TOKEN_FILE": str(path)})


def test_binary_token_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_bytes(b"\xff\xfe-not-text")
    with pytest.raises(SettingsError, match="does not hold a valid token") as error:
        load_settings({"MM_URL": URL, "MM_TOKEN_FILE": str(path)})
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "secret token with spaces",
        "secretsecretsecret\nX-Evil: 1",
        "секретсекретсекретсекрет",
    ],
)
def test_invalid_tokens_are_not_quoted(token: str) -> None:
    with pytest.raises(SettingsError, match="MM_TOKEN does not hold a valid token") as error:
        load_settings(env(MM_TOKEN=token))
    assert token not in str(error.value)


def test_invalid_log_level_is_refused() -> None:
    with pytest.raises(SettingsError, match="LOG_LEVEL must be one of"):
        load_settings(env(LOG_LEVEL="LOUD"))


def test_repr_leaves_out_the_token() -> None:
    assert TOKEN not in repr(load_settings(env()))

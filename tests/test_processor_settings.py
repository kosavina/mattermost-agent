from pathlib import Path

import pytest

from mattermost_agent.agents import ClaudeBackend, CodexBackend
from mattermost_agent.processor.settings import Roots, inside, load_processor_settings
from mattermost_agent.settings import SettingsError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    agent = tmp_path / "agent"
    (agent / "scenarios").mkdir(parents=True)
    (agent / "state").mkdir()
    return agent


def env(root: Path, **values: str) -> dict[str, str]:
    base = {
        "STATE_DIR": str(root / "state"),
        "AGENT_ROOT": str(root),
        "RULES_FILE": str(root / "rules.yaml"),
        "HOME": "/Users/example",
        "USER": "example",
    }
    base.update(values)
    return {name: value for name, value in base.items() if value != ""}


def test_defaults(root: Path) -> None:
    settings = load_processor_settings(env(root))
    assert settings.state_dir == (root / "state").resolve()
    assert settings.agent_root == root.resolve()
    assert settings.roots == Roots(
        (root / "scenarios").resolve(),
        (root / "projects").resolve(),
        (root / "conversations").resolve(),
    )
    assert settings.scenarios_file == (root / "scenarios" / "scenarios.yaml").resolve()
    assert settings.backend == "codex"
    assert settings.codex_bin == Path("/opt/homebrew/bin/codex")
    assert settings.codex_home == root.resolve() / "codex-home"
    assert settings.codex_install_dir == Path("/opt/homebrew/Caskroom/codex")
    assert settings.claude_bin == Path("/opt/homebrew/bin/claude")
    assert (settings.timeout_s, settings.poll_s, settings.log_level) == (120.0, 3.0, "INFO")
    assert (settings.home, settings.user, settings.proxies) == ("/Users/example", "example", {})


def test_missing_folders_are_created_private(root: Path) -> None:
    load_processor_settings(env(root))
    for name in ("projects", "conversations"):
        assert (root / name).is_dir()
        assert ((root / name).stat().st_mode & 0o777) == 0o700


def test_values_are_read(root: Path) -> None:
    settings = load_processor_settings(
        env(
            root,
            AGENT_BACKEND="claude",
            CODEX_BIN="/x/codex",
            CODEX_HOME=str(root / "ch"),
            CODEX_INSTALL_DIR="/x/cask",
            CLAUDE_BIN="/x/claude",
            AGENT_TIMEOUT_SECONDS="45.5",
            POLL_SECONDS="1",
            LOG_LEVEL="debug",
            HTTPS_PROXY="http://p:1",
            NO_PROXY="localhost",
        )
    )
    assert settings.backend == "claude"
    assert (settings.codex_bin, settings.claude_bin) == (Path("/x/codex"), Path("/x/claude"))
    assert settings.codex_home == (root / "ch").resolve()
    assert settings.codex_install_dir == Path("/x/cask")
    assert (settings.timeout_s, settings.poll_s, settings.log_level) == (45.5, 1.0, "DEBUG")
    assert settings.proxies == {"HTTPS_PROXY": "http://p:1", "NO_PROXY": "localhost"}


def test_backends_are_built_from_the_settings(root: Path) -> None:
    backends = load_processor_settings(env(root, HTTPS_PROXY="http://p:1")).backends()
    assert set(backends) == {"codex", "claude"}
    codex, claude = backends["codex"], backends["claude"]
    assert isinstance(codex, CodexBackend) and isinstance(claude, ClaudeBackend)
    assert codex.codex_home == root.resolve() / "codex-home"
    assert codex.state_dir == claude.state_dir == (root / "state").resolve()
    assert codex.proxies == claude.proxies == {"HTTPS_PROXY": "http://p:1"}


@pytest.mark.parametrize("name", ["STATE_DIR", "AGENT_ROOT", "RULES_FILE", "HOME", "USER"])
def test_required_variables(root: Path, name: str) -> None:
    with pytest.raises(SettingsError, match=f"set {name}"):
        load_processor_settings(env(root, **{name: ""}))


@pytest.mark.parametrize("name", ["MM_TOKEN", "MM_TOKEN_FILE", "MM_URL", "MM_ANYTHING"])
def test_listener_variables_are_refused(root: Path, name: str) -> None:
    with pytest.raises(SettingsError, match="MM_") as info:
        load_processor_settings(env(root, **{name: "secret-value-xyz"}))
    assert "secret-value-xyz" not in str(info.value)


@pytest.mark.parametrize("name", ["STATE_DIR", "AGENT_ROOT", "CODEX_HOME"])
def test_shared_volume_is_refused(root: Path, name: str) -> None:
    with pytest.raises(SettingsError, match=f"{name} is on a shared volume"):
        load_processor_settings(env(root, **{name: "/Volumes/My Shared Files/agent"}))


def test_unknown_backend_is_refused(root: Path) -> None:
    with pytest.raises(SettingsError, match="AGENT_BACKEND must be one of claude, codex"):
        load_processor_settings(env(root, AGENT_BACKEND="gpt"))


@pytest.mark.parametrize("name", ["AGENT_TIMEOUT_SECONDS", "POLL_SECONDS"])
@pytest.mark.parametrize("value", ["0", "-1", "abc", "1e3", ""])
def test_numbers_must_be_positive(root: Path, name: str, value: str) -> None:
    if value == "":
        return
    with pytest.raises(SettingsError, match=f"{name} must be a positive number"):
        load_processor_settings(env(root, **{name: value}))


def test_agent_root_must_hold_scenarios(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(SettingsError, match="no scenarios/ folder"):
        load_processor_settings(env(bare))
    with pytest.raises(SettingsError, match="AGENT_ROOT is not a folder"):
        load_processor_settings(env(tmp_path / "missing"))


def test_a_root_that_is_a_symlink_out_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "projects").symlink_to(elsewhere)
    with pytest.raises(SettingsError, match="points outside"):
        load_processor_settings(env(root))


def test_overlapping_roots_are_refused(root: Path) -> None:
    (root / "projects" / "foo" / "conv").mkdir(parents=True)
    (root / "conversations").symlink_to(root / "projects" / "foo" / "conv")
    with pytest.raises(SettingsError, match="overlap"):
        load_processor_settings(env(root))


def test_a_dangling_root_symlink_is_refused_without_a_path(root: Path) -> None:
    (root / "projects").symlink_to(root / "nonexistent")
    with pytest.raises(SettingsError, match="cannot create or open") as info:
        load_processor_settings(env(root))
    assert str(root) not in str(info.value)


def test_state_dir_inside_a_read_root_is_refused(root: Path) -> None:
    with pytest.raises(SettingsError, match="STATE_DIR is inside a folder the agents may read"):
        load_processor_settings(env(root, STATE_DIR=str(root / "scenarios" / "state")))


def test_codex_home_inside_a_read_root_is_refused(root: Path) -> None:
    with pytest.raises(SettingsError, match="CODEX_HOME is inside a folder the agents may read"):
        load_processor_settings(env(root, CODEX_HOME=str(root / "projects" / "codex")))


def test_errors_never_quote_values(root: Path) -> None:
    for values in (
        {"AGENT_BACKEND": "gpt-secret"},
        {"POLL_SECONDS": "secret"},
        {"STATE_DIR": "/Volumes/secret-volume"},
    ):
        with pytest.raises(SettingsError) as info:
            load_processor_settings(env(root, **values))
        assert "secret" not in str(info.value)


def test_inside() -> None:
    assert inside(Path("/a/b"), Path("/a"))
    assert inside(Path("/a"), Path("/a"))
    assert not inside(Path("/ab"), Path("/a"))
    assert not inside(Path("/a"), Path("/a/b"))

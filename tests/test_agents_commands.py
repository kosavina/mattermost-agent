"""Golden tests: for a fixed task, each backend builds exactly the argv and env we verified."""

import json
from pathlib import Path

import pytest

from mattermost_agent.agents import BACKENDS, AgentTask, ClaudeBackend, CodexBackend
from mattermost_agent.agents.codex import toml_string
from mattermost_agent.agents.schema import OUTPUT_SCHEMA

ROOT = Path("/Users/example/agent")
STATE = ROOT / "state"
RUN_DIR = STATE / "tmp" / "run-abc"
SCENARIO = ROOT / "scenarios" / "intake"
CONVERSATION = ROOT / "conversations" / "k3d7w1q9m2n4p6r8t0v2x4z6a8"
PROJECT = ROOT / "projects" / "sales-reports"
PROXIES = {"HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "localhost"}

TASK = AgentTask(
    scenario_dir=SCENARIO,
    read_dirs=(CONVERSATION, PROJECT),
    instructions="# Intake\nReply in English.",
    prompt="<<<DATA-00\nhello\nDATA-00>>>\n",
    timeout_s=90.0,
)

CODEX = CodexBackend(
    binary=Path("/opt/homebrew/bin/codex"),
    codex_home=ROOT / "codex-home",
    install_dir=Path("/opt/homebrew/Caskroom/codex"),
    home="/Users/example",
    user="example",
    proxies=PROXIES,
    state_dir=STATE,
)
CLAUDE = ClaudeBackend(
    binary=Path("/opt/homebrew/bin/claude"),
    home="/Users/example",
    user="example",
    proxies=PROXIES,
    state_dir=STATE,
)

EXPECTED_PROFILE = (
    '{":minimal"="read",":tmpdir"="deny",":slash_tmp"="deny",'
    '"/opt/homebrew/Caskroom/codex"="read","/opt/homebrew/bin"="read",'
    f'"{CONVERSATION}"="read","{PROJECT}"="read",'
    '":workspace_roots"={"."="read"}}'
)


def test_codex_command_is_the_verified_one() -> None:
    argv, env = CODEX.command(TASK, RUN_DIR)
    assert argv == [
        "/opt/homebrew/bin/codex",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--ignore-user-config",
        "-c",
        'default_permissions="scenario"',
        "-c",
        f"permissions.scenario.filesystem={EXPECTED_PROFILE}",
        "-c",
        "permissions.scenario.network.enabled=false",
        "--output-schema",
        str(RUN_DIR / "schema.json"),
        "-C",
        str(SCENARIO),
        "-o",
        str(RUN_DIR / "out.json"),
        "-",
    ]
    assert env == {
        "HOME": "/Users/example",
        "USER": "example",
        "PATH": "/usr/bin:/bin",
        "CODEX_HOME": str(ROOT / "codex-home"),
        **PROXIES,
    }


def test_codex_command_never_uses_the_sandbox_flag() -> None:
    argv, _ = CODEX.command(TASK, RUN_DIR)
    assert "--sandbox" not in argv
    assert "-s" not in argv
    assert not any(item.startswith("--sandbox") for item in argv)


def test_codex_profile_lists_each_read_dir_once_and_denies_temp() -> None:
    task = AgentTask(SCENARIO, (PROJECT, PROJECT), "i", "p")
    profile = CODEX.filesystem_profile(task)
    assert profile.count(f'"{PROJECT}"="read"') == 1
    assert '":tmpdir"="deny"' in profile
    assert '":slash_tmp"="deny"' in profile
    assert '"write"' not in profile
    assert "network" not in profile


def test_codex_profile_quotes_a_hostile_path() -> None:
    hostile = Path('/Users/example/agent/projects/a"b\\c')
    profile = CODEX.filesystem_profile(AgentTask(SCENARIO, (hostile,), "i", "p"))
    assert '"/Users/example/agent/projects/a\\"b\\\\c"="read"' in profile


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [("plain", '"plain"'), ('a"b', '"a\\"b"'), ("a\\b", '"a\\\\b"'), ("a\nb", '"a\\u000Ab"')],
)
def test_toml_string(raw: str, quoted: str) -> None:
    assert toml_string(raw) == quoted


def test_claude_command_is_the_verified_one() -> None:
    argv, env = CLAUDE.command(TASK, RUN_DIR)
    assert argv == [
        "/opt/homebrew/bin/claude",
        "-p",
        "--tools",
        "Read,Glob,Grep",
        "--restricted",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--add-dir",
        str(CONVERSATION),
        "--add-dir",
        str(PROJECT),
        "--append-system-prompt-file",
        str(SCENARIO / "AGENTS.md"),
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(OUTPUT_SCHEMA),
    ]
    assert env == {"HOME": "/Users/example", "USER": "example", "PATH": "/usr/bin:/bin", **PROXIES}


def test_claude_command_has_one_add_dir_per_read_dir() -> None:
    argv, _ = CLAUDE.command(
        AgentTask(SCENARIO, (PROJECT, PROJECT, CONVERSATION), "i", "p"), RUN_DIR
    )
    assert argv.count("--add-dir") == 2
    argv, _ = CLAUDE.command(AgentTask(SCENARIO, (), "i", "p"), RUN_DIR)
    assert "--add-dir" not in argv


def test_no_text_is_ever_an_argument() -> None:
    """argv is visible to every local user: the prompt goes on stdin, instructions by file."""
    for backend in (CODEX, CLAUDE):
        argv, _ = backend.command(TASK, RUN_DIR)
        assert all("hello" not in item and "Intake" not in item for item in argv)


def test_env_has_exactly_the_allowed_keys() -> None:
    _, codex_env = CODEX.command(TASK, RUN_DIR)
    _, claude_env = CLAUDE.command(TASK, RUN_DIR)
    assert set(codex_env) == {"HOME", "USER", "PATH", "CODEX_HOME", "HTTPS_PROXY", "NO_PROXY"}
    assert set(claude_env) == {"HOME", "USER", "PATH", "HTTPS_PROXY", "NO_PROXY"}


def test_registry_names_match_the_classes() -> None:
    assert {"codex": CodexBackend, "claude": ClaudeBackend} == BACKENDS
    assert CODEX.name == "codex"
    assert CLAUDE.name == "claude"

from pathlib import Path

import pytest

from mattermost_agent.processor.folders import JobSetupError, Resolved, resolve
from mattermost_agent.processor.scenarios import Scenario
from mattermost_agent.processor.settings import Roots
from mattermost_agent.rules import Bursts, Rule, RuleSet

CHANNEL = "k3d7w1q9m2n4p6r8t0v2x4z6a8"
SCENARIOS = {
    "intake": Scenario("intake", "intake", None),
    "review": Scenario("review", "review", "claude"),
}


def rule(name: str = "dm", scenario: str = "intake", read: tuple[str, ...] = ()) -> Rule:
    return Rule(name, scenario, True, frozenset(), frozenset(), read)


def rules(*items: Rule) -> RuleSet:
    return RuleSet(items or (rule(),), 20, Bursts(90, 600, 20))


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    for name in ("scenarios", "projects", "conversations"):
        (tmp_path / name).mkdir()
    for scenario in ("intake", "review"):
        (tmp_path / "scenarios" / scenario).mkdir()
        (tmp_path / "scenarios" / scenario / "AGENTS.md").write_text(f"# {scenario}\n")
    (tmp_path / "projects" / "sales").mkdir()
    return Roots(
        *((tmp_path / name).resolve() for name in ("scenarios", "projects", "conversations"))
    )


def run(roots: Roots, **overrides: object) -> Resolved:
    values: dict[str, object] = {
        "scenario": "intake",
        "rule": "dm",
        "channel_id": CHANNEL,
        "rules": rules(),
        "scenarios": SCENARIOS,
        "roots": roots,
        "default_backend": "codex",
    }
    values.update(overrides)
    return resolve(**values)  # type: ignore[arg-type]


def test_the_plain_case(roots: Roots) -> None:
    resolved = run(roots)
    assert resolved.scenario_dir == roots.scenarios / "intake"
    assert resolved.conversation_dir == roots.conversations / CHANNEL
    assert resolved.read_dirs == (roots.conversations / CHANNEL,)
    assert resolved.backend == "codex"
    assert resolved.instructions == "# intake\n"
    assert (resolved.conversation_dir.stat().st_mode & 0o777) == 0o700
    assert "intake\\n" not in repr(resolved)


def test_projects_are_attached_after_the_conversation(roots: Roots) -> None:
    (roots.projects / "docs").mkdir()
    resolved = run(roots, rules=rules(rule(read=("sales", "docs"))))
    assert resolved.read_dirs == (
        roots.conversations / CHANNEL,
        roots.projects / "sales",
        roots.projects / "docs",
    )


def test_the_scenario_backend_wins_over_the_default(roots: Roots) -> None:
    resolved = run(roots, scenario="review", rules=rules(rule(scenario="review")))
    assert resolved.backend == "claude"


def test_an_existing_conversation_folder_is_reused(roots: Roots) -> None:
    folder = roots.conversations / CHANNEL
    folder.mkdir(mode=0o700)
    (folder / "memory.md").write_text("known")
    assert run(roots).conversation_dir == folder
    assert (folder / "memory.md").read_text() == "known"


def reason(roots: Roots, **overrides: object) -> str:
    with pytest.raises(JobSetupError) as info:
        run(roots, **overrides)
    assert str(info.value) == info.value.reason
    return info.value.reason


def test_unknown_scenario_or_rule(roots: Roots) -> None:
    assert reason(roots, scenario="other") == "unknown_scenario"
    assert reason(roots, rule="other") == "unknown_rule"
    # The rule exists but points at another scenario than the job says: the file changed.
    assert reason(roots, rules=rules(rule(scenario="review"))) == "unknown_rule"


@pytest.mark.parametrize(
    "channel_id",
    [
        "",
        "..",
        "../x",
        "/etc",
        "K3D7W1Q9M2N4P6R8T0V2X4Z6A8",
        "k3d7w1q9m2n4p6r8t0v2x4z6a",
        CHANNEL + "x",
        "a" * 25 + "/",
    ],
)
def test_a_bad_channel_id_never_becomes_a_folder(roots: Roots, channel_id: str) -> None:
    assert reason(roots, channel_id=channel_id) == "bad_folder"
    assert list(roots.conversations.iterdir()) == []


def test_a_conversation_folder_that_is_a_symlink_is_refused(roots: Roots, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (roots.conversations / CHANNEL).symlink_to(outside)
    assert reason(roots) == "bad_folder"


@pytest.mark.parametrize("name", ["missing", "sales/.."])
def test_a_project_that_does_not_exist(roots: Roots, name: str) -> None:
    assert reason(roots, rules=rules(rule(read=(name,)))) == "bad_folder"


def test_a_project_symlink_out_of_projects_is_refused(roots: Roots, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (roots.projects / "linked").symlink_to(outside)
    assert reason(roots, rules=rules(rule(read=("linked",)))) == "bad_folder"


def test_a_project_symlink_into_conversations_or_scenarios_is_refused(roots: Roots) -> None:
    (roots.projects / "conv").symlink_to(roots.conversations)
    (roots.projects / "scen").symlink_to(roots.scenarios / "intake")
    assert reason(roots, rules=rules(rule(read=("conv",)))) == "bad_folder"
    assert reason(roots, rules=rules(rule(read=("scen",)))) == "bad_folder"


def test_a_project_that_is_a_file_is_refused(roots: Roots) -> None:
    (roots.projects / "flat").write_text("x")
    assert reason(roots, rules=rules(rule(read=("flat",)))) == "bad_folder"


def test_a_bad_scenario_folder_fails_the_job(roots: Roots) -> None:
    (roots.scenarios / "intake" / "CLAUDE.md").write_text("x")
    assert reason(roots) == "bad_folder"
    (roots.scenarios / "intake" / "CLAUDE.md").unlink()
    (roots.scenarios / "intake" / "AGENTS.md").unlink()
    assert reason(roots) == "bad_folder"

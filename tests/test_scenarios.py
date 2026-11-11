from pathlib import Path

import pytest

from mattermost_agent.processor.scenarios import (
    Scenario,
    ScenariosError,
    load_scenarios,
    parse_scenarios,
    scenario_dir,
)
from mattermost_agent.processor.settings import Roots

GOOD = """
scenarios:
  intake:
    folder: intake
  review:
    folder: review-v2
    backend: claude
"""


def test_scenarios_are_parsed() -> None:
    assert parse_scenarios(GOOD) == {
        "intake": Scenario("intake", "intake", None),
        "review": Scenario("review", "review-v2", "claude"),
    }


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "the file must be a mapping"),
        ("scenarios: []", "scenarios must be a mapping"),
        ("scenarios: {}", "scenarios must be a mapping"),
        ("scenarios: {intake: {}}", "scenario intake folder must be text"),
        ("scenarios: {intake: {folder: intake, extra: 1}}", "scenario intake has an unknown key"),
        ("scenarios: {intake: {folder: ../x}}", "scenario intake folder must be a single folder"),
        ("scenarios: {intake: {folder: /abs}}", "scenario intake folder must be a single folder"),
        (
            "scenarios: {intake: {folder: .hidden}}",
            "scenario intake folder must be a single folder",
        ),
        ("scenarios: {intake: {folder: Intake}}", "scenario intake folder must be a single folder"),
        ("scenarios: {intake: {folder: intake, backend: gpt}}", "backend must be one of"),
        ("scenarios: {In take: {folder: intake}}", "a scenario name must be"),
        ("scenarios: {1: {folder: intake}}", "a scenario name must be"),
        ("scenarios: {intake: {folder: intake}, intake: {folder: b}}", "duplicate key"),
        ("other: 1", "the file has an unknown key"),
        ("scenarios: [", "not valid YAML"),
    ],
)
def test_bad_files_are_refused(text: str, message: str) -> None:
    with pytest.raises(ScenariosError, match=message):
        parse_scenarios(text)


def test_file_errors_name_the_file(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.yaml"
    with pytest.raises(ScenariosError, match=f"cannot read the scenarios file {path}"):
        load_scenarios(path)
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ScenariosError, match="is not UTF-8 text"):
        load_scenarios(path)
    path.write_text("scenarios: {intake: {folder: intake}}")
    assert load_scenarios(path) == {"intake": Scenario("intake", "intake", None)}


@pytest.fixture
def roots(tmp_path: Path) -> Roots:
    for name in ("scenarios", "projects", "conversations"):
        (tmp_path / name).mkdir()
    (tmp_path / "scenarios" / "intake").mkdir()
    (tmp_path / "scenarios" / "intake" / "AGENTS.md").write_text("# Intake\n")
    return Roots(
        *((tmp_path / name).resolve() for name in ("scenarios", "projects", "conversations"))
    )


INTAKE = Scenario("intake", "intake", None)


def test_a_good_scenario_folder(roots: Roots) -> None:
    assert scenario_dir(roots, INTAKE) == roots.scenarios / "intake"


def test_missing_folder_or_instructions(roots: Roots) -> None:
    with pytest.raises(ScenariosError, match="scenario other: its folder does not exist"):
        scenario_dir(roots, Scenario("other", "other", None))
    (roots.scenarios / "intake" / "AGENTS.md").unlink()
    with pytest.raises(ScenariosError, match=r"no AGENTS\.md"):
        scenario_dir(roots, INTAKE)


@pytest.mark.parametrize("name", ["CLAUDE.md", "CLAUDE.local.md", ".claude"])
def test_claude_instruction_files_are_refused(roots: Roots, name: str) -> None:
    path = roots.scenarios / "intake" / name
    if name == ".claude":
        path.mkdir()
    else:
        path.write_text("x")
    with pytest.raises(ScenariosError, match=f"holds {name}"):
        scenario_dir(roots, INTAKE)


def test_a_dangling_claude_symlink_is_refused(roots: Roots) -> None:
    (roots.scenarios / "intake" / "CLAUDE.md").symlink_to("/nonexistent/target")
    with pytest.raises(ScenariosError, match=r"holds CLAUDE\.md"):
        scenario_dir(roots, INTAKE)


def test_a_folder_that_is_a_symlink_out_is_refused(roots: Roots, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("x")
    (roots.scenarios / "linked").symlink_to(outside)
    with pytest.raises(ScenariosError, match="not inside scenarios/"):
        scenario_dir(roots, Scenario("linked", "linked", None))


def test_a_file_instead_of_a_folder_is_refused(roots: Roots) -> None:
    (roots.scenarios / "flat").write_text("x")
    with pytest.raises(ScenariosError, match="not inside scenarios/"):
        scenario_dir(roots, Scenario("flat", "flat", None))

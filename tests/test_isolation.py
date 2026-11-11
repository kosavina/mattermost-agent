"""Who may import what: the processor never holds the token, and one module writes to Mattermost."""

import re
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).parent.parent / "src" / "mattermost_agent"
PROCESSOR_MODULES = (
    "mattermost_agent.processor.loop",
    "mattermost_agent.processor.cli",
    "mattermost_agent.processor.graph",
    "mattermost_agent.agents",
    "mattermost_agent.agents.demo",
    "mattermost_agent.replies",
)
# The reading client, the writer, and the two modules that drive them.
TOKEN_MODULES = (
    "mattermost_agent.mattermost",
    "mattermost_agent.mattermost_write",
    "mattermost_agent.deliver",
    "mattermost_agent.app",
)
IMPORT = re.compile(r"^\s*(?:from|import)\s+([\w.]+)", re.MULTILINE)


def test_processor_modules_never_import_a_mattermost_client() -> None:
    script = (
        "import sys, importlib\n"
        + "".join(f"importlib.import_module({name!r})\n" for name in PROCESSOR_MODULES)
        + "loaded = [m for m in sys.modules if m.startswith('mattermost_agent')]\n"
        + "".join(f"assert {name!r} not in sys.modules, loaded\n" for name in TOKEN_MODULES)
    )
    result = subprocess.run(  # noqa: S603 - our interpreter, our script
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def importers_of(module: str) -> set[str]:
    """Return the source files, relative to the package, with an import statement of `module`."""
    found = set()
    for path in SOURCE.rglob("*.py"):
        imported = IMPORT.findall(path.read_text(encoding="utf-8"))
        if any(name == module or name.startswith(f"{module}.") for name in imported):
            found.add(str(path.relative_to(SOURCE)))
    return found


def test_only_the_deliver_module_imports_the_writer() -> None:
    assert importers_of("mattermost_agent.mattermost_write") == {"deliver.py"}


def test_only_the_entry_point_imports_the_deliver_module() -> None:
    assert importers_of("mattermost_agent.deliver") == {"__main__.py"}


def test_nothing_in_the_processor_or_the_agents_names_a_client_module() -> None:
    for folder in ("processor", "agents"):
        for path in (SOURCE / folder).rglob("*.py"):
            imported = set(IMPORT.findall(path.read_text(encoding="utf-8")))
            assert not imported & set(TOKEN_MODULES), path.name


def test_the_listener_modules_import_without_the_processors_packages() -> None:
    """The listener's image has no LangGraph or pydantic, and deliver.py runs in it."""
    script = (
        "import sys, importlib\n"
        "importlib.import_module('mattermost_agent.__main__')\n"
        "heavy = [m for m in sys.modules if m.split('.')[0] in ('langgraph', 'pydantic')]\n"
        "assert not heavy, heavy[:5]\n"
    )
    result = subprocess.run(  # noqa: S603 - our interpreter, our script
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr

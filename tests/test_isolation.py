"""The processor never holds the token: its modules do not import the Mattermost client."""

import subprocess
import sys

PROCESSOR_MODULES = (
    "mattermost_agent.processor.loop",
    "mattermost_agent.processor.cli",
    "mattermost_agent.processor.graph",
    "mattermost_agent.agents",
)


def test_processor_modules_never_import_the_mattermost_client() -> None:
    script = (
        "import sys, importlib\n"
        + "".join(f"importlib.import_module({name!r})\n" for name in PROCESSOR_MODULES)
        + "loaded = [m for m in sys.modules if m.startswith('mattermost_agent')]\n"
        "assert 'mattermost_agent.mattermost' not in sys.modules, loaded\n"
        "assert 'mattermost_agent.app' not in sys.modules, loaded\n"
    )
    result = subprocess.run(  # noqa: S603 - our interpreter, our script
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr

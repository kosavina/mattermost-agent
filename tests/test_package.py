from importlib.metadata import version

import mattermost_agent


def test_package_is_installed() -> None:
    assert mattermost_agent.__name__ == "mattermost_agent"
    assert version("mattermost-agent")

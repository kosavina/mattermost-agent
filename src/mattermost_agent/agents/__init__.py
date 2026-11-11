"""The agent backends behind the processor, and the one interface they share.

Adding a backend means one new module and one line in BACKENDS, plus passing the contract tests.
"""

from mattermost_agent.agents.claude import ClaudeBackend
from mattermost_agent.agents.codex import CodexBackend
from mattermost_agent.agents.port import (
    AgentBackend,
    AgentError,
    AgentResult,
    AgentTask,
    BackendCheckError,
    Reason,
)

BACKENDS: dict[str, type[CodexBackend] | type[ClaudeBackend]] = {
    CodexBackend.name: CodexBackend,
    ClaudeBackend.name: ClaudeBackend,
}

__all__ = [
    "BACKENDS",
    "AgentBackend",
    "AgentError",
    "AgentResult",
    "AgentTask",
    "BackendCheckError",
    "ClaudeBackend",
    "CodexBackend",
    "Reason",
]

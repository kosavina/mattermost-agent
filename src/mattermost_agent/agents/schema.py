"""The agent's answer: the JSON schema it must follow, and the checks the schema cannot express.

The schema is fixed in code, not per scenario. It is strict: every key required, null instead of
missing, no extra keys. AgentAnswer then checks that the texts fit the decision and the limits.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from mattermost_agent.agents.port import AgentError

MAX_TEXT = 4_000
DECISIONS = ("ignore", "remember", "act")

type Decision = Literal["ignore", "remember", "act"]

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "memory", "draft"],
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "memory": {"type": ["string", "null"]},
        "draft": {"type": ["string", "null"]},
    },
}


class AgentAnswer(BaseModel):
    """A valid answer. `memory` is the complete new memory of the conversation, not an addition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Decision
    memory: str | None
    draft: str | None

    @model_validator(mode="after")
    def _texts_fit_the_decision(self) -> AgentAnswer:
        if self.decision == "ignore" and (self.memory is not None or self.draft is not None):
            raise ValueError("ignore must have neither memory nor draft")
        if self.decision == "remember" and (not self.memory or self.draft is not None):
            raise ValueError("remember needs memory and no draft")
        if self.decision == "act" and not self.draft:
            raise ValueError("act needs a draft")
        if self.memory is not None and (not self.memory.strip() or len(self.memory) > MAX_TEXT):
            raise ValueError("memory is empty or too long")
        if self.draft is not None and (not self.draft.strip() or len(self.draft) > MAX_TEXT):
            raise ValueError("draft is empty or too long")
        return self


def validate_answer(output: object) -> AgentAnswer:
    """Return the checked answer, or raise AgentError("invalid_output").

    Pydantic's own error quotes the input, which is the agent's text; it never leaves here.
    """
    try:
        return AgentAnswer.model_validate(output)
    except ValidationError:
        raise AgentError("invalid_output") from None

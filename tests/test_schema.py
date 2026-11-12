import pytest

from mattermost_agent.agents import AgentError
from mattermost_agent.agents.schema import MAX_TEXT, OUTPUT_SCHEMA, validate_answer


def answer(
    decision: str,
    memory: str | None = None,
    draft: str | None = None,
    needs_review: object = False,
) -> dict[str, object]:
    return {"decision": decision, "memory": memory, "draft": draft, "needs_review": needs_review}


@pytest.mark.parametrize(
    "output",
    [
        answer("ignore"),
        answer("remember", memory="Ivan owns the sales dashboard."),
        answer("act", draft="Yes, the filter is right."),
        answer("act", memory="Ivan asked about the filter.", draft="Yes."),
        answer("act", draft="x" * MAX_TEXT),
        answer("act", draft="Not sure about this one.", needs_review=True),
        answer("ignore", needs_review=True),
    ],
)
def test_valid_answers(output: dict[str, object]) -> None:
    checked = validate_answer(output)
    assert checked.model_dump() == output


@pytest.mark.parametrize(
    "output",
    [
        answer("ignore", memory="m"),
        answer("ignore", draft="d"),
        answer("remember"),
        answer("remember", memory="m", draft="d"),
        answer("remember", memory="   "),
        answer("act"),
        answer("act", memory="m"),
        answer("act", draft=""),
        answer("act", draft="x" * (MAX_TEXT + 1)),
        answer("act", memory="x" * (MAX_TEXT + 1), draft="d"),
        answer("post", draft="d"),
        {"decision": "ignore", "memory": None},
        {"decision": "ignore", "memory": None, "draft": None},
        answer("act", draft="d", needs_review="false"),
        answer("act", draft="d", needs_review=0),
        answer("act", draft="d", needs_review=None),
        {**answer("ignore"), "extra": 1},
        {**answer("act"), "draft": 7},
        [],
        None,
        "act",
    ],
)
def test_invalid_answers_raise_without_quoting_them(output: object) -> None:
    with pytest.raises(AgentError) as info:
        validate_answer(output)
    assert info.value.reason == "invalid_output"
    assert str(info.value) == "invalid_output"
    assert "x" * 10 not in repr(info.value)


def test_the_schema_is_strict() -> None:
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    required = OUTPUT_SCHEMA["required"]
    assert required == ["decision", "memory", "draft", "needs_review"]
    properties = OUTPUT_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert properties["decision"]["enum"] == ["ignore", "remember", "act"]
    assert properties["memory"]["type"] == ["string", "null"]
    assert properties["needs_review"] == {"type": "boolean"}
    assert isinstance(required, list) and set(properties) == set(required)

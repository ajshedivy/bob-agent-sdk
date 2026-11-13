"""Typed output: `BobAgentOptions.output_format` and `ResultMessage.structured_output`."""

import asyncio
from pathlib import Path
from typing import Literal

import pytest
from conftest import collect
from pydantic import BaseModel, Field

from bob_agent_sdk import (
    BobAgentOptions,
    BobSDKClient,
    BobSDKError,
    ModeDefinition,
    PermissionResultDeny,
    ToolResultMessage,
)


class Review(BaseModel):
    verdict: Literal["approve", "reject"]
    score: int = Field(ge=1, le=10)


SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    },
}


def run(cli: Path, prompt: str, **options) -> list:
    return asyncio.run(collect(prompt, BobAgentOptions(cli_path=cli, **options)))


def test_pydantic_model_gives_a_model_instance(cli: Path) -> None:
    result = run(cli, 'output:{"verdict": "approve", "score": 8}', output_format=Review)[-1]
    assert result.structured_output == Review(verdict="approve", score=8)
    assert (result.stop_reason, result.is_error) == ("end_turn", False)


def test_json_schema_gives_plain_data(cli: Path) -> None:
    result = run(cli, 'output:{"answer": 42}', output_format=SCHEMA)[-1]
    assert result.structured_output == {"answer": 42}


def test_a_schema_that_is_not_an_object(cli: Path) -> None:
    array = {"type": "json_schema", "schema": {"type": "array", "items": {"type": "string"}}}
    result = run(cli, 'output:{"value": ["a", "b"]}', output_format=array)[-1]
    assert result.structured_output == ["a", "b"]


def test_output_that_does_not_match_is_sent_back_with_the_reasons(cli: Path) -> None:
    prompt = 'output:{"verdict": "maybe", "score": 11}|{"verdict": "reject", "score": 2}'
    messages = run(cli, prompt, output_format=Review)

    first, second = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert first.status == "failed"
    assert "verdict" in first.output and "score" in first.output
    assert "Call `structured_output` again" in first.output
    assert second.status == "completed"
    assert messages[-1].structured_output == Review(verdict="reject", score=2)


def test_the_model_is_reminded_when_it_answers_in_text(cli: Path) -> None:
    result = run(cli, 'forget:{"answer": 7}', output_format=SCHEMA)[-1]
    assert result.structured_output == {"answer": 7}
    assert result.stop_reason == "end_turn"


def test_no_output_after_the_reminders_is_an_error(cli: Path) -> None:
    result = run(cli, "echo", output_format=SCHEMA)[-1]
    assert result.stop_reason == "error_max_structured_output_retries"
    assert result.is_error is True
    assert result.structured_output is None


def test_the_output_tool_needs_no_approval(cli: Path) -> None:
    async def deny_everything(tool, context):
        return PermissionResultDeny()

    # Neither a missing callback nor one that denies stops the SDK's own tool.
    for options in ({}, {"can_use_tool": deny_everything}, {"hooks": {}}):
        result = run(cli, 'output:{"answer": 1}', output_format=SCHEMA, **options)[-1]
        assert result.structured_output == {"answer": 1}


def test_each_turn_reports_its_own_output(cli: Path) -> None:
    async def turns() -> list:
        async with BobSDKClient(BobAgentOptions(cli_path=cli, output_format=SCHEMA)) as client:
            results = []
            for prompt in ('output:{"answer": 1}', 'output:{"answer": 2}'):
                await client.query(prompt)
                results.append([m async for m in client.receive_response()][-1].structured_output)
            return results

    assert asyncio.run(turns()) == [{"answer": 1}, {"answer": 2}]


def test_without_output_format_nothing_changes(cli: Path) -> None:
    result = run(cli, "echo")[-1]
    assert result.structured_output is None
    assert '"mcp_servers": []' in result.result


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"output_format": {"type": "object"}}, "must be a Pydantic model class"),
        ({"output_format": {"type": "json_schema"}}, "has no schema"),
        (
            {"output_format": {"type": "json_schema", "schema": {"type": "nonsense"}}},
            "not a valid JSON Schema",
        ),
        ({"output_format": SCHEMA, "mcp_servers": {"sdk_output": {"command": "x"}}}, "is used by"),
    ],
)
def test_wrong_settings_are_refused_before_bob_starts(cli: Path, options, message) -> None:
    with pytest.raises(BobSDKError, match=message):
        run(cli, "echo", **options)


# ----------------------------------------------- a mode without the mcp group

READ_ONLY = ModeDefinition(role_definition="You review code.", groups=["read"])


def test_a_mode_without_mcp_answers_in_json_text(cli: Path) -> None:
    reply = 'Here is my review.\n```json\n{"verdict": "approve", "score": 9}\n```'
    result = run(cli, f"text:{reply}", mode=READ_ONLY, output_format=Review)[-1]
    assert result.structured_output == Review(verdict="approve", score=9)
    assert result.stop_reason == "end_turn"


@pytest.mark.parametrize(
    "reply",
    [
        '{"answer": 5}',  # Only JSON.
        'The answer follows. {"answer": 5} That is all.',  # JSON inside text.
        '```\n{"wrong": true}\n```\nCorrection:\n```json\n{"answer": 5}\n```',  # The last block.
    ],
)
def test_json_is_found_in_the_reply(cli: Path, reply: str) -> None:
    result = run(cli, f"text:{reply}", mode=READ_ONLY, output_format=SCHEMA)[-1]
    assert result.structured_output == {"answer": 5}


def test_json_text_that_does_not_match_is_corrected(cli: Path) -> None:
    prompt = 'text:{"verdict": "maybe", "score": 3}|{"verdict": "reject", "score": 3}'
    result = run(cli, prompt, mode=READ_ONLY, output_format=Review)[-1]
    assert result.structured_output == Review(verdict="reject", score=3)
    assert result.num_turns == 2  # The first answer, and the corrected one.


def test_no_json_text_after_the_reminders_is_an_error(cli: Path) -> None:
    result = run(cli, "text:no json|still none|none again", mode=READ_ONLY, output_format=SCHEMA)[
        -1
    ]
    assert result.stop_reason == "error_max_structured_output_retries"
    assert result.structured_output is None


def test_a_mode_without_mcp_gets_no_extra_mcp_server(cli: Path) -> None:
    # The agent must not gain the mcp group or an MCP server because of typed output.
    result = run(cli, "echo", mode=READ_ONLY, output_format=SCHEMA)[-1]
    assert '"mcp_servers": []' in result.result
    assert '"groups": ["read"]' in result.result

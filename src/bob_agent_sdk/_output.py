"""Typed output: the agent's final answer as data that matches a schema.

Bob has no structured output of its own. The SDK gets it in one of two ways:

- **By tool.** The agent gets one extra tool, `structured_output`, whose input
  schema is the schema the caller asked for, and reports its answer by calling
  it. This is the more reliable way, because the model fills in a tool call.
  It needs the `mcp` tool group.
- **By text.** The agent ends its reply with the answer as JSON, and the SDK
  reads it from the text. This is for modes without the `mcp` group, such as
  a read-only reviewer: adding `mcp` would also give the agent every MCP
  server that is configured in Bob.

Either way the SDK checks the answer. Output that does not match is sent back
with the reasons, so the model can correct it.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ValidationError

from ._errors import BobSDKError
from .tools import SdkMcpServer, SdkMcpTool

# The key of the SDK's own entry in the session's MCP servers.
SERVER_KEY = "sdk_output"
TOOL_NAME = "structured_output"
BOB_TOOL_NAME = f"mcp__{SERVER_KEY}__{TOOL_NAME}"

# Sent when the model ended a turn without calling the tool.
TOOL_REMINDER = (
    f"You ended your turn without calling the `{TOOL_NAME}` tool. "
    f"Call it now with your final answer."
)
_FENCED = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


class OutputCollector:
    """Holds the output format of a session and the value the agent reported."""

    def __init__(self, output_format: dict[str, Any] | type[BaseModel], *, use_tool: bool) -> None:
        self.uses_tool = use_tool
        self._model: type[BaseModel] | None = None
        schema: Any
        if isinstance(output_format, type) and issubclass(output_format, BaseModel):
            self._model = output_format
            schema = output_format.model_json_schema()
        elif isinstance(output_format, dict) and output_format.get("type") == "json_schema":
            schema = output_format.get("schema")
        else:
            raise BobSDKError(
                "output_format must be a Pydantic model class, or "
                '{"type": "json_schema", "schema": {...}}.'
            )
        if not isinstance(schema, dict):
            raise BobSDKError("output_format has no schema.")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise BobSDKError(f"output_format is not a valid JSON Schema: {exc.message}") from exc
        self._validator = Draft202012Validator(schema)
        self._schema = schema
        # A tool's input must be an object. Other schemas go in a `value` field.
        self._wrapped = schema.get("type") != "object"
        self._tool_schema = (
            {"type": "object", "properties": {"value": schema}, "required": ["value"]}
            if self._wrapped
            else schema
        )
        self.has_value = False
        self.value: Any = None

    def reset(self) -> None:
        self.has_value = False
        self.value = None

    @property
    def instruction(self) -> str:
        """Added to every prompt of the session."""
        if self.uses_tool:
            return (
                f"When you have finished the task, report your final answer by calling the "
                f"`{TOOL_NAME}` tool exactly once. Its input is the answer. Do not write the "
                f"answer as text instead."
            )
        return (
            "When you have finished the task, end your reply with your final answer as one "
            "JSON value in a ```json code block. The JSON must match this JSON Schema:\n"
            + json.dumps(self._schema)
        )

    def server(self) -> SdkMcpServer:
        description = (
            "Report the final answer of the task. Call this exactly once, as your last step."
        )
        tool = SdkMcpTool(TOOL_NAME, description, self._tool_schema, self._receive)
        return SdkMcpServer(SERVER_KEY, "1.0.0", [tool])

    def reminder(self, reply: str | None) -> str | None:
        """What to tell the model after a turn that did not give a valid output.

        Returns None when the output is there. In text mode this reads `reply`,
        the last text of the turn.
        """
        if self.has_value:
            return None
        if self.uses_tool:
            return TOOL_REMINDER
        found, output = _find_json(reply or "")
        if not found:
            return (
                "Your reply has no JSON answer. End your reply with your final answer as "
                "one JSON value in a ```json code block that matches the schema."
            )
        problems = self._accept(output)
        if not problems:
            return None
        return (
            "Your JSON answer does not match the required schema:\n"
            + "\n".join(problems)
            + "\nReply again with the corrected JSON in a ```json code block."
        )

    def _accept(self, output: Any) -> list[str]:
        """Store `output` if it is valid. Returns the problems if it is not."""
        problems = [
            f"- {'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}"
            for error in self._validator.iter_errors(output)
        ]
        if not problems and self._model is not None:
            try:
                output = self._model.model_validate(output)
            except ValidationError as exc:  # A rule that the JSON Schema cannot express.
                problems = [
                    f"- {'/'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}"
                    for e in exc.errors()
                ]
        if not problems:
            self.value = output
            self.has_value = True
        return problems

    async def _receive(self, args: dict[str, Any]) -> dict[str, Any] | str:
        problems = self._accept(args.get("value") if self._wrapped else args)
        if problems:
            text = (
                "The output does not match the required schema:\n"
                + "\n".join(problems)
                + f"\nCall `{TOOL_NAME}` again with a corrected output."
            )
            return {"content": [{"type": "text", "text": text}], "isError": True}
        return "Output accepted. Your work is done. End your turn now."


def _find_json(text: str) -> tuple[bool, Any]:
    """Find the JSON answer in a reply. Returns (found, value).

    Looks in the last code block first, then at the whole reply, then for the
    first JSON value that starts anywhere in it.
    """
    candidates = [*reversed(_FENCED.findall(text)), text]
    for candidate in candidates:
        try:
            return True, json.loads(candidate)
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[{\[]", text):
        try:
            return True, decoder.raw_decode(text, match.start())[0]
        except json.JSONDecodeError:
            pass
    return False, None

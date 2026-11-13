"""Turns the stream of ACP session updates into SDK messages."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    PlanEntry,
    PlanMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultMessage,
    ToolUseBlock,
)


def to_dict(model: BaseModel) -> dict[str, Any]:
    """The JSON form of an ACP model, with the field names used on the wire."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def tool_use_block(tool_call: Any, name: str | None = None) -> ToolUseBlock:
    """Build a block from an ACP `ToolCallStart` or `ToolCallUpdate`."""
    return ToolUseBlock(
        name=name,
        id=tool_call.tool_call_id,
        title=tool_call.title or "",
        kind=tool_call.kind,
        input=tool_call.raw_input if isinstance(tool_call.raw_input, dict) else {},
        locations=[location.path for location in tool_call.locations or []],
    )


def _tool_output(update: Any) -> str | None:
    texts = [
        item.content.text
        for item in update.content or []
        if item.type == "content" and item.content.type == "text"
    ]
    if texts:
        return "".join(texts)
    raw = update.raw_output
    result = raw.get("result") if isinstance(raw, dict) else None
    return result if isinstance(result, str) else None


class MessageAssembler:
    """Bob streams text in small chunks. This class joins the chunks so that
    callers receive whole messages: one `AssistantMessage` per run of text and
    reasoning, closed by the next tool call or the end of the turn."""

    def __init__(
        self,
        session_id: str,
        include_partial_messages: bool,
        tool_names: dict[str, str],
    ) -> None:
        self._session_id = session_id
        self._partial = include_partial_messages
        self._tool_names = tool_names  # Tool call ID to Bob tool name. Filled by hooks.
        self._blocks: list[ContentBlock] = []
        self._last_text: str | None = None
        self._tool_calls = 0
        self._finished_tools: set[str] = set()

    def feed(self, update: Any) -> list[Message]:
        """Take one ACP session update and return the messages it completes."""
        out: list[Message] = []
        if self._partial:
            out.append(StreamEvent(self._session_id, to_dict(update)))
        kind = update.session_update

        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            if update.content.type == "text":
                self._append_text(kind == "agent_thought_chunk", update.content.text)
        elif kind == "tool_call":
            self._tool_calls += 1
            self._blocks.append(tool_use_block(update, self._tool_names.get(update.tool_call_id)))
            out += self._flush()
            out += self._tool_result(update)
        elif kind == "tool_call_update":
            out += self._tool_result(update)
        elif kind == "plan":
            out += self._flush()
            out.append(
                PlanMessage([PlanEntry(e.content, e.status, e.priority) for e in update.entries])
            )
        else:
            out += self._flush()
            out.append(SystemMessage(kind, to_dict(update)))
        return out

    def finish(self, stop_reason: str, duration_ms: int, **usage: Any) -> list[Message]:
        """Close the turn. `usage` holds the cost fields of `ResultMessage`."""
        out = self._flush()
        out.append(
            ResultMessage(
                session_id=self._session_id,
                stop_reason=stop_reason,
                duration_ms=duration_ms,
                num_tool_calls=self._tool_calls,
                result=self._last_text,
                is_error=stop_reason not in ("end_turn", "cancelled"),
                **usage,
            )
        )
        self._last_text = None
        self._tool_calls = 0
        self._finished_tools.clear()
        return out

    def _append_text(self, thinking: bool, text: str) -> None:
        last = self._blocks[-1] if self._blocks else None
        if thinking:
            if isinstance(last, ThinkingBlock):
                last.thinking += text
            else:
                self._blocks.append(ThinkingBlock(text))
        elif isinstance(last, TextBlock):
            last.text += text
        else:
            self._blocks.append(TextBlock(text))

    def _flush(self) -> list[Message]:
        if not self._blocks:
            return []
        blocks, self._blocks = self._blocks, []
        for block in blocks:
            if isinstance(block, TextBlock):
                self._last_text = block.text
        return [AssistantMessage(blocks)]

    def _tool_result(self, update: Any) -> list[Message]:
        if update.status not in ("completed", "failed"):
            return []
        if update.tool_call_id in self._finished_tools:
            return []  # Bob can report the final status more than once.
        self._finished_tools.add(update.tool_call_id)
        return [
            *self._flush(),
            ToolResultMessage(
                tool_use_id=update.tool_call_id,
                status=update.status,
                output=_tool_output(update),
                content=[to_dict(item) for item in update.content or []],
                raw_output=update.raw_output,
            ),
        ]

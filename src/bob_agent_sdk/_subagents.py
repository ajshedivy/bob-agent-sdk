"""Reports the work of Bob's subagents.

Bob sends ACP updates only for the session's own task. A subagent is a task of
its own, so its text, tool calls and results never arrive over ACP. Two other
sources have them:

- Bob's hook events. `PreToolUse` and `PostToolUse` fire for a subagent's tool
  calls too, but carry only the session's ID, not the subagent's.
  `PostToolUse` does not fire for a failed tool call.
- Bob's task export. At the end of the turn, the `spawn_subagent` tool message
  holds the subagent's whole conversation (`messages`) and its usage (`_meta`).

Bob runs the agent's own tool calls one at a time, except that the
`spawn_subagent` calls of one response run together. So while a subagent runs,
a hook event for another tool is a subagent's. With one subagent running, the
SDK reports it at once. With several, it cannot tell which one made the call.
A subagent waits for its own tool call, so when only one subagent is still
running while the call is open, that one made it. Calls that stay unknown are
reported from the export at the end of the turn. Each tool call and result is
reported once.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    SubagentRef,
    SubagentResultMessage,
    TextBlock,
    ToolResultMessage,
    ToolUseBlock,
)

SPAWN_TOOL = "spawn_subagent"
# Bob sends the ACP `tool_call` of the agent's own call right after its
# PreToolUse hooks return. A hook-seen call without one after this long is a
# subagent's.
GRACE_SECONDS = 1.0


@dataclass
class _Call:
    block: ToolUseBlock
    ref: SubagentRef | None  # None while several subagents run.
    timer: asyncio.TimerHandle | None = None
    seen: float = 0.0  # When its PreToolUse arrived: the time it is reported with.
    confirmed: bool = False  # Not the agent's own call: no ACP tool_call came.
    reported: bool = False  # Emitted, with its subagent.


def message_data(entry: dict[str, Any]) -> dict[str, Any]:
    """The message of a task export row. Bob stores it as JSON text or as an object."""
    data = entry.get("data", entry)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def spawn_messages(export: Any, session_id: str) -> dict[str, dict[str, Any]]:
    """The `spawn_subagent` tool messages of the session's task, by tool call ID."""
    found: dict[str, dict[str, Any]] = {}
    for entry in (export or {}).get("tasks") or []:
        if (entry.get("task") or {}).get("id") != session_id:
            continue
        for row in entry.get("messages") or []:
            data = message_data(row)
            signature = (data.get("toolUsage") or {}).get("signature") or {}
            if data.get("role") == "tool" and signature.get("name") == SPAWN_TOOL:
                if signature.get("id"):
                    found[signature["id"]] = data
    return found


def _seconds(meta: Any) -> float | None:
    stamp = meta.get("timestamp") if isinstance(meta, dict) else None
    return stamp / 1000 if isinstance(stamp, (int, float)) else None


class SubagentTracker:
    """Turns hook events and the task export into subagent messages. One per session."""

    def __init__(self, emit: Callable[[Message], None]) -> None:
        self._emit = emit
        self._running: dict[str, str] = {}  # spawn_subagent call ID to subagent name.
        self._spawns: dict[str, str] = {}  # Every spawn of this turn, in order.
        self._agent_calls: set[str] = set()  # Tool call IDs that came over ACP.
        self._calls: dict[str, _Call] = {}  # Subagent tool calls seen in hook events.
        self._results: set[str] = set()  # Subagent results already reported.

    # ------------------------------------------------------------ ACP side

    def on_tool_call(self, tool_call_id: str, name: str | None, raw_input: Any) -> None:
        """An ACP `tool_call`: always the agent's own."""
        self._agent_calls.add(tool_call_id)
        call = self._calls.pop(tool_call_id, None)
        if call is not None and call.timer is not None:
            call.timer.cancel()
        if name == SPAWN_TOOL:
            given = raw_input.get("name") if isinstance(raw_input, dict) else None
            subagent = given if isinstance(given, str) and given else "general"
            self._running[tool_call_id] = subagent
            self._spawns[tool_call_id] = subagent

    def on_tool_done(self, tool_call_id: str) -> None:
        self._running.pop(tool_call_id, None)

    def permission(self, tool_call_id: str) -> SubagentRef | None:
        """The subagent that asks for this tool call, or None for the agent's own call.

        Bob sends the agent's own `tool_call` before it asks, so a request for a
        call that did not come over ACP is a subagent's.
        """
        if tool_call_id in self._agent_calls or not self._running:
            return None
        call = self._calls.get(tool_call_id)
        if call is not None:
            self._confirm(call)  # The call comes before its permission request.
            if call.ref is not None:
                return call.ref
        return self._current() or self._shared_name()

    # ----------------------------------------------------------- hook side

    def on_hook(self, payload: dict[str, Any]) -> None:
        """A hook event, before the hook callbacks run."""
        event = payload.get("hook_event_name")
        tool_call_id = payload.get("tool_use_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        if event == "PreToolUse":
            name = payload.get("tool_name")
            if (
                name == SPAWN_TOOL  # Subagents cannot start subagents.
                or tool_call_id in self._agent_calls
                # The first event for an ID counts: the commands that Bob runs can
                # also reach the hook server.
                or tool_call_id in self._calls
                or not self._running
            ):
                return
            tool_input = payload.get("tool_input")
            block = ToolUseBlock(
                id=tool_call_id,
                title=str(name or "tool"),
                kind=None,
                input=tool_input if isinstance(tool_input, dict) else {},
                name=name if isinstance(name, str) else None,
            )
            self._calls[tool_call_id] = _Call(block, self._current(), seen=time.time())
        elif event == "PostToolUse":
            call = self._calls.get(tool_call_id)
            if call is None:
                return
            self._confirm(call)
            if not call.reported or call.ref is None or tool_call_id in self._results:
                return
            self._results.add(tool_call_id)
            response = payload.get("tool_response")
            self._emit(
                ToolResultMessage(
                    tool_use_id=tool_call_id,
                    status="completed",
                    output=response if isinstance(response, str) else None,
                    content=[],
                    subagent=self._stamped(call.ref),
                )
            )

    def on_hook_done(self, payload: dict[str, Any]) -> None:
        """A hook event after the hook callbacks ran: start the wait for the ACP `tool_call`."""
        if payload.get("hook_event_name") != "PreToolUse":
            return
        call = self._calls.get(str(payload.get("tool_use_id")))
        if call is not None and not call.confirmed and call.timer is None:
            loop = asyncio.get_running_loop()
            call.timer = loop.call_later(GRACE_SECONDS, self._confirm, call)

    # ------------------------------------------------------- end of a turn

    def finish(self, export: Any, session_id: str) -> list[Message]:
        """The subagent messages that only the export has, and how each subagent ended.

        Covers the `spawn_subagent` calls of this turn. Ends the turn's tracking."""
        out: list[Message] = []
        for spawn_id, data in spawn_messages(export, session_id).items():
            if spawn_id not in self._agent_calls:
                continue  # A spawn of an earlier turn.
            meta = data.get("_meta") or {}
            arguments = ((data.get("toolUsage") or {}).get("signature") or {}).get("arguments")
            given = arguments.get("name") if isinstance(arguments, dict) else None
            name = str(self._spawns.get(spawn_id) or meta.get("agentType") or given or "general")
            ref = SubagentRef(tool_use_id=spawn_id, name=name)
            nested = [message_data(m) for m in data.get("messages") or []]
            out += self._transcript(nested, ref)
            spend = meta.get("spend") or {}
            signature = (data.get("toolUsage") or {}).get("signature") or {}
            reason = meta.get("loopExitReason")
            cost = spend.get("cost")
            out.append(
                SubagentResultMessage(
                    tool_use_id=spawn_id,
                    name=name,
                    subagent_id=meta.get("subagentId"),
                    stop_reason=reason if isinstance(reason, str) else None,
                    is_error=bool(signature.get("isError")) or reason == "error",
                    duration_ms=meta.get("durationMs"),
                    num_turns=sum(1 for m in nested if m.get("role") == "assistant"),
                    num_tool_calls=sum(1 for m in nested if m.get("role") == "tool"),
                    cost=cost if isinstance(cost, (int, float)) else None,
                )
            )
        self.reset()
        return out

    def reset(self) -> None:
        for call in self._calls.values():
            if call.timer is not None:
                call.timer.cancel()
        self._running.clear()
        self._spawns.clear()
        self._agent_calls.clear()
        self._calls.clear()
        self._results.clear()

    # ------------------------------------------------------------ internals

    def _transcript(self, messages: list[dict[str, Any]], ref: SubagentRef) -> list[Message]:
        out: list[Message] = []
        for data in messages:
            role = data.get("role")
            at = _seconds(data.get("_meta"))
            stamped = SubagentRef(ref.tool_use_id, ref.name, at)
            if role == "assistant":
                blocks: list[ContentBlock] = []
                text = data.get("content")
                if isinstance(text, str) and text.strip():
                    blocks.append(TextBlock(text))
                for call in data.get("toolCalls") or []:
                    call_id = call.get("id")
                    known = self._calls.get(call_id)
                    if not call_id or (known is not None and known.reported):
                        continue
                    name = call.get("name")
                    arguments = call.get("arguments")
                    blocks.append(
                        ToolUseBlock(
                            id=call_id,
                            title=str(name or "tool"),
                            kind=None,
                            input=arguments if isinstance(arguments, dict) else {},
                            name=name if isinstance(name, str) else None,
                        )
                    )
                if blocks:
                    out.append(AssistantMessage(blocks, subagent=stamped))
            elif role == "tool":
                signature = (data.get("toolUsage") or {}).get("signature") or {}
                call_id = signature.get("id")
                if not call_id or call_id in self._results:
                    continue
                content = data.get("content")
                out.append(
                    ToolResultMessage(
                        tool_use_id=call_id,
                        status="failed" if signature.get("isError") else "completed",
                        output=content if isinstance(content, str) else None,
                        content=[],
                        subagent=stamped,
                    )
                )
        return out

    def _current(self) -> SubagentRef | None:
        """The running subagent, when exactly one runs."""
        if len(self._running) != 1:
            return None
        ((spawn_id, name),) = self._running.items()
        return SubagentRef(tool_use_id=spawn_id, name=name)

    def _shared_name(self) -> SubagentRef:
        """A permission request while several subagents run: the name if they share one."""
        names = set(self._running.values())
        return SubagentRef(tool_use_id=None, name=names.pop() if len(names) == 1 else None)

    def _confirm(self, call: _Call) -> None:
        """The call is a subagent's. Emit it as soon as it is known whose."""
        if call.timer is not None:
            call.timer.cancel()
            call.timer = None
        if call.reported or call.block.id in self._agent_calls:
            return
        call.confirmed = True
        if call.ref is None:
            call.ref = self._current()
        if call.ref is not None:
            call.reported = True
            ref = call.ref
            self._emit(
                AssistantMessage(
                    [call.block], subagent=SubagentRef(ref.tool_use_id, ref.name, call.seen)
                )
            )

    @staticmethod
    def _stamped(ref: SubagentRef) -> SubagentRef:
        return SubagentRef(ref.tool_use_id, ref.name, time.time())

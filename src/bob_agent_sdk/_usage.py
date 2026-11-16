"""Reads cost and turn data from Bob's task export.

Bob does not report usage over ACP. It does save it with the task, and its ACP
extension `_bob/task/export` returns the saved task. The SDK reads the numbers
from there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UsageSnapshot:
    total_cost: float = 0.0
    """Cost of the whole session so far, including its subagents."""
    costs: dict[str, Any] = field(default_factory=dict)
    """Bob's raw cost record. Token counts appear when Bob records them."""
    model: str | None = None
    model_requests: int = 0
    """Assistant messages saved so far, also those of subagents. Bob saves one per model request."""
    tool_call_ids: set[str] = field(default_factory=set)


def parse_export(export: Any, session_id: str) -> UsageSnapshot | None:
    """Return the usage of `session_id`, or None if the export does not have it."""
    for entry in (export or {}).get("tasks") or []:
        task = entry.get("task") or {}
        if task.get("id") != session_id:
            continue
        costs = task.get("costs") or {}
        snapshot = UsageSnapshot(
            total_cost=float(costs.get("cost") or 0.0),
            costs=costs,
            model=((task.get("env") or {}).get("model") or {}).get("id"),
        )
        for message in entry.get("messages") or []:
            data = message.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    data = None
            if message.get("role") == "tool" and isinstance(data, dict):
                # A subagent's conversation is saved inside its spawn_subagent tool message.
                # Its model requests count too; its cost is already in the task's cost.
                snapshot.model_requests += sum(
                    1
                    for nested in data.get("messages") or []
                    if isinstance(nested, dict) and nested.get("role") == "assistant"
                )
            if message.get("role") != "assistant":
                continue
            snapshot.model_requests += 1
            for call in (data or {}).get("toolCalls") or []:
                if call.get("id"):
                    snapshot.tool_call_ids.add(call["id"])
        return snapshot
    return None

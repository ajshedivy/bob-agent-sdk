"""Tests for the work of Bob's subagents, which Bob does not send over ACP."""

import asyncio
from pathlib import Path
from typing import Any

from conftest import collect

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    SubagentResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultMessage,
    ToolUseBlock,
)


def options(cli: Path, cwd: Path, **kwargs: Any) -> BobAgentOptions:
    return BobAgentOptions(cli_path=cli, cwd=cwd, hooks={}, **kwargs)


def subagent_items(messages: list[Any]) -> list[tuple[str, str, str | None]]:
    """(kind, id or text, spawn call ID) for each subagent message, in order."""
    items = []
    for message in messages:
        if isinstance(message, AssistantMessage) and message.subagent:
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    items.append(("call", block.id, message.subagent.tool_use_id))
                elif isinstance(block, TextBlock):
                    items.append(("text", block.text, message.subagent.tool_use_id))
        elif isinstance(message, ToolResultMessage) and message.subagent:
            items.append(("result", message.tool_use_id, message.subagent.tool_use_id))
    return items


def test_one_subagent_is_reported_live_then_completed_from_the_export(
    cli: Path, tmp_path: Path
) -> None:
    messages = asyncio.run(collect("subagents:tester", options(cli, tmp_path)))
    spawn_result = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, ToolResultMessage) and m.tool_use_id == "spawn0"
    )
    live = subagent_items(messages[:spawn_result])
    # While it runs: the call and the result that hook events show.
    assert live == [("call", "spawn0-ok", "spawn0"), ("result", "spawn0-ok", "spawn0")]
    late = subagent_items(messages[spawn_result:])
    # At the end of the turn: its text and the failed result, which no hook event shows.
    # The failed call itself is live when it waited longer than the grace time.
    assert [item for item in late if item[1] != "spawn0-bad" or item[0] == "result"] == [
        ("text", "tester step 0", "spawn0"),
        ("text", "tester step 1", "spawn0"),
        ("result", "spawn0-bad", "spawn0"),
        ("text", "tester is done", "spawn0"),
    ]
    # Each call and result once: the export does not repeat what was live.
    everything = subagent_items(messages)
    assert len(everything) == len(set(everything)) == 7
    failed = next(
        m for m in messages if isinstance(m, ToolResultMessage) and m.tool_use_id == "spawn0-bad"
    )
    assert failed.is_error and failed.output == "bad failed"
    assert failed.subagent is not None and failed.subagent.name == "tester"
    assert failed.subagent.at == 1.001  # Bob's own time of the message.
    summary, result = messages[-2:]
    assert isinstance(summary, SubagentResultMessage)
    assert (summary.tool_use_id, summary.name, summary.subagent_id) == (
        "spawn0",
        "tester",
        "task-spawn0",
    )
    assert (summary.num_turns, summary.num_tool_calls, summary.cost) == (3, 2, 0.02)
    assert (summary.stop_reason, summary.is_error, summary.duration_ms) == ("stop", False, 1200)
    assert isinstance(result, ResultMessage)
    # The agent's two model requests and the subagent's three.
    assert result.num_turns == 5
    # The agent's own result text is not the subagent's.
    assert result.result is None


def test_parallel_subagents_are_reported_once_each_and_under_their_own_spawn(
    cli: Path, tmp_path: Path
) -> None:
    messages = asyncio.run(collect("subagents:a,b", options(cli, tmp_path)))
    first_spawn_result = next(
        i for i, m in enumerate(messages) if isinstance(m, ToolResultMessage) and not m.subagent
    )
    # While both run, hook events cannot say which subagent made a call.
    assert subagent_items(messages[:first_spawn_result]) == []
    items = subagent_items(messages)
    assert len(items) == len(set(items))
    for spawn, name in (("spawn0", "a"), ("spawn1", "b")):
        assert sorted(item[:2] for item in items if item[2] == spawn) == sorted(
            [
                ("text", f"{name} step 0"),
                ("call", f"{spawn}-ok"),
                ("result", f"{spawn}-ok"),
                ("text", f"{name} step 1"),
                ("call", f"{spawn}-bad"),
                ("result", f"{spawn}-bad"),
                ("text", f"{name} is done"),
            ]
        )
    summaries = [m for m in messages if isinstance(m, SubagentResultMessage)]
    assert {s.tool_use_id for s in summaries} == {"spawn0", "spawn1"}


def test_when_one_subagent_is_left_its_calls_are_live_and_never_given_to_another(
    cli: Path, tmp_path: Path
) -> None:
    # b runs at once and ends; its failed call has no PostToolUse, and its grace time runs out
    # while a, which waits 2 s, still runs. That call stays b's. Then a runs alone: live.
    messages = asyncio.run(collect("subagents:a:2,b", options(cli, tmp_path)))
    items = subagent_items(messages)
    assert len(items) == len(set(items))
    assert {item[2] for item in items if item[1].startswith("spawn1-")} == {"spawn1"}
    assert {item[2] for item in items if item[1].startswith("spawn0-")} == {"spawn0"}
    spawn0_result = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, ToolResultMessage) and m.tool_use_id == "spawn0"
    )
    assert ("call", "spawn0-ok", "spawn0") in subagent_items(messages[:spawn0_result])
    assert ("result", "spawn0-ok", "spawn0") in subagent_items(messages[:spawn0_result])


def test_a_subagent_permission_request_names_the_subagent(cli: Path, tmp_path: Path) -> None:
    asked: list[tuple[str, Any]] = []

    async def can_use_tool(tool: ToolUseBlock, context: ToolPermissionContext) -> Any:
        asked.append((tool.id, context.subagent))
        return PermissionResultAllow()

    opts = options(cli, tmp_path, can_use_tool=can_use_tool)
    messages = asyncio.run(collect("subagents:tester!", opts))
    assert len(asked) == 1
    tool_id, subagent = asked[0]
    assert tool_id == "spawn0-ok"
    assert (subagent.tool_use_id, subagent.name) == ("spawn0", "tester")
    # The call is reported before Bob waits for the decision, and only once.
    assert [i for i in subagent_items(messages) if i[:2] == ("call", "spawn0-ok")] == [
        ("call", "spawn0-ok", "spawn0")
    ]


def test_permission_with_parallel_subagents_names_only_what_is_certain(
    cli: Path, tmp_path: Path
) -> None:
    asked: list[Any] = []

    async def can_use_tool(tool: ToolUseBlock, context: ToolPermissionContext) -> Any:
        asked.append(context.subagent)
        return PermissionResultAllow()

    opts = options(cli, tmp_path, can_use_tool=can_use_tool)
    asyncio.run(collect("subagents:x!,y!", opts))
    assert [(s.tool_use_id, s.name) for s in asked] == [(None, None), (None, None)]
    asked.clear()
    asyncio.run(collect("subagents:x!,x!", opts))
    assert [(s.tool_use_id, s.name) for s in asked] == [(None, "x"), (None, "x")]


def test_the_agents_own_tool_calls_are_not_a_subagents(cli: Path, tmp_path: Path) -> None:
    messages = asyncio.run(collect("work", options(cli, tmp_path)))
    assert subagent_items(messages) == []
    assert not any(isinstance(m, SubagentResultMessage) for m in messages)

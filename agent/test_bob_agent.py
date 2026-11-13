"""Tests for the adapter. They use the SDK's stand-in for `bob acp`, so they need no login."""

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest
from agno.db.in_memory import InMemoryDb
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunPausedEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus
from bob_agent import BobAgent

FAKE_BOB = Path(__file__).parent.parent / "tests" / "fake_bob.py"


@pytest.fixture
def agent(tmp_path: Path) -> BobAgent:
    cli = tmp_path / "bob"
    cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_BOB}" "$@"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return BobAgent(name="Test Bob", cwd=workspace, options_kwargs={"cli_path": cli})


async def events(agent: BobAgent, prompt: str, **kwargs) -> list:
    return [event async for event in agent.arun(prompt, stream=True, **kwargs)]


def test_run_returns_bobs_final_text(agent: BobAgent) -> None:
    output = asyncio.run(agent.arun("echo"))
    assert output.status == RunStatus.completed
    assert json.loads(output.content)["session"]["cwd"] == str(Path(agent.cwd).resolve())


def test_stream_maps_text_and_tool_calls(agent: BobAgent) -> None:
    stream = asyncio.run(events(agent, "work"))

    started = [e for e in stream if isinstance(e, ToolCallStartedEvent)]
    completed = [e for e in stream if isinstance(e, ToolCallCompletedEvent)]
    assert [e.tool.tool_call_id for e in started] == ["w1", "w2", "w3", "w4", "w5"]
    assert started[0].tool.tool_name == "edit"  # The kind of action: hooks are off.
    assert started[0].tool.tool_args == {"path": "1.txt", "content": "hi"}
    assert [e.tool.tool_call_id for e in completed] == ["w1", "w2", "w3", "w4", "w5"]
    assert json.loads(completed[0].tool.result) == {"path": "1.txt", "content": "hi"}
    assert completed[0].tool.tool_call_error is False

    text = "".join(e.content for e in stream if isinstance(e, RunContentEvent))
    assert text == "finished"
    assert isinstance(stream[-1], RunCompletedEvent)


def test_same_agno_session_resumes_the_bob_session(agent: BobAgent) -> None:
    async def run() -> tuple[dict, dict, dict]:
        first = await agent.arun("echo", session_id="chat")
        second = await agent.arun("echo", session_id="chat")
        other = await agent.arun("echo", session_id="another chat")
        return tuple(json.loads(o.content)["session"] for o in (first, second, other))

    first, second, other = asyncio.run(run())
    assert "resumed" not in first
    assert second == {"resumed": "session-1"}
    assert "resumed" not in other


def test_a_limit_marks_the_run_as_failed(agent: BobAgent) -> None:
    agent.max_turns = 2
    output = asyncio.run(agent.arun("work"))
    assert output.status == RunStatus.error
    assert "max_turns" in output.content

    stream = asyncio.run(events(agent, "work"))
    assert isinstance(stream[-1], RunErrorEvent)


# ------------------------------------------------- approval by a person (HITL)


async def run_with_decision(agent: BobAgent, confirmed: bool) -> list:
    """Run a prompt that makes Bob ask for approval, and answer the request."""
    stream = []
    decision = None

    async def decide(paused: RunPausedEvent) -> None:
        (tool,) = paused.tools
        assert tool.requires_confirmation
        assert (tool.tool_name, tool.tool_args) == ("execute", {"command": "rm x"})
        tool.confirmed = confirmed
        await agent.acontinue_run(run_id=paused.run_id, session_id="chat", updated_tools=[tool])

    async for event in agent.arun("tool", stream=True, session_id="chat"):
        stream.append(event)
        if isinstance(event, RunPausedEvent):
            # The run waits for the decision, so it must come from another task.
            decision = asyncio.create_task(decide(event))
    assert decision is not None, "the run never paused"
    await decision
    return stream


@pytest.mark.parametrize(("confirmed", "outcome"), [(True, "allow"), (False, "reject")])
def test_approval_request_pauses_the_run_for_a_person(
    agent: BobAgent, confirmed: bool, outcome: str
) -> None:
    agent.db = InMemoryDb()
    stream = asyncio.run(run_with_decision(agent, confirmed))

    (completed,) = [e for e in stream if isinstance(e, ToolCallCompletedEvent)]
    assert completed.tool.result == outcome
    assert completed.tool.tool_call_error is (not confirmed)
    assert isinstance(stream[-1], RunCompletedEvent)


def test_without_a_database_approval_requests_are_rejected(agent: BobAgent) -> None:
    stream = asyncio.run(events(agent, "tool"))
    assert not any(isinstance(e, RunPausedEvent) for e in stream)
    (completed,) = [e for e in stream if isinstance(e, ToolCallCompletedEvent)]
    assert completed.tool.result == "reject"


def test_a_person_is_not_asked_when_something_else_decides(agent: BobAgent) -> None:
    agent.db = InMemoryDb()
    assert agent._uses_native_hitl

    agent.auto_approve = True
    assert not agent._uses_native_hitl

    async def can_use_tool(tool, context):
        raise AssertionError("not called here")

    agent.auto_approve = False
    agent.options_kwargs["can_use_tool"] = can_use_tool
    assert not agent._uses_native_hitl

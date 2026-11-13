"""Tests for the adapter. They use the SDK's stand-in for `bob acp`, so they need no login."""

import asyncio
import json
import stat
import sys
from pathlib import Path

import pytest
from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
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
    assert started[0].tool.tool_name == "Writing file"  # The title: hooks are off.
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

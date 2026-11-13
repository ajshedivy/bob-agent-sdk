import asyncio
import json
from pathlib import Path

import pytest
from conftest import collect

from bob_agent_sdk import (
    AssistantMessage,
    AuthenticationError,
    BobAgentOptions,
    BobSDKClient,
    BobSDKError,
    CLINotFoundError,
    PermissionResultAllow,
    PermissionResultDeny,
    PlanMessage,
    ProcessError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)


def test_query_joins_chunks_and_passes_options(cli: Path, tmp_path: Path) -> None:
    @tool("ping", "Ping", {})
    async def ping(args):
        return "pong"

    options = BobAgentOptions(
        cli_path=cli,
        cwd=tmp_path,
        mode="plan",
        api_key="secret",
        auto_approve=True,
        disable_subagents=True,
        log_level="debug",
        mcp_servers={
            "local": create_sdk_mcp_server("local", tools=[ping]),
            "files": {"command": "files-mcp", "args": ["--root", "."], "env": {"A": "1"}},
            "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {"X": "y"}},
        },
    )
    messages = asyncio.run(collect("echo", options))

    init, assistant, result = messages
    assert isinstance(init, SystemMessage) and init.subtype == "init"
    assert init.data["session_id"] == "session-1"
    assert isinstance(assistant, AssistantMessage)
    thinking, text = assistant.content
    assert thinking == ThinkingBlock("hm")
    assert isinstance(text, TextBlock)
    state = json.loads(text.text)
    assert state["argv"] == [
        "acp", "--auto-approve", "--disable-subagents", "--log-level", "debug",
    ]  # fmt: skip
    assert state["api_key"] == "secret"
    assert state["session"] == {
        "cwd": str(tmp_path.resolve()),
        "mode": "plan",
        "mcp_servers": ["local", "files", "remote"],
        "mode_files": [],
        "agent_files": {},
        "skills": {},
    }
    assert isinstance(result, ResultMessage)
    assert result.stop_reason == "end_turn"
    assert result.session_id == "session-1"
    assert result.result == text.text


def test_include_partial_messages_yields_raw_updates(cli: Path) -> None:
    options = BobAgentOptions(cli_path=cli, include_partial_messages=True)
    messages = asyncio.run(collect("echo", options))
    events = [m.event["sessionUpdate"] for m in messages if isinstance(m, StreamEvent)]
    assert events == ["agent_thought_chunk", "agent_message_chunk", "agent_message_chunk"]


@pytest.mark.parametrize(
    ("decision", "expected_choice", "expected_status"),
    [
        (PermissionResultAllow(), "allow", "completed"),
        (PermissionResultAllow(always=True), "always", "completed"),
        (PermissionResultDeny(), "reject", "failed"),
        (None, "reject", "failed"),  # No callback: reject.
    ],
)
def test_permission_decisions(cli: Path, decision, expected_choice, expected_status) -> None:
    seen = []

    async def can_use_tool(tool_use, context):
        seen.append((tool_use, context))
        return decision

    options = BobAgentOptions(cli_path=cli, can_use_tool=can_use_tool if decision else None)
    messages = asyncio.run(collect("tool", options))

    tool_use = next(m for m in messages if isinstance(m, AssistantMessage)).content[0]
    assert tool_use == ToolUseBlock(
        id="t1", title="rm x", kind="execute", input={"command": "rm x"}
    )
    (tool_result,) = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert (tool_result.output, tool_result.status) == (expected_choice, expected_status)
    assert tool_result.is_error == (expected_status == "failed")
    assert messages[-1].num_tool_calls == 1
    if decision:
        assert seen[0][0] == tool_use
        assert [o["kind"] for o in seen[0][1].options] == [
            "allow_once", "allow_always", "reject_once",
        ]  # fmt: skip


def test_permission_callback_error_reaches_the_reader(cli: Path) -> None:
    async def can_use_tool(tool_use, context):
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        asyncio.run(collect("tool", BobAgentOptions(cli_path=cli, can_use_tool=can_use_tool)))


def test_plan_update(cli: Path) -> None:
    messages = asyncio.run(collect("plan", BobAgentOptions(cli_path=cli)))
    plan = next(m for m in messages if isinstance(m, PlanMessage))
    assert [(e.content, e.status, e.priority) for e in plan.entries] == [
        ("step", "pending", "high")
    ]


def test_multi_turn_interrupt_and_set_mode(cli: Path) -> None:
    async def run() -> None:
        async with BobSDKClient(BobAgentOptions(cli_path=cli)) as client:
            await client.query("hang")
            with pytest.raises(BobSDKError, match="already in progress"):
                await client.query("echo")
            await asyncio.sleep(0.2)
            await client.interrupt()
            first = [m async for m in client.receive_response()]
            assert first[-1].stop_reason == "cancelled"
            assert first[-1].result == "working"

            await client.set_mode("ask")
            await client.query("echo")
            second = [m async for m in client.receive_response()]
            state = json.loads(second[-1].result)
            assert state["session"]["mode"] == "ask"

    asyncio.run(run())


def test_resume(cli: Path) -> None:
    messages = asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, resume="old-session")))
    assert messages[-1].session_id == "old-session"
    assert json.loads(messages[-1].result)["session"] == {"resumed": "old-session"}


def test_process_exit_raises_process_error(cli: Path) -> None:
    with pytest.raises(ProcessError) as info:
        asyncio.run(collect("die", BobAgentOptions(cli_path=cli)))
    assert info.value.exit_code == 3
    assert "boom" in info.value.stderr


def test_auth_required_raises_authentication_error(cli: Path) -> None:
    with pytest.raises(AuthenticationError):
        asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, api_key="bad-key")))


def echoed_key(cli: Path, **options) -> str | None:
    """The API key that the Bob process received."""
    result = asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, **options)))[-1]
    return json.loads(result.result)["api_key"]


def test_login_uses_the_api_key_from_the_environment(cli: Path) -> None:
    assert echoed_key(cli) == "test-key"
    assert echoed_key(cli, api_key="from-options") == "from-options"
    assert echoed_key(cli, env={"BOB_API_KEY": "from-env-option"}) == "from-env-option"


def test_no_api_key_stops_before_bob_starts(
    cli: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOB_API_KEY")
    # The path does not exist: the error must come before the SDK tries to start Bob.
    options = BobAgentOptions(cli_path=tmp_path / "missing")
    with pytest.raises(AuthenticationError, match="BOB_USE_SSO=1"):
        asyncio.run(collect("echo", options))


def test_sso_is_used_only_when_asked_for(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOB_USE_SSO", "1")
    # Bob must not see the key from the environment, or it would use it.
    assert echoed_key(cli) is None
    monkeypatch.delenv("BOB_API_KEY")
    assert echoed_key(cli) is None
    # A key passed in code is a clear choice, so it still wins.
    assert echoed_key(cli, api_key="from-options") == "from-options"


def test_missing_cli_raises_cli_not_found(tmp_path: Path) -> None:
    with pytest.raises(CLINotFoundError):
        asyncio.run(collect("echo", BobAgentOptions(cli_path=tmp_path / "missing")))

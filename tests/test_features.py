"""Tests for the features built on Bob's workspace files and task export:
usage, limits, hooks, custom modes, subagents and teams."""

import asyncio
import json
from pathlib import Path

import pytest
from conftest import collect

from bob_agent_sdk import (
    ACPError,
    AgentDefinition,
    AssistantMessage,
    BobAgentOptions,
    BobSDKClient,
    BobSDKError,
    HookMatcher,
    ModeDefinition,
    ResultMessage,
    ToolResultMessage,
    ToolUseBlock,
)
from bob_agent_sdk._hooks import HOOK_EVENTS, bridge_group


def tool_uses(messages: list) -> list[ToolUseBlock]:
    return [
        block
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ToolUseBlock)
    ]


# ------------------------------------------------------------ usage, limits


def test_result_reports_usage(cli: Path, tmp_path: Path) -> None:
    async def run() -> list[ResultMessage]:
        async with BobSDKClient(BobAgentOptions(cli_path=cli, cwd=tmp_path)) as client:
            results = []
            for _ in range(2):
                await client.query("echo")
                results.append([m async for m in client.receive_response()][-1])
            return results

    first, second = asyncio.run(run())
    assert (first.num_turns, first.cost, first.total_cost) == (1, 0.01, 0.01)
    assert (second.num_turns, second.cost, second.total_cost) == (1, 0.01, 0.02)
    assert first.model == "fake-model"
    assert first.usage == {"cost": 0.01, "contextTokens": 100}
    assert first.is_error is False


def test_resumed_session_reports_only_new_usage(cli: Path, tmp_path: Path) -> None:
    options = BobAgentOptions(cli_path=cli, cwd=tmp_path, resume="old-session")
    result = asyncio.run(collect("echo", options))[-1]
    assert (result.num_turns, result.cost, result.total_cost) == (1, 0.01, 0.51)


def test_max_turns_stops_the_turn(cli: Path, tmp_path: Path) -> None:
    options = BobAgentOptions(cli_path=cli, cwd=tmp_path, max_turns=2)
    result = asyncio.run(collect("work", options))[-1]
    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert result.num_turns == 2


def test_max_cost_stops_the_turn_and_blocks_the_next(cli: Path, tmp_path: Path) -> None:
    async def run() -> list[ResultMessage]:
        options = BobAgentOptions(cli_path=cli, cwd=tmp_path, max_cost=0.03)
        async with BobSDKClient(options) as client:
            results = []
            for _ in range(2):
                await client.query("work")
                results.append([m async for m in client.receive_response()][-1])
            return results

    first, second = asyncio.run(run())
    assert first.stop_reason == "max_cost"
    assert first.total_cost == 0.03
    # The budget is spent, so the SDK does not send the second prompt to Bob.
    assert (second.stop_reason, second.num_turns, second.cost) == ("max_cost", 0, 0.0)


def test_limits_need_a_bob_that_reports_usage(cli: Path, tmp_path: Path) -> None:
    options = BobAgentOptions(cli_path=cli, cwd=tmp_path, max_turns=2, env={"FAKE_BOB_OLD": "1"})
    with pytest.raises(BobSDKError, match="cannot report usage"):
        asyncio.run(collect("work", options))


# -------------------------------------------------------------------- hooks


def test_hooks_name_block_and_rewrite_tool_calls(cli: Path, tmp_path: Path) -> None:
    seen = []

    async def pre(event):
        seen.append(("pre", event["tool_use_id"], event["tool_name"]))
        if event["tool_use_id"] == "w1":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "not this one",
                }
            }
        if event["tool_use_id"] == "w2":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": {**event["tool_input"], "content": "CHANGED"},
                }
            }
        return None

    async def never(event):
        raise AssertionError("the matcher must filter this callback out")

    async def post(event):
        seen.append(("post", event["tool_use_id"], event["tool_response"]))

    async def stop(event):
        seen.append(("stop", event["last_assistant_message"]))

    options = BobAgentOptions(
        cli_path=cli,
        cwd=tmp_path,
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="^write_", hooks=[pre]),
                HookMatcher(matcher="^execute_command$", hooks=[never]),
            ],
            "PostToolUse": [HookMatcher(hooks=[post])],
            "Stop": [HookMatcher(hooks=[stop])],
        },
    )
    messages = asyncio.run(collect("work", options))

    assert [block.name for block in tool_uses(messages)] == ["write_file"] * 5
    results = [m for m in messages if isinstance(m, ToolResultMessage)]
    assert (results[0].status, results[0].output) == ("failed", "not this one")
    assert json.loads(results[1].output) == {"path": "2.txt", "content": "CHANGED"}
    assert json.loads(results[2].output) == {"path": "3.txt", "content": "hi"}
    assert ("pre", "w1", "write_file") in seen
    assert ("post", "w1", "ok") not in seen  # The denied call never ran.
    assert ("post", "w2", "ok") in seen
    assert seen[-1] == ("stop", "finished")


def test_hook_error_reaches_the_reader_and_bob_continues(cli: Path, tmp_path: Path) -> None:
    async def broken(event):
        raise RuntimeError("hook failed")

    options = BobAgentOptions(
        cli_path=cli, cwd=tmp_path, hooks={"PreToolUse": [HookMatcher(hooks=[broken])]}
    )
    with pytest.raises(RuntimeError, match="hook failed"):
        asyncio.run(collect("work", options))


def test_empty_hooks_still_give_tool_names(cli: Path, tmp_path: Path) -> None:
    messages = asyncio.run(collect("work", BobAgentOptions(cli_path=cli, cwd=tmp_path, hooks={})))
    assert tool_uses(messages)[0].name == "write_file"
    without = asyncio.run(collect("work", BobAgentOptions(cli_path=cli, cwd=tmp_path)))
    assert tool_uses(without)[0].name is None


def test_hook_bridge_is_removed_and_user_settings_survive(cli: Path, tmp_path: Path) -> None:
    settings = tmp_path / ".bob" / "settings.json"
    settings.parent.mkdir()
    user_hook = {"matcher": "^x$", "hooks": [{"type": "command", "command": "true"}]}
    original = {"session": {"maxTurns": 50}, "hooks": {"Stop": [user_hook]}}
    settings.write_text(json.dumps(original))
    during = {}

    async def capture(event):
        during.update(json.loads(settings.read_text()))

    options = BobAgentOptions(
        cli_path=cli, cwd=tmp_path, hooks={"PreToolUse": [HookMatcher(hooks=[capture])]}
    )
    asyncio.run(collect("work", options))

    assert during["session"] == {"maxTurns": 50}
    assert during["hooks"]["Stop"] == [user_hook, bridge_group()]
    assert set(during["hooks"]) == set(HOOK_EVENTS)
    assert settings.read_text() == json.dumps(original)  # The exact bytes, not reformatted.


def test_workspace_is_clean_after_the_session(cli: Path, tmp_path: Path) -> None:
    options = BobAgentOptions(
        cli_path=cli,
        cwd=tmp_path,
        hooks={},
        mode=ModeDefinition(role_definition="You review code."),
        agents={"helper": AgentDefinition(description="Helps", prompt="You help.")},
    )
    asyncio.run(collect("echo", options))
    assert list(tmp_path.iterdir()) == [cli]  # Only the test's own `bob` script is left.


def test_invalid_settings_file_is_not_touched(cli: Path, tmp_path: Path) -> None:
    settings = tmp_path / ".bob" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{ not json")
    with pytest.raises(BobSDKError, match="not valid JSON"):
        asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, cwd=tmp_path, hooks={})))
    assert settings.read_text() == "{ not json"


# ------------------------------------------------- modes, subagents, teams


def test_mode_definition_becomes_a_custom_mode(cli: Path, tmp_path: Path) -> None:
    mode = ModeDefinition(
        name="Reviewer",
        role_definition="You review code.",
        custom_instructions="Be brief.",
        groups=["read"],
    )
    result = asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, cwd=tmp_path, mode=mode)))[
        -1
    ]
    session = json.loads(result.result)["session"]
    (mode_file,) = session["mode_files"]
    (entry,) = mode_file["customModes"]
    assert entry == {
        "slug": session["mode"],
        "name": "Reviewer",
        "roleDefinition": "You review code.",
        "customInstructions": "Be brief.",
        "groups": ["read"],
    }
    assert entry["slug"].startswith("sdk-")


def test_agents_become_subagent_files(cli: Path, tmp_path: Path) -> None:
    agents = {
        "poet": AgentDefinition(
            description="Writes\na haiku",
            prompt="You write one haiku.",
            groups=["read"],
            deny_tools=["grep"],
            model_tier="standard",
            max_turns=3,
        )
    }
    options = BobAgentOptions(cli_path=cli, cwd=tmp_path, agents=agents)
    result = asyncio.run(collect("echo", options))[-1]
    assert json.loads(result.result)["session"]["agent_files"] == {
        "poet.md": (
            "---\nname: poet\ndescription: Writes a haiku\ngroups:\n  - read\n"
            "denyTools:\n  - grep\nmodelTier: standard\nmaxTurns: 3\n---\n"
            "You write one haiku.\n"
        )
    }


def test_existing_agent_file_is_not_overwritten(cli: Path, tmp_path: Path) -> None:
    existing = tmp_path / ".bob" / "agents" / "poet.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("mine")
    agents = {"poet": AgentDefinition(description="d", prompt="p")}
    with pytest.raises(BobSDKError, match="already exists"):
        asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, cwd=tmp_path, agents=agents)))
    assert existing.read_text() == "mine"


def test_skills_are_linked_for_the_session(cli: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\nDo it.")

    options = BobAgentOptions(cli_path=cli, cwd=workspace, skills=[skill])
    result = asyncio.run(collect("echo", options))[-1]
    assert json.loads(result.result)["session"]["skills"] == {
        "my-skill": "---\nname: my-skill\n---\nDo it."
    }
    assert list(workspace.iterdir()) == []
    assert (skill / "SKILL.md").exists()  # Cleanup removes the link, not the skill.

    bad_name = tmp_path / "Bad_Name"
    bad_name.mkdir()
    (bad_name / "SKILL.md").write_text("x")
    for bad, reason in ((tmp_path, "no SKILL.md"), (bad_name, "Invalid skill folder name")):
        options = BobAgentOptions(cli_path=cli, cwd=workspace, skills=[bad])
        with pytest.raises(BobSDKError, match=reason):
            asyncio.run(collect("echo", options))


def test_team_id(cli: Path, tmp_path: Path) -> None:
    options = BobAgentOptions(cli_path=cli, cwd=tmp_path, team_id="team-1")
    result = asyncio.run(collect("echo", options))[-1]
    assert json.loads(result.result)["session"]["team"] == "team-1"

    with pytest.raises(ACPError):
        asyncio.run(collect("echo", BobAgentOptions(cli_path=cli, cwd=tmp_path, team_id="nope")))

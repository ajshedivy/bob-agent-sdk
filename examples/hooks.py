"""Python callbacks for Bob's hook events.

Hooks see every tool call with its real tool name, also the calls that Bob
approves on its own. A hook can block a call, change its input, or add context
for the model.

    uv run --env-file .env examples/hooks.py
"""

import asyncio
import tempfile
from typing import Any

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    HookMatcher,
    TextBlock,
    ToolUseBlock,
    query,
)


async def block_rm(event: dict[str, Any]) -> dict[str, Any] | None:
    """PreToolUse: refuse shell commands that delete files."""
    command = event["tool_input"].get("command", "")
    if "rm " in command:
        print(f"  [hook] blocked: {command}")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Deleting files is not allowed in this workspace.",
            }
        }
    return None


async def shout(event: dict[str, Any]) -> dict[str, Any] | None:
    """PreToolUse: change the content of every file that Bob writes."""
    tool_input = event["tool_input"]
    print(f"  [hook] rewriting the content of {tool_input.get('path')}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "content": tool_input["content"].upper()},
        }
    }


async def audit(event: dict[str, Any]) -> None:
    """PostToolUse: record every tool that ran."""
    print(f"  [hook] {event['tool_name']} finished")


async def on_stop(event: dict[str, Any]) -> None:
    print("  [hook] Bob stopped")


async def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        options = BobAgentOptions(
            cwd=workspace,
            auto_approve=True,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="^execute_command$", hooks=[block_rm]),
                    HookMatcher(matcher="^write_file$", hooks=[shout]),
                ],
                "PostToolUse": [HookMatcher(hooks=[audit])],
                "Stop": [HookMatcher(hooks=[on_stop])],
            },
        )
        prompt = (
            "Create notes.txt that contains the words hello world. Read it back with "
            "`cat notes.txt` and tell me the exact content. Then delete it with `rm notes.txt`."
        )
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Bob: {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [tool call] {block.name}: {block.title}")


if __name__ == "__main__":
    asyncio.run(main())

"""Decide in Python which tool calls Bob may run.

Bob only asks for approval when its own settings do not already allow the
call. Read-only tools, and anything listed under `approval` in
~/.bob/settings/settings.json, run without calling the callback.

    uv run --env-file .env examples/tool_permission_callback.py
"""

import asyncio
import tempfile

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
    ToolPermissionContext,
    ToolResultMessage,
    ToolUseBlock,
    query,
)


async def can_use_tool(tool: ToolUseBlock, context: ToolPermissionContext) -> PermissionResult:
    print(f"  [approval request] kind={tool.kind} title={tool.title!r}")
    if tool.kind == "execute" and "rm " in tool.input.get("command", ""):
        print("  [denied]")
        return PermissionResultDeny()
    print("  [allowed]")
    return PermissionResultAllow()


async def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        options = BobAgentOptions(cwd=workspace, can_use_tool=can_use_tool)
        prompt = (
            "Create a file notes.txt that contains the word hello. "
            "Then delete it with the shell command `rm notes.txt`. "
            "Tell me what happened at each step."
        )
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Bob: {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [tool call] {block.kind}: {block.title}")
            elif isinstance(message, ToolResultMessage):
                print(f"  [tool {message.status}]")


if __name__ == "__main__":
    asyncio.run(main())

"""One prompt, one answer.

uv run examples/quick_start.py
"""

import asyncio

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultMessage,
    ToolUseBlock,
    query,
)


async def basic() -> None:
    print("--- basic")
    async for message in query(prompt="What is 2 + 2? Answer in one short sentence."):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Bob: {block.text}")


async def with_options_and_tools() -> None:
    print("--- with options and tools")
    options = BobAgentOptions(
        mode="ask",  # Read-only mode: Bob answers questions and does not edit files.
        cwd=".",
    )
    async for message in query(
        prompt="List the files in this directory, then say in one sentence what this project is.",
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Bob: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool: {block.kind}] {block.title}")
        elif isinstance(message, ToolResultMessage):
            print(f"  [tool {message.status}]")
        elif isinstance(message, ResultMessage):
            print(
                f"Done: {message.stop_reason}, {message.num_tool_calls} tool calls, "
                f"{message.duration_ms} ms, session {message.session_id}"
            )


async def main() -> None:
    await basic()
    await with_options_and_tools()


if __name__ == "__main__":
    asyncio.run(main())

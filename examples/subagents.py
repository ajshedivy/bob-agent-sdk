"""Define subagents in Python. Bob starts them with its `spawn_subagent` tool.

uv run examples/subagents.py
"""

import asyncio
import tempfile

from bob_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    BobAgentOptions,
    TextBlock,
    ToolUseBlock,
    query,
)


async def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        options = BobAgentOptions(
            cwd=workspace,
            auto_approve=True,
            agents={
                "haiku-writer": AgentDefinition(
                    description="Writes one haiku about a given topic",
                    prompt=(
                        "You write exactly one haiku about the topic you are given. "
                        "Return only the three lines of the haiku."
                    ),
                    groups=["read"],
                    max_turns=3,
                ),
                "critic": AgentDefinition(
                    description="Rates a short text from 1 to 10 with one reason",
                    prompt="You rate the text you are given from 1 to 10. Give one short reason.",
                    groups=["read"],
                    max_turns=3,
                ),
            },
        )
        prompt = (
            "Ask the haiku-writer subagent for a haiku about mainframes. "
            "Then ask the critic subagent to rate it. Show me both results."
        )
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Bob: {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [tool call] {block.title} {block.input}")


if __name__ == "__main__":
    asyncio.run(main())

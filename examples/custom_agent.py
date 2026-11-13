"""Give the agent its own role, instructions and tool groups.

The SDK creates a custom Bob mode for the session. Its role and instructions
go into Bob's system prompt. Tool groups that are left out are not available
to the agent.

    uv run --env-file .env examples/custom_agent.py
"""

import asyncio

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    ModeDefinition,
    TextBlock,
    ToolUseBlock,
    query,
)

reviewer = ModeDefinition(
    name="Security Reviewer",
    role_definition=(
        "You are a security reviewer. You read code and report risks. You never change code."
    ),
    custom_instructions=(
        "Report at most three findings. For each one give the file, the risk "
        "and a one-line fix. Start your answer with the line 'SECURITY REVIEW'."
    ),
    groups=["read"],  # No edit and no execute: the agent cannot change anything.
)


async def main() -> None:
    options = BobAgentOptions(mode=reviewer, cwd=".")
    prompt = "Review src/bob_agent_sdk/_http.py. Then fix the worst problem you find."
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Bob: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool call] {block.kind}: {block.title}")


if __name__ == "__main__":
    asyncio.run(main())

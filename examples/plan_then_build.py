"""Start in plan mode, review the plan, then switch to agent mode to build it.

uv run examples/plan_then_build.py
"""

import asyncio
import tempfile

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    BobSDKClient,
    PlanMessage,
    TextBlock,
    ToolUseBlock,
)


async def show(client: BobSDKClient) -> None:
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Bob: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool call] {block.kind}: {block.title}")
        elif isinstance(message, PlanMessage):
            for entry in message.entries:
                print(f"  [{entry.status}] {entry.content}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        options = BobAgentOptions(cwd=workspace, mode="plan", auto_approve=True)
        async with BobSDKClient(options) as client:
            await client.query(
                "Plan a Python script fizzbuzz.py that prints FizzBuzz for 1 to 15. "
                "Keep the plan to three steps. Do not ask me questions."
            )
            await show(client)

            await client.set_mode("agent")
            await client.query("The plan is approved. Build it, then run the script.")
            await show(client)


if __name__ == "__main__":
    asyncio.run(main())

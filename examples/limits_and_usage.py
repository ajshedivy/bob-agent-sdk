"""Limit the turns and the cost of a session, and read what it spent.

uv run examples/limits_and_usage.py
"""

import asyncio
import tempfile

from bob_agent_sdk import BobAgentOptions, BobSDKClient, ResultMessage, ToolUseBlock


async def run(client: BobSDKClient, prompt: str) -> ResultMessage:
    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            return message
        for block in getattr(message, "content", []):
            if isinstance(block, ToolUseBlock):
                print(f"  [tool call] {block.title}")
    raise RuntimeError("The session closed before the turn ended")


def report(result: ResultMessage) -> None:
    print(
        f"  stop_reason={result.stop_reason} is_error={result.is_error} "
        f"turns={result.num_turns} cost={result.cost} total_cost={result.total_cost} "
        f"model={result.model}"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        print("--- usage of a normal turn")
        async with BobSDKClient(BobAgentOptions(cwd=workspace, auto_approve=True)) as client:
            report(await run(client, "What is 17 * 23? Answer with the number only."))

        print("--- max_turns=2 on a task that needs more")
        options = BobAgentOptions(cwd=workspace, auto_approve=True, max_turns=2)
        async with BobSDKClient(options) as client:
            report(
                await run(
                    client,
                    "Create the files one.txt, two.txt, three.txt, four.txt and five.txt, "
                    "one at a time. Check each file with `cat` before you create the next.",
                )
            )

        print("--- max_cost=0.03 over two turns")
        options = BobAgentOptions(cwd=workspace, auto_approve=True, max_cost=0.03)
        async with BobSDKClient(options) as client:
            report(await run(client, "List the files here, then read each one."))
            # The budget is used up, so the SDK does not start this turn.
            report(await run(client, "Now count the files."))


if __name__ == "__main__":
    asyncio.run(main())

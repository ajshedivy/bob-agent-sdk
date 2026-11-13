"""A conversation with several turns, an interrupt, and a resumed session.

uv run examples/multi_turn.py
"""

import asyncio

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    BobSDKClient,
    ResultMessage,
    TextBlock,
)


async def print_response(client: BobSDKClient, quiet: bool = False) -> ResultMessage | None:
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage) and not quiet:
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Bob: {block.text}")
        elif isinstance(message, ResultMessage):
            return message
    return None


async def main() -> None:
    options = BobAgentOptions(mode="ask")

    async with BobSDKClient(options) as client:
        print("User: What is the capital of France?")
        await client.query("What is the capital of France? Answer in one short sentence.")
        await print_response(client)

        # The same session: Bob remembers the first turn.
        print("User: What is the population of that city?")
        await client.query("What is the population of that city? Answer in one short sentence.")
        await print_response(client)

        # Interrupt a long turn.
        print("User: Count from 1 to 500. (interrupted after 3 seconds)")
        await client.query("Count from 1 to 500, one number per line.")
        await asyncio.sleep(3)
        await client.interrupt()
        result = await print_response(client, quiet=True)
        print(f"[stop reason: {result.stop_reason if result else None}]")
        session_id = client.session_id

    # Bob saved the session. A new process can continue it.
    async with BobSDKClient(BobAgentOptions(resume=session_id)) as client:
        print("User: Which country did I ask about first?")
        await client.query("Which country did I ask about first? Answer in one word.")
        await print_response(client)


if __name__ == "__main__":
    asyncio.run(main())

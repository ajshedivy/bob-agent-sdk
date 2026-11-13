"""`query()`: one prompt, one answer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .client import BobSDKClient
from .types import BobAgentOptions, Message


async def query(
    *, prompt: str | list[dict[str, Any]], options: BobAgentOptions | None = None
) -> AsyncIterator[Message]:
    """Run a single prompt in a new Bob session and yield the messages.

        async for message in query(prompt="What does this project do?"):
            print(message)

    Use `BobSDKClient` instead for follow-up prompts, interrupts or mode changes.
    """
    async with BobSDKClient(options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            yield message

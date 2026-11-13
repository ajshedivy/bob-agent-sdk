"""Give Bob tools that are plain Python functions in this process.

uv run examples/custom_tools.py
"""

import asyncio
from typing import Any

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    TextBlock,
    ToolResultMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

ORDERS = {"A-1001": "shipped", "A-1002": "waiting for payment"}


@tool("get_order_status", "Look up the status of a customer order by its ID", {"order_id": str})
async def get_order_status(args: dict[str, Any]) -> str:
    return ORDERS.get(args["order_id"], "unknown order")


@tool("multiply", "Multiply two numbers", {"a": float, "b": float})
async def multiply(args: dict[str, Any]) -> str:
    return str(args["a"] * args["b"])


async def main() -> None:
    options = BobAgentOptions(
        mcp_servers={
            "shop": create_sdk_mcp_server("shop", tools=[get_order_status, multiply]),
        },
        # Without this (or a can_use_tool callback), tool calls that need
        # approval are rejected.
        auto_approve=True,
    )
    prompt = (
        "Use your shop tools. What is the status of order A-1002? "
        "Also, what is 12.5 multiplied by 8?"
    )
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"Bob: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool call] {block.title} {block.input}")
        elif isinstance(message, ToolResultMessage):
            print(f"  [tool result] {message.output}")


if __name__ == "__main__":
    asyncio.run(main())

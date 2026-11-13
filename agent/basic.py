"""A Bob agent through Agno's standard interface.

cd agent
uv run basic.py
"""

import tempfile

from bob_agent import BobAgent

from bob_agent_sdk import ModeDefinition, create_sdk_mcp_server, tool

ORDERS = {"A-1001": "shipped", "A-1002": "waiting for payment"}


@tool("get_order_status", "Look up the status of a customer order by its ID", {"order_id": str})
async def get_order_status(args):
    return ORDERS.get(args["order_id"], "unknown order")


support_agent = BobAgent(
    name="Order Support",
    description="Answers questions about customer orders",
    mode=ModeDefinition(
        name="Order Support",
        role_definition="You are an order support agent. You answer in one or two sentences.",
        groups=["read", "mcp"],  # No edit and no execute.
    ),
    mcp_servers={"shop": create_sdk_mcp_server("shop", tools=[get_order_status])},
    auto_approve=True,
    max_turns=10,
    cwd=tempfile.mkdtemp(),
)

if __name__ == "__main__":
    # Streaming, with tool calls shown as they happen.
    support_agent.print_response("What is the status of order A-1002?", stream=True)

    # The same Agno session continues the same Bob session, so Bob remembers.
    session = "order-chat"
    support_agent.print_response("Check order A-1001.", session_id=session)
    support_agent.print_response("Which order did I just ask about?", session_id=session)

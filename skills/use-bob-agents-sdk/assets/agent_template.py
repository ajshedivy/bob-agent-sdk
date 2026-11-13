"""<One line: what this agent does.>

Run:
    uv run --env-file .env agent.py          # with uv
    .venv/bin/python agent.py                # without uv (BOB_API_KEY must be exported)
"""

import asyncio
import sys

from bob_agent_sdk import (
    AssistantMessage,
    AuthenticationError,
    BobAgentOptions,
    BobSDKError,
    ResultMessage,
    TextBlock,
    ToolResultMessage,
    ToolUseBlock,
    query,
)

OPTIONS = BobAgentOptions(
    cwd=".",  # The folder the agent works in.
    mode="agent",  # "agent", "plan", "ask", or a ModeDefinition for a custom role.
    auto_approve=True,  # Without this or can_use_tool, calls that need approval are rejected.
    max_turns=20,
)


async def run(prompt: str) -> ResultMessage | None:
    async for message in query(prompt=prompt, options=OPTIONS):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(f"  [tool] {block.name or block.kind}: {block.title}")
        elif isinstance(message, ToolResultMessage) and message.is_error:
            print(f"  [tool failed] {message.output}")
        elif isinstance(message, ResultMessage):
            return message
    return None


def main() -> int:
    prompt = " ".join(sys.argv[1:]) or "Summarize this project in three sentences."
    try:
        result = asyncio.run(run(prompt))
    except AuthenticationError as error:
        print(f"Login problem: {error}", file=sys.stderr)
        return 2
    except BobSDKError as error:
        print(f"Bob failed: {error}", file=sys.stderr)
        return 1
    if result is None:
        print("Stopped early: no result", file=sys.stderr)
        return 1
    # Print the usage first: a run that hit a limit is the one you want numbers for.
    print(f"\n[{result.num_turns} model requests, cost {result.cost} Bobcoins]")
    if result.is_error:
        print(f"Stopped early: {result.stop_reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Runs one SDK session with every kind of workspace file. Used by test_workspace.py.

    session_script.py <cli> <workspace> <skill folder> <hold|run> [isolation]

Prints `ready` once the session is connected. With `hold` it then waits for a
line on stdin. With `run` it runs one prompt and prints how many hooks fired.
"""

import asyncio
import sys

from bob_agent_sdk import (
    AgentDefinition,
    BobAgentOptions,
    BobSDKClient,
    HookMatcher,
    ModeDefinition,
)


async def main() -> None:
    cli, workspace, skill, action = sys.argv[1:5]
    fired = []

    async def count(event):
        fired.append(event["tool_use_id"])

    options = BobAgentOptions(
        cli_path=cli,
        cwd=workspace,
        mode=ModeDefinition(role_definition="You review code."),
        agents={"helper": AgentDefinition(description="Helps", prompt="You help.")},
        skills=[skill],
        hooks={"PreToolUse": [HookMatcher(hooks=[count])]},
        isolation=sys.argv[5] if len(sys.argv) > 5 else "shared",
    )
    async with BobSDKClient(options) as client:
        print("ready", flush=True)
        if action == "hold":
            await asyncio.to_thread(sys.stdin.readline)
        else:
            await client.query("work")
            async for _ in client.receive_response():
                pass
            print(f"hooks={len(fired)}", flush=True)


asyncio.run(main())

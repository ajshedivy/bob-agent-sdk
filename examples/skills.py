"""Attach skill folders to a session.

A skill is a folder with a `SKILL.md` file, and any files that the skill
refers to. Bob reads the skill's description, and loads the full instructions
when a request matches it.

    uv run --env-file .env examples/skills.py
"""

import asyncio
import tempfile
from pathlib import Path

from bob_agent_sdk import (
    AssistantMessage,
    BobAgentOptions,
    TextBlock,
    ToolUseBlock,
    query,
)

SKILLS = Path(__file__).parent / "skills"


async def main() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        options = BobAgentOptions(
            cwd=workspace,
            skills=[SKILLS / "release-notes"],
            auto_approve=True,
        )
        prompt = (
            "Write the release notes for version 1.5.0. We added a skills option, "
            "fixed a hang in the turn task, and made workspace cleanup safe for "
            "several sessions."
        )
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"Bob: {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"  [tool call] {block.title}")


if __name__ == "__main__":
    asyncio.run(main())

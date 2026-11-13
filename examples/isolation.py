"""Keep SDK sessions apart: `isolation="enforced"` and `isolation="worktree"`.

uv run examples/isolation.py
"""

import asyncio
import subprocess
import tempfile
from pathlib import Path

from bob_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    BobAgentOptions,
    BobSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultMessage,
    ToolUseBlock,
    remove_worktree,
)

REVIEWER = AgentDefinition(
    description="Returns the review password",
    prompt="Reply with exactly: REVIEW-OK",
    groups=["read"],
)
POET = AgentDefinition(
    description="Writes one haiku",
    prompt="Write exactly one haiku about the topic you are given.",
    groups=["read"],
)


async def run(client: BobSDKClient, prompt: str) -> ResultMessage | None:
    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"    Bob: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"    [tool call] {block.name}: {block.input}")
        elif isinstance(message, ToolResultMessage):
            print(f"    [tool {message.status}] {(message.output or '')[:90]}")
        elif isinstance(message, ResultMessage):
            return message
    return None


async def enforced(workspace: Path) -> None:
    """Two sessions share one folder. Each can only use its own subagent."""
    print("--- enforced: two sessions in one workspace")
    base = dict(cwd=workspace, auto_approve=True, isolation="enforced")
    first = BobSDKClient(BobAgentOptions(**base, agents={"reviewer": REVIEWER}))
    second = BobSDKClient(BobAgentOptions(**base, agents={"poet": POET}))
    async with first, second:
        print("  The second session asks for the first session's subagent:")
        await run(
            second,
            "Call the spawn_subagent tool with the name 'reviewer' and tell me what happened. "
            "Do not try anything else.",
        )
        print("  The second session uses its own subagent:")
        await run(second, "Ask the poet subagent for a haiku about locks. Show it.")


async def worktree(repo: Path) -> None:
    """The session works on a branch of its own. The checkout is not touched."""
    print("--- worktree: a session in a working copy of its own")
    options = BobAgentOptions(cwd=repo, auto_approve=True, isolation="worktree")
    client = BobSDKClient(options)
    async with client:
        assert client.worktree is not None
        print(f"  worktree: {client.worktree.path}")
        print(f"  branch:   {client.worktree.branch}")
        await run(client, "Create notes.txt that contains the word hello. Do nothing else.")

    kept = client.worktree
    print(f"  removed on close: {kept.removed}  (False, because the session left a file)")
    print(f"  notes.txt in the checkout: {(repo / 'notes.txt').exists()}")
    print(f"  notes.txt in the worktree: {(kept.path / 'notes.txt').exists()}")
    # Take the work with `git merge`, or discard it:
    remove_worktree(kept, force=True)
    print(f"  worktree after remove_worktree: {kept.path.exists()}")


def make_repo(path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    (path / "README.md").write_text("# Demo\n")
    git("init", "-b", "main")
    git("add", ".")
    git("-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-m", "first")


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        await enforced(Path(folder))
    with tempfile.TemporaryDirectory() as folder:
        make_repo(Path(folder))
        await worktree(Path(folder))


if __name__ == "__main__":
    asyncio.run(main())

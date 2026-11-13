"""`isolation="enforced"` and `isolation="worktree"`."""

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import collect

from bob_agent_sdk import (
    AgentDefinition,
    BobAgentOptions,
    BobSDKClient,
    BobSDKError,
    ModeDefinition,
    SystemMessage,
    ToolResultMessage,
    _worktree,
    cleanup_workspace,
    remove_worktree,
)

SCRIPT = Path(__file__).parent / "session_script.py"
HELPER = AgentDefinition(description="Helps", prompt="You help.")


async def ask(client: BobSDKClient, prompt: str) -> ToolResultMessage:
    """Send a prompt that makes the stand-in Bob call one tool. Return its result."""
    await client.query(prompt)
    messages = [m async for m in client.receive_response()]
    (result,) = [m for m in messages if isinstance(m, ToolResultMessage)]
    return result


def skill_folder(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nDo it.")
    return path


# ---------------------------------------------------------------- enforced


def test_enforced_refuses_what_only_another_session_defined(cli: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".bob" / "agents").mkdir(parents=True)
    (workspace / ".bob" / "agents" / "project-agent.md").write_text("the project's own")
    base = dict(cli_path=cli, cwd=workspace, isolation="enforced")

    async def run() -> None:
        first = BobSDKClient(
            BobAgentOptions(
                **base,
                agents={"reviewer": HELPER, "shared": HELPER},
                skills=[skill_folder(tmp_path, "review-skill")],
                mode=ModeDefinition(role_definition="You review.", slug="reviewer-mode"),
            )
        )
        second = BobSDKClient(BobAgentOptions(**base, agents={"poet": HELPER, "shared": HELPER}))
        await first.connect()
        await second.connect()

        for prompt, reason in (
            ("spawn_subagent:reviewer", "Subagent 'reviewer' belongs to another SDK session"),
            ("use_skill:review-skill", "Skill 'review-skill' belongs to another SDK session"),
            ("switch_mode:reviewer-mode", "Mode 'reviewer-mode' belongs to another SDK session"),
        ):
            refused = await ask(second, prompt)
            assert refused.status == "failed"
            assert reason in refused.output

        for prompt in (
            "spawn_subagent:poet",  # Its own.
            "spawn_subagent:shared",  # Both sessions define it the same way.
            "spawn_subagent:project-agent",  # The project's own file.
            "spawn_subagent:explore",  # Built into Bob.
            "switch_mode:plan",
        ):
            assert (await ask(second, prompt)).status == "completed"

        # The first session can use its own, and not the second session's.
        assert (await ask(first, "spawn_subagent:reviewer")).status == "completed"
        assert (await ask(first, "spawn_subagent:poet")).status == "failed"

        # Once the first session is gone, nothing of it is left to refuse.
        await first.disconnect()
        third = BobSDKClient(BobAgentOptions(**base, agents={"reviewer": HELPER}))
        await third.connect()
        assert (await ask(second, "spawn_subagent:reviewer")).status == "failed"
        await third.disconnect()
        await second.disconnect()

    asyncio.run(run())
    assert sorted(p.name for p in (workspace / ".bob").rglob("*")) == ["agents", "project-agent.md"]


def test_shared_isolation_does_not_refuse(cli: Path, tmp_path: Path) -> None:
    async def run() -> None:
        base = dict(cli_path=cli, cwd=tmp_path, hooks={})
        first = BobSDKClient(BobAgentOptions(**base, agents={"reviewer": HELPER}))
        second = BobSDKClient(BobAgentOptions(**base, agents={"poet": HELPER}))
        await first.connect()
        await second.connect()
        assert (await ask(second, "spawn_subagent:reviewer")).status == "completed"
        await second.disconnect()
        await first.disconnect()

    asyncio.run(run())


def test_enforced_mode_lists_only_its_own_subagents(cli: Path, tmp_path: Path) -> None:
    (tmp_path / ".bob" / "agents").mkdir(parents=True)
    (tmp_path / ".bob" / "agents" / "project-agent.md").write_text("the project's own")
    base = dict(cli_path=cli, cwd=tmp_path, isolation="enforced")

    async def run() -> list:
        other = BobSDKClient(BobAgentOptions(**base, agents={"reviewer": HELPER}))
        await other.connect()
        mode = ModeDefinition(role_definition="You write.")
        try:
            return await collect(
                "echo", BobAgentOptions(**base, mode=mode, agents={"poet": HELPER})
            )
        finally:
            await other.disconnect()

    session = json.loads(asyncio.run(run())[-1].result)["session"]
    (mode_file,) = session["mode_files"]
    # Bob hides every subagent that is not on this list from the model.
    assert mode_file["customModes"][0]["allowedSubagents"] == [
        "general", "explore", "poet", "project-agent",
    ]  # fmt: skip


def test_enforced_keeps_an_explicit_subagent_list(cli: Path, tmp_path: Path) -> None:
    mode = ModeDefinition(role_definition="You write.", allowed_subagents=["poet"])
    options = BobAgentOptions(cli_path=cli, cwd=tmp_path, isolation="enforced", mode=mode)
    session = json.loads(asyncio.run(collect("echo", options))[-1].result)["session"]
    assert session["mode_files"][0]["customModes"][0]["allowedSubagents"] == ["poet"]


# ---------------------------------------------------------------- worktree


def git(directory: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repository with one commit. Worktrees go to a temporary folder."""
    monkeypatch.setattr(_worktree, "WORKTREES_DIR", tmp_path / "worktrees")
    path = tmp_path / "repo"
    (path / "src").mkdir(parents=True)
    (path / "src" / "app.py").write_text("print('hi')\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "first")
    return path


def worktree_options(cli: Path, cwd: Path, **more) -> BobAgentOptions:
    return BobAgentOptions(cli_path=cli, cwd=cwd, isolation="worktree", **more)


def test_worktree_without_work_is_removed(cli: Path, repo: Path) -> None:
    options = worktree_options(cli, repo, agents={"helper": HELPER}, hooks={})
    messages = asyncio.run(collect("echo", options))

    init = next(m for m in messages if isinstance(m, SystemMessage) and m.subtype == "init")
    worktree = Path(init.data["worktree"]["path"])
    session = json.loads(messages[-1].result)["session"]
    assert Path(session["cwd"]) == worktree.resolve()
    assert "helper.md" in session["agent_files"]  # Session files are in the worktree.

    assert not worktree.exists()
    assert git(repo, "branch", "--list", "bob/*") == ""
    assert git(repo, "status", "--porcelain") == ""  # Nothing was written to the checkout.
    assert not (repo / ".bob").exists()


def test_worktree_with_work_is_kept(cli: Path, repo: Path) -> None:
    async def run() -> BobSDKClient:
        client = BobSDKClient(worktree_options(cli, repo / "src"))
        async with client:
            assert client.worktree is not None
            await client.query("touch")
            async for _ in client.receive_response():
                pass
        return client

    worktree = asyncio.run(run()).worktree
    assert worktree is not None and worktree.removed is False
    # `cwd` was the `src` folder, so the session ran in the worktree's `src` folder.
    assert (worktree.path / "src" / "made.txt").read_text() == "made by the agent"
    assert not (repo / "src" / "made.txt").exists()
    assert git(repo, "branch", "--list", "bob/*", "--format=%(refname:short)") == worktree.branch

    with pytest.raises(BobSDKError, match="uncommitted changes"):
        remove_worktree(worktree)
    assert worktree.path.exists()

    # Once the work is on another branch, the worktree can go.
    git(worktree.path, "add", ".")
    git(worktree.path, "commit", "-q", "-m", "agent work")
    with pytest.raises(BobSDKError, match="on no other branch"):
        remove_worktree(worktree.path)
    git(repo, "merge", "-q", worktree.branch)
    remove_worktree(worktree.path)
    assert not worktree.path.exists()
    assert git(repo, "branch", "--list", "bob/*") == ""
    assert (repo / "src" / "made.txt").exists()


def test_remove_worktree_with_force_discards_work(cli: Path, repo: Path) -> None:
    async def run() -> BobSDKClient:
        client = BobSDKClient(worktree_options(cli, repo))
        async with client:
            await client.query("touch")
            async for _ in client.receive_response():
                pass
        return client

    worktree = asyncio.run(run()).worktree
    assert worktree is not None
    remove_worktree(worktree, force=True)
    assert not worktree.path.exists()
    assert git(repo, "branch", "--list", "bob/*") == ""


def test_worktree_needs_a_git_repository(cli: Path, tmp_path: Path) -> None:
    with pytest.raises(BobSDKError, match="needs a git repository"):
        asyncio.run(collect("echo", worktree_options(cli, tmp_path)))


def test_worktree_cannot_be_combined_with_resume(cli: Path, repo: Path) -> None:
    with pytest.raises(BobSDKError, match="Pass the worktree path as cwd"):
        asyncio.run(collect("echo", worktree_options(cli, repo, resume="old-session")))
    assert git(repo, "worktree", "list").count("\n") == 0  # No worktree was created.


def test_two_worktree_sessions_do_not_see_each_other(cli: Path, repo: Path) -> None:
    async def run() -> tuple[dict, dict]:
        first = BobSDKClient(worktree_options(cli, repo, agents={"reviewer": HELPER}))
        second = BobSDKClient(worktree_options(cli, repo, agents={"poet": HELPER}))
        async with first, second:
            results = []
            for client in (first, second):
                await client.query("echo")
                result = [m async for m in client.receive_response()][-1]
                results.append(json.loads(result.result)["session"])
            return results[0], results[1]

    first, second = asyncio.run(run())
    assert list(first["agent_files"]) == ["reviewer.md"]
    assert list(second["agent_files"]) == ["poet.md"]
    assert first["cwd"] != second["cwd"]


def test_worktree_of_a_killed_session_is_removed_by_the_next_one(
    cli: Path, repo: Path, tmp_path: Path
) -> None:
    worktrees = tmp_path / "worktrees"
    skill = skill_folder(tmp_path, "my-skill")
    code = (
        "import sys, runpy; from pathlib import Path; import bob_agent_sdk._worktree as w; "
        f"w.WORKTREES_DIR = Path({str(worktrees)!r}); "
        f"sys.argv = [{str(SCRIPT)!r}, *sys.argv[1:]]; "
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    victim = subprocess.Popen(
        [sys.executable, "-c", code, str(cli), str(repo), str(skill), "hold", "worktree"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert victim.stdout is not None and victim.stdout.readline().strip() == "ready"
    victim.send_signal(signal.SIGKILL)
    victim.wait()
    (left_behind,) = list(worktrees.iterdir())
    # Its session files are still there, so the worktree is not empty of changes yet.
    assert (left_behind / ".bob" / "agents" / "helper.md").exists()

    asyncio.run(collect("echo", worktree_options(cli, repo)))
    assert list(worktrees.iterdir()) == [left_behind]  # Kept: it has untracked files.

    # After the dead session's files are cleaned up, the next session removes it.
    cleanup_workspace(left_behind)
    asyncio.run(collect("echo", worktree_options(cli, repo)))
    assert list(worktrees.iterdir()) == []
    assert git(repo, "branch", "--list", "bob/*") == ""

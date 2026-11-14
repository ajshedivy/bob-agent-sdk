"""Several SDK sessions in one workspace, and sessions that die without cleanup."""

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from bob_agent_sdk import (
    AgentDefinition,
    BobAgentOptions,
    BobSDKClient,
    BobSDKError,
    HookMatcher,
    ModeDefinition,
    cleanup_workspace,
)
from bob_agent_sdk._hooks import bridge_group
from bob_agent_sdk._workspace import _sha, state_dir

SCRIPT = Path(__file__).parent / "session_script.py"
HELPER = AgentDefinition(description="Helps", prompt="You help.")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    path = tmp_path / "my-skill"
    path.mkdir()
    (path / "SKILL.md").write_text("---\nname: my-skill\n---\nDo it.")
    return path


def files(workspace: Path) -> list[str]:
    return sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))


def start(cli: Path, workspace: Path, skill: Path, action: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), str(cli), str(workspace), str(skill), action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    return process


def wait_ready(process: subprocess.Popen) -> None:
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"


def test_two_sessions_share_files_until_the_last_one_closes(
    cli: Path, workspace: Path, skill: Path
) -> None:
    fired = []

    async def count(event):
        fired.append(event["tool_use_id"])

    def options() -> BobAgentOptions:
        return BobAgentOptions(
            cli_path=cli,
            cwd=workspace,
            mode=ModeDefinition(role_definition="You review code."),
            agents={"helper": HELPER},
            skills=[skill],
            hooks={"PreToolUse": [HookMatcher(hooks=[count])]},
        )

    async def run() -> None:
        first = BobSDKClient(options())
        second = BobSDKClient(options())
        await first.connect()
        await second.connect()

        modes = list((workspace / ".bob" / "plugins").iterdir())
        assert len(modes) == 2  # One mode folder per session.
        settings = json.loads((workspace / ".bob" / "settings.json").read_text())
        assert settings["hooks"]["PreToolUse"] == [bridge_group()]  # One entry for both.

        await first.disconnect()

        # The second session still has its subagent, skill and hooks.
        assert (workspace / ".bob" / "agents" / "helper.md").exists()
        assert (workspace / ".bob" / "skills" / "my-skill" / "SKILL.md").exists()
        assert len(list((workspace / ".bob" / "plugins").iterdir())) == 1
        await second.query("work")
        async for _ in second.receive_response():
            pass
        assert len(fired) == 5

        await second.disconnect()

    asyncio.run(run())
    assert files(workspace) == []


def test_conflicting_definitions_are_refused(cli: Path, workspace: Path, skill: Path) -> None:
    other_skill = skill.parent / "elsewhere" / "my-skill"
    other_skill.mkdir(parents=True)
    (other_skill / "SKILL.md").write_text("different")

    async def run() -> None:
        base = dict(cli_path=cli, cwd=workspace)
        mode = ModeDefinition(role_definition="You review code.", slug="reviewer")
        first = BobSDKClient(
            BobAgentOptions(**base, agents={"helper": HELPER}, skills=[skill], mode=mode)
        )
        await first.connect()
        before = files(workspace)

        conflicts = [
            {"agents": {"helper": AgentDefinition(description="Other", prompt="Other.")}},
            {"skills": [other_skill]},
            {"mode": ModeDefinition(role_definition="You write code.", slug="reviewer")},
        ]
        for conflict in conflicts:
            with pytest.raises(BobSDKError, match="Another SDK session"):
                await BobSDKClient(BobAgentOptions(**base, **conflict)).connect()
            assert files(workspace) == before  # A refused session leaves nothing behind.

        # The same definitions are fine.
        same = BobSDKClient(
            BobAgentOptions(**base, agents={"helper": HELPER}, skills=[skill], mode=mode)
        )
        await same.connect()
        await same.disconnect()
        await first.disconnect()

    asyncio.run(run())
    assert files(workspace) == []


def test_files_of_a_killed_session_are_removed_by_the_next_one(
    cli: Path, workspace: Path, skill: Path
) -> None:
    survivor = start(cli, workspace, skill, "hold")
    wait_ready(survivor)
    victim = start(cli, workspace, skill, "hold")
    wait_ready(victim)
    assert len(list((workspace / ".bob" / "plugins").iterdir())) == 2

    victim.send_signal(signal.SIGKILL)
    victim.wait()
    # Nothing has cleaned up yet: the victim's mode folder is still there.
    assert len(list((workspace / ".bob" / "plugins").iterdir())) == 2

    cleanup_workspace(workspace)
    # The victim's own files are gone. What the survivor shares with it stays.
    assert len(list((workspace / ".bob" / "plugins").iterdir())) == 1
    assert (workspace / ".bob" / "agents" / "helper.md").exists()
    assert (workspace / ".bob" / "skills" / "my-skill").is_symlink()
    assert "hooks" in json.loads((workspace / ".bob" / "settings.json").read_text())

    survivor.send_signal(signal.SIGKILL)
    survivor.wait()
    assert files(workspace) != []

    # A new session in the workspace cleans up, then works as usual.
    finisher = start(cli, workspace, skill, "run")
    assert finisher.communicate(timeout=60)[0].split() == ["ready", "hooks=5"]
    assert files(workspace) == []


def test_cleanup_keeps_what_the_user_changed(cli: Path, workspace: Path, skill: Path) -> None:
    settings = workspace / ".bob" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"session": {"maxTurns": 50}}))

    victim = start(cli, workspace, skill, "hold")
    wait_ready(victim)
    victim.send_signal(signal.SIGKILL)
    victim.wait()

    # The user edits the leftover subagent file and adds a file next to it.
    agent = workspace / ".bob" / "agents" / "helper.md"
    agent.write_text("my own version")
    (workspace / ".bob" / "agents" / "mine.md").write_text("mine")

    cleanup_workspace(workspace)
    assert files(workspace) == [
        ".bob",
        ".bob/agents",
        ".bob/agents/helper.md",
        ".bob/agents/mine.md",
        ".bob/settings.json",
    ]
    assert agent.read_text() == "my own version"
    assert json.loads(settings.read_text()) == {"session": {"maxTurns": 50}}


def test_many_sessions_starting_at_once(cli: Path, workspace: Path, skill: Path) -> None:
    processes = [start(cli, workspace, skill, "run") for _ in range(6)]
    outputs = [process.communicate(timeout=120)[0].split() for process in processes]
    assert outputs == [["ready", "hooks=5"]] * 6
    assert files(workspace) == []


# ------------------------------------------------- state outside the workspace


def test_the_sdks_bookkeeping_is_outside_the_workspace(
    cli: Path, workspace: Path, skill: Path
) -> None:
    holder = start(cli, workspace, skill, "hold")
    wait_ready(holder)
    assert not (workspace / ".bob" / ".bob-agent-sdk").exists()
    assert len(list((state_dir(workspace) / "sessions").glob("*.json"))) == 1
    holder.communicate("\n", timeout=60)
    assert files(workspace) == []
    assert not state_dir(workspace).exists()


def test_state_from_an_older_sdk_in_the_workspace_is_cleaned_up(workspace: Path) -> None:
    # What an earlier SDK version left when its session was killed.
    agent = workspace / ".bob" / "agents" / "old.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("old")
    legacy = workspace / ".bob" / ".bob-agent-sdk" / "sessions"
    legacy.mkdir(parents=True)
    resource = {"kind": "file", "path": ".bob/agents/old.md", "sha": _sha("old")}
    manifest = {"pid": 1, "resources": [resource], "dirs": [".bob", ".bob/agents"]}
    (legacy / "0123456789abcdef.json").write_text(json.dumps(manifest))

    cleanup_workspace(workspace)
    assert files(workspace) == []


def test_global_hooks_leave_the_workspace_untouched(
    cli: Path, workspace: Path, home: Path, tmp_path: Path
) -> None:
    settings = home / ".bob" / "settings" / "settings.json"
    settings.parent.mkdir(parents=True)
    original = json.dumps({"bobShell": {"lastRunVersion": "2.1.0"}})
    settings.write_text(original)
    other = tmp_path / "other"
    other.mkdir()
    fired = []

    async def count(event):
        fired.append(event["tool_use_id"])

    def options(cwd: Path) -> BobAgentOptions:
        hooks = {"PreToolUse": [HookMatcher(hooks=[count])]}
        return BobAgentOptions(cli_path=cli, cwd=cwd, hooks=hooks, hook_scope="global")

    async def run() -> None:
        # Two sessions in two workspaces share the one global entry.
        first = BobSDKClient(options(workspace))
        second = BobSDKClient(options(other))
        await first.connect()
        await second.connect()
        during = json.loads(settings.read_text())
        assert during["hooks"]["PreToolUse"] == [bridge_group()]
        assert during["bobShell"] == {"lastRunVersion": "2.1.0"}
        await first.disconnect()
        await second.query("work")
        async for _ in second.receive_response():
            pass
        assert len(fired) == 5  # The second session's hooks still run.
        await second.disconnect()

    asyncio.run(run())
    assert files(workspace) == [] and files(other) == []
    assert settings.read_text() == original

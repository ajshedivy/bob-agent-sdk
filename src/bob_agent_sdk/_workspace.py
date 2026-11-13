"""Files that the SDK places in the workspace's `.bob` directory for one session.

Bob reads custom modes, subagent definitions, skills and hooks from the
workspace, and `bob acp` has no other way to receive them. The SDK writes them
before it starts Bob and removes them when the session closes.

Several SDK sessions can use one workspace at the same time, and a session can
be killed before it cleans up. The rules that keep this safe:

- **One lock.** Every change happens while holding the workspace lock.
- **One manifest per session.** `.bob/.bob-agent-sdk/sessions/<id>.json` lists
  what the session uses. The session also holds a lock of its own for as long
  as it lives, so a manifest whose session lock is free belongs to a dead
  session.
- **Shared resources are counted.** Two sessions that need the same subagent
  file, skill link or hook entry share it. It is removed when the last
  session that lists it is gone.
- **Dead sessions are cleaned up** by the next session that starts or closes
  in the workspace, or by `cleanup_workspace()`.
- **Only the SDK's own files are removed.** A file is deleted only if it still
  has the content the SDK wrote, a link only if it still has the same target.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from ._errors import BobSDKError
from ._hooks import HOOK_EVENTS, SCOPED_TOOLS, bridge_group
from ._locks import lock_file, path_key, remove_lock_file
from .types import AgentDefinition, ModeDefinition

_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_SKILL_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_STATE_DIR = ".bob/.bob-agent-sdk"
_SETTINGS = ".bob/settings.json"

# A resource is a JSON object in the manifest. `kind` and `path` identify it:
#   {"kind": "file",  "path": ..., "sha": ...}     a file with known content
#   {"kind": "link",  "path": ..., "target": ...}  a symlink to a skill folder
#   {"kind": "mode",  "path": ..., "sha": ..., "slug": ...}  a custom mode file
#   {"kind": "hooks", "path": ".bob/settings.json", "created_file": bool}
Resource = dict[str, Any]
Manifest = dict[str, Any]


def cleanup_workspace(cwd: str | Path) -> None:
    """Remove the files that killed SDK sessions left in a workspace.

    A new session does this on its own. Call it to clean up without starting one.
    """
    WorkspaceFiles(str(cwd)).reap()


class WorkspaceFiles:
    def __init__(self, cwd: str) -> None:
        self._root = Path(cwd)
        self._id = secrets.token_hex(8)
        self._resources: list[Resource] = []
        self._contents: dict[str, str] = {}  # Path to the text to write there.
        self._dirs: list[str] = []  # Directories this session keeps alive.
        self._session_lock: FileLock | None = None
        self._orphan_dirs: set[str] = set()  # Directories that dead sessions created.

    # ----------------------------------------------------------- declaring
    # These methods only record what the session needs. install() writes it.

    def add_mode(self, mode: ModeDefinition) -> str:
        """Define a custom mode in a plugin folder of its own. Returns the mode slug."""
        slug = mode.slug or f"sdk-{self._id}"
        entry: dict[str, Any] = {
            "slug": slug,
            "name": mode.name,
            "roleDefinition": mode.role_definition,
            "groups": list(mode.groups),
        }
        if mode.custom_instructions:
            entry["customInstructions"] = mode.custom_instructions
        if mode.description:
            entry["description"] = mode.description
        if mode.allowed_subagents is not None:
            entry["allowedSubagents"] = list(mode.allowed_subagents)
        # JSON is valid YAML, so no YAML library is needed.
        content = json.dumps({"customModes": [entry]}, indent=2)
        path = f".bob/plugins/bob-agent-sdk-{self._id}/custom_modes.yaml"
        self._contents[path] = content
        self._resources.append({"kind": "mode", "path": path, "sha": _sha(content), "slug": slug})
        return slug

    def add_agents(self, agents: dict[str, AgentDefinition]) -> None:
        """Define subagents as `.bob/agents/<name>.md` files."""
        for name, agent in agents.items():
            if not _NAME.match(name):
                raise BobSDKError(f"Invalid agent name {name!r}. Use letters, digits, '-' and '_'.")
            lines = ["---", f"name: {name}", f"description: {_one_line(agent.description)}"]
            for key, items in (
                ("groups", agent.groups),
                ("allowTools", agent.allow_tools),
                ("denyTools", agent.deny_tools),
            ):
                if items is not None:
                    lines += [f"{key}:", *(f"  - {item}" for item in items)]
            if agent.model_tier:
                lines.append(f"modelTier: {agent.model_tier}")
            if agent.max_turns is not None:
                lines.append(f"maxTurns: {agent.max_turns}")
            content = "\n".join([*lines, "---", agent.prompt, ""])
            path = f".bob/agents/{name}.md"
            self._contents[path] = content
            self._resources.append({"kind": "file", "path": path, "sha": _sha(content)})

    def add_skills(self, skills: Sequence[str | Path]) -> None:
        """Make skill folders available as `.bob/skills/<name>`, through symlinks."""
        for skill in skills:
            source = Path(skill).resolve()
            if not (source / "SKILL.md").is_file():
                raise BobSDKError(f"{source} is not a skill folder: it has no SKILL.md.")
            if not _SKILL_NAME.match(source.name) or len(source.name) > 64:
                raise BobSDKError(
                    f"Invalid skill folder name {source.name!r}. Bob accepts lowercase "
                    "letters, digits and single dashes, up to 64 characters."
                )
            self._resources.append(
                {"kind": "link", "path": f".bob/skills/{source.name}", "target": str(source)}
            )

    def add_hook_bridge(self) -> None:
        """Add the SDK's hook command to every event in `.bob/settings.json`."""
        if os.name == "nt":
            raise BobSDKError("Hooks need a POSIX shell and are not supported on Windows.")
        self._resources.append({"kind": "hooks", "path": _SETTINGS, "created_file": False})

    # ---------------------------------------------------------- life cycle

    def install(self) -> None:
        """Write everything that was declared. Changes nothing if it raises."""
        if not self._resources:
            return
        with self._locked():
            others = self._reap()
            self._remove_unused_dirs(others)
            self._plan(others)
            self._write_manifest()
            self._create()

    def cleanup(self) -> None:
        """Remove what this session wrote, unless another live session uses it."""
        if self._session_lock is None:
            return
        with self._locked():
            others = self._reap()
            self._release(self._resources, others)
            self._orphan_dirs.update(self._dirs)
            self._manifest_path(self._id).unlink(missing_ok=True)
            self._session_lock.release()
            remove_lock_file(self._session_lock)
            self._session_lock = None
            self._remove_unused_dirs(others)

    def reap(self) -> None:
        """Clean up after dead sessions only."""
        if not (self._root / _STATE_DIR).exists():
            return
        with self._locked():
            self._remove_unused_dirs(self._reap())

    # ------------------------------------------------------- other sessions

    def foreign_names(self) -> dict[str, set[str]]:
        """The subagents, skills and modes that only other live SDK sessions define.

        Keyed by the Bob tool that uses them: `spawn_subagent`, `use_skill`, `switch_mode`.
        """
        with self._locked():
            others = self._reap()
        mine = {(r["kind"], r["path"]) for r in self._resources}
        my_modes = {r["slug"] for r in self._resources if r["kind"] == "mode"}
        names: dict[str, set[str]] = {tool: set() for tool in SCOPED_TOOLS}
        for manifest in others:
            for resource in manifest["resources"]:
                kind, path = resource["kind"], Path(resource["path"])
                if (kind, resource["path"]) in mine:
                    continue  # Both sessions define it the same way, so it is shared.
                if kind == "file" and path.parent.name == "agents":
                    names["spawn_subagent"].add(path.stem)
                elif kind == "link":
                    names["use_skill"].add(path.name)
                elif kind == "mode" and resource["slug"] not in my_modes:
                    names["switch_mode"].add(resource["slug"])
        return names

    def project_agents(self) -> list[str]:
        """The subagents in `.bob/agents` that no other SDK session owns."""
        foreign = self.foreign_names()["spawn_subagent"]
        files = sorted((self._root / ".bob" / "agents").glob("*.md"))
        return [file.stem for file in files if file.stem not in foreign]

    # ------------------------------------------------------------ planning

    def _plan(self, others: list[Manifest]) -> None:
        """Check every resource against the workspace and the other sessions.

        Raises before anything is written. Afterwards `self._resources` holds
        what this session will own or share, and `self._dirs` the directories
        it keeps alive.
        """
        owned = {(r["kind"], r["path"]): r for manifest in others for r in manifest["resources"]}
        kept: list[Resource] = []
        for resource in self._resources:
            kind, path = resource["kind"], resource["path"]
            existing = owned.get((kind, path))
            target = self._root / path
            if kind == "hooks":
                settings = _read_settings(target)  # Raises if the file is not usable.
                if existing:
                    resource = dict(existing)  # Share it, with its `created_file` flag.
                elif _has_bridge(settings):
                    continue  # Someone put the entry there by hand. It is not ours to remove.
                else:
                    resource["created_file"] = not target.exists()
            elif kind == "mode":
                for other in owned.values():
                    if other["kind"] == "mode" and other["slug"] == resource["slug"]:
                        if other["sha"] != resource["sha"]:
                            raise BobSDKError(
                                f"Another SDK session in this workspace defines the mode "
                                f"{resource['slug']!r} differently."
                            )
            elif existing:
                same = existing.get("sha") == resource.get("sha") and existing.get(
                    "target"
                ) == resource.get("target")
                if not same:
                    raise BobSDKError(
                        f"Another SDK session in this workspace uses {path} with a "
                        "different definition."
                    )
            elif target.exists() or target.is_symlink():
                raise BobSDKError(f"{target} already exists. The SDK does not overwrite it.")
            kept.append(resource)
        self._resources = kept

        other_dirs = {d for manifest in others for d in manifest["dirs"]}
        # The manifest path is included so that `.bob` is tracked even when the
        # session has no other file. The state folders have their own cleanup.
        paths = [r["path"] for r in self._resources] + [f"{_STATE_DIR}/sessions/x"]
        dirs: set[str] = set()
        for path in paths:
            for parent in Path(path).parents:
                relative = parent.as_posix()
                if relative == "." or relative.startswith(_STATE_DIR):
                    continue
                # Keep a directory alive if this session creates it, or if a
                # session that created it is still alive.
                if not (self._root / parent).exists() or relative in other_dirs:
                    dirs.add(relative)
        self._dirs = sorted(dirs)

    def _write_manifest(self) -> None:
        # Held until cleanup(), or until this process dies.
        self._session_lock = self._lock_for(self._id)
        self._session_lock.acquire()
        path = self._manifest_path(self._id)
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {"pid": os.getpid(), "resources": self._resources, "dirs": self._dirs}
        _atomic_write(path, json.dumps(manifest, indent=2))

    def _create(self) -> None:
        for resource in self._resources:
            kind = resource["kind"]
            target = self._root / resource["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "hooks":
                settings = _read_settings(target)
                if not _has_bridge(settings):
                    hooks = settings.setdefault("hooks", {})
                    for event in HOOK_EVENTS:
                        groups = hooks.setdefault(event, [])
                        if bridge_group() not in groups:
                            groups.append(bridge_group())
                    _atomic_write(target, json.dumps(settings, indent=2) + "\n")
            elif kind == "link":
                if not target.is_symlink():  # Else another session made it.
                    target.symlink_to(resource["target"], target_is_directory=True)
            elif not target.exists():
                _atomic_write(target, self._contents[resource["path"]])

    # ----------------------------------------------------------- releasing

    def _reap(self) -> list[Manifest]:
        """Clean up after dead sessions. Returns the manifests of the other live ones."""
        sessions = self._root / _STATE_DIR / "sessions"
        live: list[Manifest] = []
        dead: list[tuple[Path, FileLock, Manifest]] = []
        for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
            if path.stem == self._id:
                continue
            lock = self._lock_for(path.stem)
            try:
                lock.acquire(timeout=0)
            except Timeout:  # Its session holds the lock, so it is alive.
                live.append(_load_manifest(path))
                continue
            dead.append((path, lock, _load_manifest(path)))  # Nobody holds its lock.
        # Release the dead sessions only now, when all live ones are known.
        for path, lock, manifest in dead:
            self._release(manifest["resources"], live)
            self._orphan_dirs.update(manifest["dirs"])
            path.unlink(missing_ok=True)
            lock.release()
            remove_lock_file(lock)
        return live

    def _release(self, resources: list[Resource], others: list[Manifest]) -> None:
        """Remove the resources that no session in `others` lists."""
        in_use = {(r["kind"], r["path"]) for other in others for r in other["resources"]}
        for resource in resources:
            kind, path = resource["kind"], resource["path"]
            if (kind, path) in in_use:
                continue
            target = self._root / path
            if kind == "hooks":
                self._remove_bridge(target, resource.get("created_file", False))
            elif kind == "link":
                if target.is_symlink() and os.readlink(target) == resource["target"]:
                    target.unlink()
            elif target.is_file() and _sha(target.read_text()) == resource["sha"]:
                target.unlink()

    @staticmethod
    def _remove_bridge(path: Path, created_file: bool) -> None:
        if not path.exists():
            return
        try:
            settings = _read_settings(path)
        except BobSDKError:
            return  # Someone broke the file since. Leave it to them.
        hooks = settings.get("hooks", {})
        for event in HOOK_EVENTS:
            groups = [g for g in hooks.get(event, []) if g != bridge_group()]
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event, None)
        if not hooks:
            settings.pop("hooks", None)
        if not settings and created_file:
            path.unlink()
        else:
            _atomic_write(path, json.dumps(settings, indent=2) + "\n")

    def _remove_unused_dirs(self, others: list[Manifest]) -> None:
        """Remove the SDK's state folders once no session is left, then the empty
        directories that closed or dead sessions created."""
        state = self._root / _STATE_DIR
        for directory in (state / "sessions", state):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        in_use = {d for other in others for d in other["dirs"]}
        for relative in sorted(self._orphan_dirs - in_use, key=len, reverse=True):  # Deepest first.
            directory = self._root / relative
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        self._orphan_dirs.clear()

    # --------------------------------------------------------------- utils

    def _manifest_path(self, session_id: str) -> Path:
        return self._root / _STATE_DIR / "sessions" / f"{session_id}.json"

    def _lock_for(self, name: str) -> FileLock:
        """A lock for this workspace. `name` is `workspace` or a session ID."""
        return lock_file(f"{path_key(self._root)}-{name}")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the workspace lock."""
        with self._lock_for("workspace"):
            yield


def _load_manifest(path: Path) -> Manifest:
    try:
        manifest = json.loads(path.read_text())
        return {"resources": list(manifest["resources"]), "dirs": list(manifest["dirs"])}
    except (json.JSONDecodeError, KeyError, TypeError):
        # The session died while it wrote the manifest, so before it created anything.
        return {"resources": [], "dirs": []}


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BobSDKError(f"{path} is not valid JSON. The SDK did not change it.") from exc
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise BobSDKError(f"{path} does not have the expected shape. The SDK did not change it.")
    return settings


def _has_bridge(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks", {})
    return all(bridge_group() in hooks.get(event, []) for event in HOOK_EVENTS)


def _atomic_write(path: Path, content: str) -> None:
    """Write through a temporary file, so that a reader never sees half a file."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(content)
    os.replace(temporary, path)


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _one_line(text: str) -> str:
    return " ".join(text.split())

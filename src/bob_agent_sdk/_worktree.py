"""`isolation="worktree"`: each session works in a git worktree of its own.

The session gets a new branch and a separate working copy, so it shares no
files with other sessions and writes nothing to the user's checkout. When the
session closes, the SDK removes the worktree if the session left no work in
it. A worktree with work is kept: only the user can decide what to do with it.
"""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path

from filelock import FileLock, Timeout

from ._errors import BobSDKError
from ._locks import lock_file, path_key, remove_lock_file
from .types import Worktree

# Not the temporary directory: the system may delete old files there, and a
# kept worktree holds work that exists nowhere else.
WORKTREES_DIR = Path.home() / ".bob-agent-sdk" / "worktrees"
_BRANCH_PREFIX = "bob/"


def create_worktree(cwd: str) -> tuple[Worktree, str, FileLock]:
    """Create a worktree for the repository that contains `cwd`.

    Returns the worktree, the directory in it that corresponds to `cwd`, and a
    lock that marks the worktree as in use. Pass the lock to close_worktree().
    """
    try:
        repo = Path(_git(cwd, "rev-parse", "--show-toplevel"))
        _git(repo, "rev-parse", "--verify", "HEAD")
    except BobSDKError as exc:
        raise BobSDKError(
            f"isolation='worktree' needs a git repository with at least one commit: {exc}"
        ) from exc
    _prune(repo)

    token = secrets.token_hex(4)
    worktree = Worktree(
        path=WORKTREES_DIR / f"{repo.name}-{token}",
        branch=f"{_BRANCH_PREFIX}{token}",
        repo=repo,
    )
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    lock = _in_use_lock(worktree.path)
    lock.acquire()
    try:
        _git(repo, "worktree", "add", "-b", worktree.branch, str(worktree.path), "HEAD")
    except BobSDKError:
        lock.release()
        remove_lock_file(lock)
        raise
    session_cwd = worktree.path / Path(cwd).resolve().relative_to(repo.resolve())
    session_cwd.mkdir(parents=True, exist_ok=True)  # `cwd` may be an untracked folder.
    return worktree, str(session_cwd), lock


def close_worktree(worktree: Worktree, lock: FileLock) -> None:
    """Remove the worktree if the session left no work in it. Sets `worktree.removed`."""
    try:
        if worktree.path.exists() and not _has_work(worktree.repo, worktree.path):
            _remove(worktree.repo, worktree.path, worktree.branch, force=False)
            worktree.removed = True
    finally:
        lock.release()
        remove_lock_file(lock)


def remove_worktree(worktree: Worktree | str | Path, *, force: bool = False) -> None:
    """Remove a worktree that a session left behind, and its branch.

    Refuses if the worktree has uncommitted changes, or commits that are on no
    other branch. `force=True` removes it anyway and discards that work.
    """
    path = Path(worktree.path if isinstance(worktree, Worktree) else worktree)
    if not path.exists():
        raise BobSDKError(f"{path} does not exist.")
    # The first entry of `git worktree list` is always the main checkout.
    repo = Path(_git(path, "worktree", "list", "--porcelain").splitlines()[0].split(" ", 1)[1])
    branch = _git(path, "branch", "--show-current")
    if not force and _has_work(repo, path):
        raise BobSDKError(
            f"{path} has uncommitted changes, or commits that are on no other branch. "
            f"Merge the branch {branch!r} first, or pass force=True to discard the work."
        )
    _remove(repo, path, branch, force=force)


def _has_work(repo: Path, path: Path) -> bool:
    """True if removing the worktree and its branch would lose something."""
    if _git(path, "status", "--porcelain"):
        return True  # Changed or untracked files.
    head = _git(path, "rev-parse", "HEAD")
    own = _git(path, "branch", "--show-current")
    branches = _git(
        repo, "for-each-ref", "--contains", head, "--format=%(refname:short)", "refs/heads"
    )
    return not any(branch != own for branch in branches.splitlines())


def _remove(repo: Path, path: Path, branch: str, *, force: bool) -> None:
    _git(repo, "worktree", "remove", *(["--force"] if force else []), str(path))
    if branch:
        _git(repo, "branch", "-D", branch)


def _prune(repo: Path) -> None:
    """Remove the worktrees of sessions that died without leaving work."""
    path: Path | None = None
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line.split(" ", 1)[1])
        elif line.startswith(f"branch refs/heads/{_BRANCH_PREFIX}") and path is not None:
            if WORKTREES_DIR.resolve() not in path.resolve().parents or not path.exists():
                continue
            lock = _in_use_lock(path)
            try:
                lock.acquire(timeout=0)
            except Timeout:
                continue  # A live session works in it.
            try:
                if not _has_work(repo, path):
                    _remove(repo, path, line.removeprefix("branch refs/heads/"), force=False)
            finally:
                lock.release()
                remove_lock_file(lock)


def _in_use_lock(path: Path) -> FileLock:
    return lock_file(f"{path_key(path)}-worktree")


def _git(directory: str | Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), *args], capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise BobSDKError("git was not found on PATH.") from exc
    if done.returncode != 0:
        raise BobSDKError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()

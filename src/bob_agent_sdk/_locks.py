"""Lock files that work on Windows, macOS and Linux.

The files are in the user's temporary directory, not in a workspace. A lock
file must never be deleted while a process may be waiting for it, and keeping
them outside lets the SDK leave the workspace empty.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import tempfile
from pathlib import Path

from filelock import FileLock


def path_key(path: Path) -> str:
    """A short, stable name for a directory, for use in lock names."""
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def lock_file(name: str) -> FileLock:
    user = hashlib.sha256(getpass.getuser().encode()).hexdigest()[:12]
    directory = Path(tempfile.gettempdir()) / f"bob-agent-sdk-{user}"
    directory.mkdir(mode=0o700, exist_ok=True)
    return FileLock(directory / f"{name}.lock")


def remove_lock_file(lock: FileLock) -> None:
    """Delete a lock file whose name is never used again, such as a session's."""
    try:
        os.unlink(lock.lock_file)
    except OSError:
        pass

#!/usr/bin/env python3
"""Install the Bob Agent SDK from source into a project, and check what it needs.

The SDK is not on PyPI yet. This script clones its repository and installs it
into the project in editable mode. It uses uv when uv is installed, and a
plain virtual environment with pip otherwise.

    python3 setup_sdk.py                     # set up the current directory
    python3 setup_sdk.py --project ./my-agent
    python3 setup_sdk.py --update            # also pull the latest SDK source

Needs only the Python standard library. Exit code 0 means the SDK is importable.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/ajshedivy/bob-agent-sdk.git"
DEFAULT_SDK_DIR = Path.home() / ".bob-agent-sdk" / "sdk"
MIN_PYTHON = (3, 10)
MIN_BOB = (2, 1)
API_KEY_HELP = "https://bob.ibm.com/docs/ide/features/bobalytics"


def run(*command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False
    )


def must(*command: str, cwd: Path | None = None) -> str:
    done = run(*command, cwd=cwd)
    if done.returncode != 0:
        sys.exit(f"FAILED: {' '.join(command)}\n{done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def get_source(sdk_dir: Path, update: bool) -> None:
    if (sdk_dir / "src" / "bob_agent_sdk").is_dir():
        if update:
            must("git", "-C", str(sdk_dir), "pull", "--ff-only")
            print(f"OK   SDK source updated: {sdk_dir}")
        else:
            print(f"OK   SDK source found: {sdk_dir}")
        return
    if sdk_dir.exists() and any(sdk_dir.iterdir()):
        sys.exit(f"FAILED: {sdk_dir} exists and is not a clone of the SDK. Pass --sdk-dir.")
    if shutil.which("git") is None:
        sys.exit("FAILED: git is not installed. Install git, or clone the SDK yourself.")
    sdk_dir.parent.mkdir(parents=True, exist_ok=True)
    must("git", "clone", "--depth", "1", REPO_URL, str(sdk_dir))
    print(f"OK   SDK source cloned: {sdk_dir}")


def install_with_uv(project: Path, sdk_dir: Path) -> list[str]:
    """Add the SDK to the project's pyproject.toml. Returns the command that runs Python."""
    if not (project / "pyproject.toml").exists():
        must("uv", "init", "--bare", cwd=project)
        print("OK   Created pyproject.toml (uv init --bare)")
    must("uv", "add", "--editable", str(sdk_dir), cwd=project)
    print("OK   Installed bob-agent-sdk with uv (editable)")
    return ["uv", "run", "python"]


def install_with_pip(project: Path, sdk_dir: Path) -> list[str]:
    """Install the SDK into .venv with pip. Returns the command that runs Python."""
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"FAILED: Python {'.'.join(map(str, MIN_PYTHON))} or later is needed, "
            f"this is {sys.version.split()[0]}. Install uv, or run this script with a newer Python."
        )
    venv = project / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        must(sys.executable, "-m", "venv", str(venv))
        print("OK   Created .venv")
    must(str(python), "-m", "pip", "install", "--quiet", "--editable", str(sdk_dir))
    print("OK   Installed bob-agent-sdk with pip (editable)")
    return [str(python.relative_to(project))]


def check_bob() -> bool:
    bob = shutil.which("bob")
    if bob is None:
        print("MISSING  Bob Shell: `bob` is not on PATH. Install Bob Shell 2.1 or later.")
        return False
    try:
        output = subprocess.run(
            [bob, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        output = ""
    match = re.search(r"(\d+)\.(\d+)", output)
    if not match:
        print("WARN  Bob Shell: could not read the version from `bob --version`.")
        return True
    version = (int(match.group(1)), int(match.group(2)))
    if version < MIN_BOB:
        found = f"{version[0]}.{version[1]}"
        print(f"MISSING  Bob Shell {found} is too old. Version 2.1 or later is needed.")
        return False
    print(f"OK   Bob Shell {version[0]}.{version[1]}")
    return True


def check_login(project: Path) -> tuple[bool, bool]:
    """Returns (login is set up, the key is in the project's .env file)."""
    env_file = project / ".env"
    in_env_file = env_file.exists() and bool(
        re.search(r"^\s*(export\s+)?BOB_API_KEY\s*=\s*\S", env_file.read_text(), re.MULTILINE)
    )
    if in_env_file:
        print("OK   Login: BOB_API_KEY is in .env")
        inside_repo = run("git", "rev-parse", "--is-inside-work-tree", cwd=project).returncode == 0
        if inside_repo and run("git", "check-ignore", "-q", ".env", cwd=project).returncode != 0:
            print("WARN .env holds a key and git does not ignore it. Add `.env` to .gitignore.")
    elif os.environ.get("BOB_API_KEY"):
        print("OK   Login: BOB_API_KEY is set in the environment")
    elif os.environ.get("BOB_USE_SSO", "").strip().lower() in ("1", "true", "yes"):
        print("OK   Login: BOB_USE_SSO is set, so Bob's saved SSO login is used")
    else:
        print(
            "MISSING  Login: no Bob API key. Create one at\n"
            f"         {API_KEY_HELP}\n"
            "         and put BOB_API_KEY=<key> in .env (keep .env out of git).\n"
            "         To use the SSO login that `bob` saved on this machine, set BOB_USE_SSO=1."
        )
        return False, False
    return True, in_env_file


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="project directory")
    parser.add_argument("--sdk-dir", type=Path, default=DEFAULT_SDK_DIR, help="SDK clone location")
    parser.add_argument("--update", action="store_true", help="pull the latest SDK source")
    parser.add_argument("--no-uv", action="store_true", help="use venv and pip even if uv exists")
    args = parser.parse_args()

    project = args.project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    sdk_dir = args.sdk_dir.expanduser().resolve()

    get_source(sdk_dir, args.update)
    use_uv = not args.no_uv and shutil.which("uv") is not None
    if not use_uv and not args.no_uv:
        print("NOTE uv is not installed. Using venv and pip.")
    python = install_with_uv(project, sdk_dir) if use_uv else install_with_pip(project, sdk_dir)

    version = must(
        *python, "-c", "import bob_agent_sdk; print(bob_agent_sdk.__version__)", cwd=project
    )
    print(f"OK   bob_agent_sdk {version} is importable")

    bob_ok = check_bob()
    login_ok, key_in_env_file = check_login(project)

    if use_uv:
        runner = "uv run --env-file .env" if key_in_env_file else "uv run"
    else:
        runner = python[0]
    print(f"\nRun an agent with:  {runner} my_agent.py")
    if not use_uv and key_in_env_file:
        print("Load the key first:  set -a; . ./.env; set +a")
    if not (bob_ok and login_ok):
        print("\nThe SDK is installed. Fix the MISSING items above before you run an agent.")


if __name__ == "__main__":
    main()

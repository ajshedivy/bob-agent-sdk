import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from bob_agent_sdk import BobAgentOptions, query

FAKE_BOB = Path(__file__).parent / "fake_bob.py"


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    """An executable named `bob` that runs the stand-in agent."""
    path = tmp_path / "bob"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_BOB}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


async def collect(prompt: str, options: BobAgentOptions) -> list[Any]:
    return [message async for message in query(prompt=prompt, options=options)]

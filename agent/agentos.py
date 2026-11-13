"""Serve a Bob agent with AgentOS, with a person approving its shell commands.

The agent is available at the standard /agents/{agent_id}/runs endpoint, with
streaming (SSE) and non-streaming responses, next to any native Agno agents.

The agent has a database and `auto_approve=False`. When Bob wants to run a
command that is not already approved in your Bob settings, the run pauses and
the AgentOS chat asks you to confirm or reject it.

    cd agent
    uv run agentos.py

Then call the API:

    curl http://localhost:7777/agents

    # A run that needs your approval ends with a RunPaused event. It carries
    # the run_id and the tool that Bob wants to use.
    curl -N -X POST http://localhost:7777/agents/bob-reviewer/runs \\
        --form-string "message=Run the command: wc -l bob_agent.py" \\
        -F "session_id=demo" -F "stream=true"

    # Approve it: send the tool back with "confirmed": true (or false to reject).
    curl -N -X POST http://localhost:7777/agents/bob-reviewer/runs/<run_id>/continue \\
        --form-string 'tools=[{"tool_call_id": "...", "tool_name": "execute",
            "tool_args": {"command": "wc -l bob_agent.py"}, "confirmed": true}]' \\
        -F "session_id=demo" -F "stream=true"

The AgentOS chat UI does both steps for you: it shows the request and sends your answer.
"""

from pathlib import Path

from agno.db.sqlite import SqliteDb
from agno.os import AgentOS
from bob_agent import BobAgent

from bob_agent_sdk import ModeDefinition

bob_reviewer = BobAgent(
    name="Bob Reviewer",
    description="A code reviewer that runs on Bob and asks before it runs a command",
    mode=ModeDefinition(
        name="Reviewer",
        role_definition="You are a code reviewer. You read code and report risks.",
        custom_instructions="Be brief. Never change files.",
        groups=["read", "execute"],
    ),
    cwd=Path(__file__).parent,
    # The database holds a paused run while it waits for your decision.
    db=SqliteDb(db_file=str(Path(__file__).parent / "tmp" / "agentos.db")),
    # False: Bob asks before it runs a command, and the run pauses for your answer.
    auto_approve=False,
    max_turns=15,
)

agent_os = AgentOS(
    name="Bob Agent SDK Example",
    description="AgentOS serving a Bob agent",
    agents=[bob_reviewer],
)
app = agent_os.get_app()

if __name__ == "__main__":
    agent_os.serve(app="agentos:app", port=7777)

"""Serve a Bob agent with AgentOS.

The agent is available at the standard /agents/{agent_id}/runs endpoint, with
streaming (SSE) and non-streaming responses, next to any native Agno agents.

    cd agent
    uv run agentos.py

Then call the API:

    curl http://localhost:7777/agents

    curl -X POST http://localhost:7777/agents/bob-reviewer/runs \\
        -F "message=Review bob_agent.py and give me the two biggest risks" \\
        -F "stream=true" --no-buffer

    curl -X POST http://localhost:7777/agents/bob-reviewer/runs \\
        -F "message=Which file has the BobAgent class?" \\
        -F "stream=false"
"""

from pathlib import Path

from agno.os import AgentOS
from bob_agent import BobAgent

from bob_agent_sdk import ModeDefinition

bob_reviewer = BobAgent(
    name="Bob Reviewer",
    description="A read-only code reviewer that runs on Bob",
    mode=ModeDefinition(
        name="Reviewer",
        role_definition="You are a code reviewer. You read code and report risks.",
        custom_instructions="Be brief. Never change files.",
        groups=["read"],
    ),
    cwd=Path(__file__).parent,
    auto_approve=True,
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

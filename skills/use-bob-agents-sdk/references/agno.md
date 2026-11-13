# Bob as an Agno agent (AgentOS)

`BobAgent` runs a Bob agent through Agno's `run()` and `print_response()`, and serves it with AgentOS.

## Setup

`BobAgent` is one file, `agent/bob_agent.py`, in the SDK repository. It is not part of the `bob_agent_sdk` package. Copy it into the project and add Agno:

```bash
cp ~/.bob-agent-sdk/sdk/agent/bob_agent.py .
uv add "agno[os]"            # or: .venv/bin/pip install "agno[os]"
```

It needs Agno 3.2 or later. The repository's `agent/` folder has runnable examples (`basic.py`, `agentos.py`).

## Use

```python
from bob_agent import BobAgent
from bob_agent_sdk import ModeDefinition

agent = BobAgent(
    name="Bob Reviewer",
    mode=ModeDefinition(role_definition="You are a code reviewer.", groups=["read"]),
    cwd="/path/to/project",
    auto_approve=True,
    max_turns=15,
)
agent.print_response("Review src/app.py", stream=True)
agent.print_response("And the tests?", session_id="chat")  # same session_id = same Bob session
```

Fields: `mode`, `cwd`, `auto_approve`, `max_turns`, `max_cost`, `mcp_servers`, `agents`, `skills`, `isolation`, `db`, and `options_kwargs` for any other `BobAgentOptions` field (for example `hooks`).

## AgentOS

```python
from agno.os import AgentOS

agent_os = AgentOS(agents=[agent])
app = agent_os.get_app()

if __name__ == "__main__":
    agent_os.serve(app="my_module:app", port=7777)
```

The agent is served at `/agents/{agent_id}/runs` (streaming and non-streaming). The ID comes from the name: `Bob Reviewer` becomes `bob-reviewer`.

## Approval by a person

Give the agent a database and leave `auto_approve` off. Bob's approval requests then pause the run, and the person confirms or rejects each call in the AgentOS chat.

```python
from agno.db.sqlite import SqliteDb

agent = BobAgent(name="Bob Coder", db=SqliteDb(db_file="tmp/agentos.db"), auto_approve=False)
```

| Settings | Who decides |
|---|---|
| `auto_approve=True` | Bob |
| `options_kwargs={"can_use_tool": fn}` | Your function |
| a `db`, and neither of the above | A person, in the chat |
| no `db`, and neither of the above | Nobody: the call is rejected |

Over HTTP, a paused run ends its stream with a `RunPaused` event. Send the tool back with `"confirmed": true` or `false` to `/agents/{agent_id}/runs/{run_id}/continue`.

## Things to know

- A run that hits `max_turns` or `max_cost` ends with the status `ERROR`.
- The link between an Agno session and its Bob session is kept in memory. After a restart, a session starts a new Bob session.
- Use `--form-string` with `curl` for messages that contain `;`. `-F` cuts the value at the first `;`.

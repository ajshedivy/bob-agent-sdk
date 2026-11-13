# Bob as an Agno external agent

`BobAgent` runs a Bob agent through [Agno](https://docs.agno.com). You can call it with Agno's `run()` and `print_response()`, and serve it with AgentOS next to native Agno agents.

It is built on `bob_agent_sdk` and on Agno's `BaseExternalAgent`, the same base class that Agno's `ClaudeAgent` uses.

## Requirements

- Everything that [`bob_agent_sdk`](../README.md#requirements) needs: Bob Shell on your `PATH` and a Bob login
- Agno 3.2 or later (installed by `uv sync`)

## Install

```bash
cd agent
uv sync
```

## Use it

```python
from bob_agent import BobAgent

agent = BobAgent(
    name="Bob Coder",
    cwd="/path/to/project",
    auto_approve=True,
)
agent.print_response("Summarize this project", stream=True)
```

Runs with the same `session_id` continue the same Bob session, so Bob remembers earlier messages:

```python
agent.print_response("Check order A-1001.", session_id="order-chat")
agent.print_response("Which order did I just ask about?", session_id="order-chat")
```

## Serve it with AgentOS

```python
from agno.os import AgentOS

agent_os = AgentOS(agents=[agent])
app = agent_os.get_app()
```

The agent is then available at `/agents/{agent_id}/runs`, with streaming and non-streaming responses.

## Options

| Option | Purpose |
|---|---|
| `mode` | `agent`, `plan`, `ask`, a custom mode slug, or a `ModeDefinition` with the agent's role and tool groups. |
| `cwd` | Workspace directory. |
| `auto_approve` | Let Bob approve tool calls itself. See below. |
| `max_turns`, `max_cost` | Limits. A run that reaches one ends with the status `ERROR`. |
| `mcp_servers` | In-process tools and external MCP servers. |
| `agents`, `skills` | Subagents and skill folders. |
| `isolation` | `shared`, `enforced` or `worktree`. |
| `options_kwargs` | Any other `BobAgentOptions` field, for example `hooks`. |

The options have the same meaning as in [`bob_agent_sdk`](../README.md#options).

## Things to know

- **Set `auto_approve=True` for unattended runs.** An AgentOS run has nobody to ask. Without it, the SDK rejects every tool call that Bob wants approval for.
- **Tool names.** Agno shows each tool call by Bob's title for it, for example the shell command. To get Bob's tool names (`execute_command`, `write_file`), pass `options_kwargs={"hooks": {}}`.
- **Session memory is in the process.** The link between an Agno session and its Bob session is kept in memory. After a restart, a session starts a new Bob session.
- **Worktree sessions keep their worktree.** With `isolation="worktree"`, later runs of the same Agno session continue in the worktree of the first run.

## Examples

| File | Shows |
|---|---|
| [`basic.py`](basic.py) | A support agent with its own role and an in-process tool, streaming and multi-turn. |
| [`agentos.py`](agentos.py) | A read-only code reviewer served by AgentOS, with `curl` commands. |

```bash
uv run basic.py
uv run agentos.py
```

## Tests

```bash
uv run pytest
```

The tests use the SDK's stand-in for `bob acp`, so they need no Bob login.

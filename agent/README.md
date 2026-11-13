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
| `auto_approve` | Let Bob approve tool calls itself. |
| `db` | An Agno database. Needed for approval by a person. |
| `max_turns`, `max_cost` | Limits. A run that reaches one ends with the status `ERROR`. |
| `mcp_servers` | In-process tools and external MCP servers. |
| `agents`, `skills` | Subagents and skill folders. |
| `isolation` | `shared`, `enforced` or `worktree`. |
| `options_kwargs` | Any other `BobAgentOptions` field, for example `hooks`. |

The options have the same meaning as in [`bob_agent_sdk`](../README.md#options).

## Approval by a person

Give the agent a database and leave `auto_approve` off. When Bob wants approval for a tool call, the run pauses, and the person confirms or rejects the call in the chat.

```python
from agno.db.sqlite import SqliteDb

agent = BobAgent(
    name="Bob Coder",
    cwd="/path/to/project",
    db=SqliteDb(db_file="tmp/agentos.db"),
    auto_approve=False,
)
```

How it works:

1. Bob asks for approval. This happens for tool calls that your Bob settings (`~/.bob/settings/settings.json`) do not already allow.
2. The run ends its stream with a `RunPaused` event that carries the tool and its arguments. The AgentOS chat shows it as a confirmation.
3. The answer goes to `/agents/{agent_id}/runs/{run_id}/continue`. Bob, which was waiting, runs the tool or is told that it was rejected, and the run goes on.

A request waits for `hitl_timeout` seconds (default 900). After that it fails, and so does the run.

Who decides, depending on the settings:

| Settings | Who decides |
|---|---|
| `auto_approve=True` | Bob, by its safety rules |
| `options_kwargs={"can_use_tool": ...}` | Your function |
| A database, and neither of the above | A person, in the chat |
| No database, and neither of the above | Nobody: the call is rejected |

This uses Agno's support for approval requests of external agents, the same that its `ClaudeAgent` and `CodexAgent` use. That support is in a private Agno module (`agno.agents._hitl`), so an Agno update can change it. It was tested with Agno 3.2.0.

## Things to know

- **Tool names.** Agno shows a tool call by its kind of action (`execute`, `edit`, `read`) with the arguments, or by Bob's title for tools without a kind, such as MCP tools. To get Bob's tool names (`execute_command`, `write_file`), pass `options_kwargs={"hooks": {}}`.
- **Session memory is in the process.** The link between an Agno session and its Bob session is kept in memory. After a restart, a session starts a new Bob session.
- **Worktree sessions keep their worktree.** With `isolation="worktree"`, later runs of the same Agno session continue in the worktree of the first run.

## Examples

| File | Shows |
|---|---|
| [`basic.py`](basic.py) | A support agent with its own role and an in-process tool, streaming and multi-turn. |
| [`agentos.py`](agentos.py) | A code reviewer served by AgentOS that asks before it runs a command, with `curl` commands. |

```bash
uv run basic.py
uv run agentos.py
```

## Tests

```bash
uv run pytest
```

The tests use the SDK's stand-in for `bob acp`, so they need no Bob login.

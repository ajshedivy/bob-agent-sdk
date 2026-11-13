# API reference

Everything is imported from `bob_agent_sdk`. The SDK is async only.

## Contents

- [Entry points](#entry-points)
- [BobAgentOptions](#bobagentoptions)
- [Messages](#messages)
- [BobSDKClient](#bobsdkclient)
- [Errors](#errors)

## Entry points

```python
async for message in query(prompt="...", options=BobAgentOptions(...)):  # one prompt, new session
    ...

async with BobSDKClient(options) as client:  # several turns, one session
    await client.query("...")
    async for message in client.receive_response():
        ...
```

`prompt` is a string, or a list of ACP content blocks (for example to attach an image).

## BobAgentOptions

| Field | Type, default | Meaning |
|---|---|---|
| `cwd` | path, current dir | Workspace the agent works in |
| `mode` | `str \| ModeDefinition`, `"agent"` | `agent`, `plan`, `ask`, a custom mode slug, or a `ModeDefinition` |
| `mcp_servers` | dict | In-process tool servers and external MCP servers, by name |
| `can_use_tool` | async callback | Decides each tool call that Bob asks approval for |
| `auto_approve` | `False` | Bob approves tool calls itself when its safety rules allow |
| `agents` | dict of `AgentDefinition` | Subagents, by name |
| `skills` | list of paths | Skill folders (each with `SKILL.md`) to attach |
| `hooks` | dict or `None` | Python callbacks for Bob's hook events. Any non-`None` value, also `{}`, turns hooks on |
| `isolation` | `"shared"` | `shared`, `enforced` or `worktree` |
| `output_format` | `None` | A Pydantic model class, or `{"type": "json_schema", "schema": {...}}`. The answer is in `ResultMessage.structured_output` |
| `max_turns` | `None` | Stop a `query()` after this many model requests |
| `max_cost` | `None` | Stop when the session has spent this many Bobcoins |
| `resume` | `None` | Session ID of an earlier session to continue |
| `include_partial_messages` | `False` | Also yield `StreamEvent` for raw updates (text deltas) |
| `api_key` | `None` | Bob API key. Default: `BOB_API_KEY` |
| `team_id` | `None` | Bob team, for accounts with several teams |
| `trust`, `accept_license` | `False` | Pass `--trust` / `--accept-license` to Bob |
| `disable_mcp`, `disable_subagents` | `False` | Turn off Bob's own MCP servers / subagents |
| `cli_path`, `env`, `log_level`, `stderr` | | Control the Bob process |

## Messages

Check the type with `isinstance`.

| Message | Fields |
|---|---|
| `AssistantMessage` | `content`: list of `TextBlock(text)`, `ThinkingBlock(thinking)`, `ToolUseBlock` |
| `ToolUseBlock` | `id`, `title` (label, e.g. the shell command), `kind` (`read`, `edit`, `execute`, ...), `input` (dict), `locations`, `name` (Bob's tool name, e.g. `execute_command`; `None` unless hooks are on) |
| `ToolResultMessage` | `tool_use_id`, `status` (`completed` / `failed`), `output` (text or `None`), `content` (raw; file edits are `{"type": "diff", ...}`), `is_error` |
| `PlanMessage` | `entries`: list of `PlanEntry(content, status, priority)`. Each update replaces the last |
| `SystemMessage` | `subtype`, `data`. The first has `subtype == "init"` and `data["session_id"]`, `data["cwd"]`, `data["worktree"]` |
| `StreamEvent` | `event`: raw update dict. Text deltas have `event["sessionUpdate"] == "agent_message_chunk"` and `event["content"]["text"]` |
| `ResultMessage` | `stop_reason`, `is_error`, `result` (Bob's last text), `structured_output` (the typed answer, or `None`), `num_turns` (model requests in this `query()`), `cost` (this `query()`), `total_cost` (whole session), `usage`, `model`, `session_id`, `duration_ms`, `num_tool_calls`. Costs are in Bobcoins |

`stop_reason` values: `end_turn`, `cancelled` (after `interrupt()`), `max_turns`, `max_cost`, `error_max_structured_output_retries`, and Bob's own `max_tokens`, `max_turn_requests`, `refusal`. `is_error` is true for everything except `end_turn` and `cancelled`.

## BobSDKClient

| Member | Meaning |
|---|---|
| `await connect()` / `await disconnect()` | Start and stop Bob. `async with` does both |
| `await query(prompt)` | Send a prompt. Returns at once |
| `receive_response()` | Yield messages up to and including the turn's `ResultMessage` |
| `receive_messages()` | Yield all messages until the session closes |
| `await interrupt()` | Stop the current turn |
| `await set_mode(mode)` | Switch mode between turns |
| `session_id` | Pass later as `BobAgentOptions(resume=...)` |
| `worktree` | The `Worktree(path, branch, repo, removed)` when `isolation="worktree"` |

Only one turn runs at a time. Read `receive_response()` to its end, or call `interrupt()`, before the next `query()`.

## Errors

All extend `BobSDKError`.

| Error | Cause |
|---|---|
| `AuthenticationError` | No API key (and no `BOB_USE_SSO=1`), or Bob rejected the login |
| `CLINotFoundError` | `bob` is not on `PATH` |
| `ACPError` | Bob rejected a request. Has `code`, `message`, `data` |
| `ProcessError` | Bob exited unexpectedly. Has `exit_code`, `stderr` |

Exceptions in your own code:

- **`can_use_tool` or a hook callback raises:** the exception is re-raised from the message loop (`receive_response()` or `query()`).
- **A tool handler raises:** the error text goes to the model as a failed tool result. Nothing is raised.

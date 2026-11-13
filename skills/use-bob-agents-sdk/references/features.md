# Features

Short, working patterns for each feature. All names are imported from `bob_agent_sdk`.

## Contents

- [Custom tools](#custom-tools)
- [Tool approval](#tool-approval)
- [Custom role and tool groups](#custom-role-and-tool-groups)
- [Subagents](#subagents)
- [Skills](#skills)
- [Hooks](#hooks)
- [Typed output](#typed-output)
- [Limits and usage](#limits-and-usage)
- [Multi-turn, interrupt, resume](#multi-turn-interrupt-resume)
- [Isolation between sessions](#isolation-between-sessions)
- [Files the SDK writes](#files-the-sdk-writes)

## Custom tools

A tool is an async Python function that runs in the agent's own process, so it can use local variables and connections.

```python
@tool("get_order_status", "Look up the status of an order by its ID", {"order_id": str})
async def get_order_status(args: dict) -> str:
    return ORDERS.get(args["order_id"], "unknown order")


options = BobAgentOptions(
    mcp_servers={"shop": create_sdk_mcp_server("shop", tools=[get_order_status])},
    auto_approve=True,
)
```

- The schema is a `{name: python type}` mapping (all required) or a full JSON Schema dict.
- Return a string, or an MCP result dict such as `{"content": [...], "isError": True}`.
- **Bob asks approval for these tools.** Set `auto_approve=True` or `can_use_tool`, or every call fails.
- A mode that should use the tools needs the `mcp` tool group. `groups=["mcp"]` alone gives an agent that can only use your tools.
- Bob's name for the tool is `mcp__<key>__<tool>`, where `<key>` is the key in the `mcp_servers` dict: here `mcp__shop__get_order_status`. Hook matchers use this name.
- External MCP servers go in the same dict: `{"command": "npx", "args": [...], "env": {...}}` or `{"type": "http", "url": "...", "headers": {...}}`.

## Tool approval

Bob asks for approval only for calls that its own settings do not already allow. Three choices:

| Setting | Effect |
|---|---|
| neither | The SDK rejects the call. The stream shows a failed `ToolResultMessage`. |
| `auto_approve=True` | Bob approves by its safety rules |
| `can_use_tool=fn` | Your function decides |

```python
async def can_use_tool(tool: ToolUseBlock, context: ToolPermissionContext) -> PermissionResult:
    if tool.kind == "execute" and "rm " in tool.input.get("command", ""):
        return PermissionResultDeny()
    return PermissionResultAllow()  # PermissionResultAllow(always=True) remembers it
```

`can_use_tool` does not see calls that Bob approves on its own. To see or block every call, use a `PreToolUse` hook.

## Custom role and tool groups

A `ModeDefinition` is the system prompt of the agent. Leave a tool group out to take those tools away.

```python
reviewer = ModeDefinition(
    name="Security Reviewer",
    role_definition="You are a security reviewer. You read code and report risks.",
    custom_instructions="Report at most three findings.",
    groups=["read"],
)
options = BobAgentOptions(mode=reviewer)
```

Tool groups: `read`, `edit`, `execute`, `mcp`, `skill`, `todo`, `subagent`, `mode`. The default is all of them.

`role_definition` and `custom_instructions` are guidance. The model usually follows them, but can ignore them. Enforce a hard rule in code: remove a tool group, or block the call in a hook.

## Subagents

```python
options = BobAgentOptions(
    agents={
        "haiku-writer": AgentDefinition(
            description="Writes one haiku about a given topic",  # tells Bob when to use it
            prompt="You write exactly one haiku about the topic you are given.",
            groups=["read"],
            max_turns=3,
        ),
    },
)
```

Other fields: `allow_tools`, `deny_tools` (Bob tool names), `model_tier`. Names use letters, digits, `-` and `_`. The main agent needs the `subagent` tool group.

## Skills

```python
options = BobAgentOptions(skills=["./skills/release-notes"])
```

Each path is a folder with a `SKILL.md`. The folder name is the skill name: lowercase letters, digits and single dashes. The agent needs the `skill` tool group.

## Hooks

A hook is an async function that Bob calls at a fixed point. It receives Bob's event as a dict and returns Bob's hook output as a dict, or `None`.

```python
async def block_rm(event: dict) -> dict | None:
    if "rm " in event["tool_input"].get("command", ""):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Deleting files is not allowed.",
            }
        }


options = BobAgentOptions(
    hooks={"PreToolUse": [HookMatcher(matcher="^execute_command$", hooks=[block_rm])]},
)
```

| Event | Payload fields | What a hook can return |
|---|---|---|
| `PreToolUse` | `tool_name`, `tool_input`, `tool_use_id` | Deny (as above), or `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {...}}}` to change the input |
| `PostToolUse` | the above plus `tool_response` | `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}` |
| `UserPromptSubmit` | `prompt` | `{"decision": "block", "reason": "..."}` |
| `PermissionRequest` | as `PreToolUse` | `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow" \| "deny", "message": "..."}}}` |
| `Stop` | `last_assistant_message` | nothing |
| `SessionStart`, `SessionEnd`, `PreCompact`, `PostCompact`, `PermissionResolved`, `Notification` | see Bob's hooks documentation | mostly nothing |

- `matcher` is a regular expression on the tool name. Leave it out to match every tool. Bob tool names include `execute_command`, `write_file`, `read_file`, `apply_diff`, `spawn_subagent`, `use_skill`, `switch_mode`, and `mcp__<key>__<tool>` for MCP tools.
- `event["tool_input"]` is the tool's arguments as a dict, with typed values. For a custom tool it is what the handler would receive.
- `HookMatcher(timeout=60.0)` limits each callback.
- `hooks={}` turns hooks on without callbacks. That is the way to fill `ToolUseBlock.name`.
- Hooks need `curl` and a POSIX shell. They do not work on Windows.

A hook on a custom tool:

```python
async def cap_reorder(event: dict) -> dict | None:
    if event["tool_input"]["quantity"] > 100:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Reorders of more than 100 units are not allowed.",
            }
        }


hooks = {"PreToolUse": [HookMatcher(matcher="^mcp__shop__reorder$", hooks=[cap_reorder])]}
```

What a denied call looks like:

- The tool does not run. The model receives the reason and continues.
- The stream has no `ToolUseBlock` for it, only a `ToolResultMessage` with `status == "failed"` and the reason in `output`.
- So to log every call, including denied ones, print inside a `PreToolUse` hook. Do not rely on `ToolUseBlock` alone.

## Typed output

Use it whenever code reads the agent's answer. Do not parse JSON out of the text.

```python
class Review(BaseModel):  # from pydantic
    verdict: Literal["approve", "request_changes"]
    reasons: list[str]


async for message in query(prompt="Review app.py", options=BobAgentOptions(output_format=Review)):
    if isinstance(message, ResultMessage):
        review: Review = message.structured_output  # a Review instance
```

- `output_format` is a Pydantic model class (the answer is an instance) or `{"type": "json_schema", "schema": {...}}` (the answer is plain data).
- The SDK validates the answer. The model must correct output that does not match, so constraints in the model (`Field(ge=1)`, `Literal[...]`, `max_length`) are enforced.
- If the agent never reports, the run ends with `stop_reason == "error_max_structured_output_retries"` and `structured_output` is `None`. Check `is_error` before reading it.
- It works with every mode and needs no `auto_approve`. A mode with the `mcp` tool group reports through a tool that the SDK adds. A mode without it, such as `groups=["read"]`, reports as JSON at the end of its reply, and the SDK reads it.
- Do not add `mcp` to a mode only for typed output. The `mcp` group also gives the agent every MCP server that is configured in Bob. If the agent needs `mcp` for your own tools, set `disable_mcp=True` to keep Bob's configured servers out.
- With `BobSDKClient`, every `query()` gets its own output.

## Limits and usage

```python
options = BobAgentOptions(max_turns=10, max_cost=0.50)
```

- `max_turns` counts model requests in one `query()`. A tool call and the answer after it are two requests.
- A limit ends the run with `stop_reason` `max_turns` or `max_cost` and `is_error=True`.
- The SDK checks between model requests, so a run can pass a limit by one request.
- When the cost budget is used up, the next `query()` returns a `max_cost` result without calling Bob.
- Every `ResultMessage` has `num_turns`, `cost`, `total_cost` and `model`. Costs are in Bobcoins. There are no token counts.

## Multi-turn, interrupt, resume

```python
async with BobSDKClient(options) as client:
    await client.query("Find the slowest test")
    async for message in client.receive_response():
        ...

    await client.query("Now make it faster")  # same session, Bob remembers
    async for message in client.receive_response():
        ...

    await client.set_mode("plan")  # between turns
    session_id = client.session_id

# Later, in a new process. Use the same cwd.
async with BobSDKClient(BobAgentOptions(resume=session_id)) as client:
    ...
```

To interrupt, call `await client.interrupt()` from another task. The turn then ends with `stop_reason == "cancelled"`.

## Isolation between sessions

Bob reads subagents, skills and modes from the workspace, so sessions that share a `cwd` see each other's.

| `isolation` | Use it when |
|---|---|
| `"shared"` (default) | One session per workspace, or sessions with the same definitions |
| `"enforced"` | Several sessions in one workspace must not use each other's subagents, skills or modes. File edits are still shared. Needs hooks. |
| `"worktree"` | Sessions run in parallel on separate tasks. Each gets a git worktree and branch. Needs a git repository with a commit. |

```python
async with BobSDKClient(BobAgentOptions(cwd=repo, isolation="worktree")) as client:
    print(client.worktree.path, client.worktree.branch)
    ...
# No work left: the worktree is removed. Work left: it is kept. Merge the branch, then:
remove_worktree(client.worktree)  # force=True discards the work
```

A worktree starts from the last commit: the agent does not see uncommitted changes or git-ignored files such as `.env`.

## Files the SDK writes

`ModeDefinition`, `agents`, `skills` and `hooks` write files under `<cwd>/.bob` for the session and remove them when it closes. Tell the user when an agent will do this in their repository. If a session is killed, the next session cleans up, or call `cleanup_workspace(cwd)`.

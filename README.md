# bob-agent-sdk

A Python SDK for building custom agents on [IBM Bob Shell](https://bob.ibm.com/docs/shell). 

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Login](#login)
- [One prompt: `query()`](#one-prompt-query)
- [A conversation: `BobSDKClient`](#a-conversation-bobsdkclient)
- [Custom tools](#custom-tools)
- [Hooks](#hooks)
- [Typed output](#typed-output)
- [Isolation between sessions](#isolation-between-sessions)
- [Options](#options)
- [Examples](#examples)
- [How it works](#how-it-works)

## Requirements

- Python 3.10 or later, managed with [uv](https://docs.astral.sh/uv/)
- Bob Shell 2.1 or later on your `PATH` (check with `bob --version`)
- A Bob API key. See [Login](#login).


## Skill for coding agents

The [`use-bob-agents-sdk`](skills/use-bob-agents-sdk/SKILL.md) skill teaches a coding agent, such as Claude Code or Bob, how to write agents with this SDK. Install it with the [skills](https://github.com/vercel-labs/skills) command:

```bash
npx skills add ajshedivy/bob-agent-sdk
```

Then ask your coding agent to build an agent with the Bob Agent SDK. The skill installs the SDK from source into your project, so you do not need to clone this repository first.

## Install

```bash
uv sync
```

To use the SDK from another uv project:

```bash
uv add /path/to/bob-agent-sdk
```

## Quick start

```python
import asyncio
from bob_agent_sdk import query


async def main():
    async for message in query(prompt="What does this project do?"):
        print(message)


asyncio.run(main())
```

Run it with `uv run --env-file .env my_agent.py`, where `.env` holds your `BOB_API_KEY`.

## Login

The SDK uses a Bob API key. Create one in [Bobalytics](https://bob.ibm.com/docs/ide/features/bobalytics), then set it in the `BOB_API_KEY` environment variable:

```bash
# add to .env file (git ignored)
echo "BOB_API_KEY=your-key" > .env
uv run --env-file .env my_agent.py

# export directly
export BOB_API_KEY=your-key
uv run my_agent.py
```

To use the SSO login that `bob` already saved on your machine, set `BOB_USE_SSO=1` instead:

```bash
BOB_USE_SSO=1 uv run my_agent.py
```

Without either, the SDK raises `AuthenticationError`.

## One prompt: `query()`

`query()` starts Bob, sends one prompt, yields the messages, and stops Bob.

```python
from bob_agent_sdk import AssistantMessage, BobAgentOptions, TextBlock, query

options = BobAgentOptions(
    cwd="/path/to/project",
    mode="ask",  # agent (default), plan, ask, or a custom mode slug
)

async for message in query(prompt="Explain the build setup", options=options):
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                print(block.text)
```

## A conversation: `BobSDKClient`

`BobSDKClient` keeps one Bob session open. Bob remembers earlier turns. You can interrupt a turn and change the mode.

```python
from bob_agent_sdk import BobSDKClient

async with BobSDKClient() as client:
    await client.query("Find the slowest test")
    async for message in client.receive_response():
        print(message)

    await client.query("Now make it faster")  # same session
    async for message in client.receive_response():
        print(message)
```

| Member | What it does |
|---|---|
| `query(prompt)` | Sends a prompt and returns immediately. |
| `receive_response()` | Yields messages until the turn ends with a `ResultMessage`. |
| `receive_messages()` | Yields all messages until the session closes. |
| `interrupt()` | Stops the current turn. The turn ends with `stop_reason == "cancelled"`. |
| `set_mode(mode)` | Switches mode, for example from `plan` to `agent`. |
| `session_id` | The ID to pass as `BobAgentOptions(resume=...)` later. |

## Messages

| Message | Meaning |
|---|---|
| `AssistantMessage` | Text (`TextBlock`), reasoning (`ThinkingBlock`) and tool calls (`ToolUseBlock`) from Bob. |
| `ToolResultMessage` | The outcome of one tool call: `status`, `output`, and raw `content` (file edits appear as diffs). |
| `PlanMessage` | Bob's current todo list. |
| `SystemMessage` | Session metadata. The first one has `subtype == "init"`. |
| `ResultMessage` | The end of a turn: `stop_reason`, `result` (Bob's last text), `structured_output`, `is_error`, `num_turns`, `cost`, `total_cost`, `model`, `session_id`, `duration_ms`, `num_tool_calls`. |
| `StreamEvent` | A raw update, such as a text delta. Only sent when `include_partial_messages=True`. |

A `ToolUseBlock` has a `title` (for example the shell command) and a `kind` (`read`, `edit`, `execute`, ...). Its `name` (for example `execute_command`) is only set when hooks are on. See [Hooks](#hooks).

## Custom tools

A tool is an async Python function. It runs in your process, so it can use your variables and connections.

```python
from bob_agent_sdk import BobAgentOptions, create_sdk_mcp_server, query, tool


@tool("get_order_status", "Look up the status of an order", {"order_id": str})
async def get_order_status(args):
    return ORDERS.get(args["order_id"], "unknown order")


options = BobAgentOptions(
    mcp_servers={"shop": create_sdk_mcp_server("shop", tools=[get_order_status])},
    auto_approve=True,
)
```

The SDK serves these tools to Bob as an MCP server on a loopback port, protected by a random token. You can also add external MCP servers:

```python
mcp_servers = {
    "files": {"command": "npx", "args": ["some-mcp-server"], "env": {"KEY": "value"}},
    "remote": {
        "type": "http",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer ..."},
    },
}
```

## Tool approval

Bob decides which tool calls need approval. Read-only tools, and everything allowed under `approval` in `~/.bob/settings/settings.json`, run without a question. For the other calls there are three choices:

- **No option set:** the SDK rejects the call. This is the safe default.
- **`auto_approve=True`:** Bob approves the call itself when its safety rules allow.
- **`can_use_tool=...`:** your function decides.

```python
from bob_agent_sdk import PermissionResultAllow, PermissionResultDeny


async def can_use_tool(tool, context):
    if tool.kind == "execute" and "rm " in tool.input.get("command", ""):
        return PermissionResultDeny()
    return PermissionResultAllow()  # or PermissionResultAllow(always=True)


options = BobAgentOptions(can_use_tool=can_use_tool)
```

## A custom agent: role, instructions and tools

A `ModeDefinition` gives the agent its own role and limits its tools. The role and instructions go into Bob's system prompt. A tool group that you leave out is not available to the agent.

```python
from bob_agent_sdk import BobAgentOptions, ModeDefinition

reviewer = ModeDefinition(
    name="Security Reviewer",
    role_definition="You are a security reviewer. You read code and report risks.",
    custom_instructions="Report at most three findings.",
    groups=["read"],  # no edit, no execute
)
options = BobAgentOptions(mode=reviewer)
```

The tool groups are `read`, `edit`, `execute`, `mcp`, `skill`, `todo`, `subagent` and `mode`. The default is all of them.

## Subagents

An `AgentDefinition` is a subagent that Bob can start with its `spawn_subagent` tool.

```python
from bob_agent_sdk import AgentDefinition, BobAgentOptions

options = BobAgentOptions(
    agents={
        "haiku-writer": AgentDefinition(
            description="Writes one haiku about a given topic",
            prompt="You write exactly one haiku about the topic you are given.",
            groups=["read"],
            max_turns=3,
        ),
    },
)
```

A subagent can also set `allow_tools`, `deny_tools` (lists of Bob tool names) and `model_tier`.

### Seeing a subagent's work

Bob does not stream a subagent's work over ACP. The SDK reports it from two other sources, as the same message types as the agent's own work. A subagent's `AssistantMessage` and `ToolResultMessage` have `subagent` set to a `SubagentRef`: the `spawn_subagent` call that started it (`tool_use_id`), its `name`, and when Bob recorded the message (`at`).

| When | What the SDK reports |
|---|---|
| While the subagent runs | Its tool calls and successful results, from Bob's hook events. Needs hooks (`hooks={}` is enough). |
| At the end of the turn, before the `ResultMessage` | From Bob's task export, everything not reported yet: its text, failed results, and calls that could not be tied to a subagent. Then one `SubagentResultMessage` per subagent, with its usage. |

Each call and result is reported once. Bob's hook events do not say which subagent made a call. While several subagents run at the same time, the SDK reports a call only when it knows whose it is, so the work of parallel subagents can wait for the end of the turn. A subagent's tool call can need approval: `ToolPermissionContext.subagent` then names the subagent, as far as it is known. The commands Bob runs can reach the SDK's hook server too, so a command can add a tool call or result that looks like a subagent's while a subagent runs. Treat this record as a log of what the session did, not as proof.

## Skills

A skill is a folder with a `SKILL.md` file. Pass skill folders to make them available for the session, in addition to the skills that Bob already has.

```python
options = BobAgentOptions(skills=["./skills/release-notes", "/shared/skills/db2-review"])
```

The folder name is the skill name. Bob accepts lowercase letters, digits and single dashes.

## Isolation between sessions

Bob reads subagents, skills and modes from the workspace, so sessions that share a workspace can see each other's. The `isolation` option controls this.

| Value | Where the session works | Other sessions' subagents, skills and modes |
|---|---|---|
| `"shared"` (default) | In `cwd` | Visible and usable |
| `"enforced"` | In `cwd` | Refused on use, or hidden. See below. |
| `"worktree"` | In a git worktree of its own, on a new branch | Not visible |

### `isolation="enforced"`

```python
options = BobAgentOptions(isolation="enforced", agents={"poet": poet})
```

The session cannot use a subagent, skill or mode that only another SDK session defined. Bob still lists them to the model. If the model tries one, the call fails with a reason such as `Subagent 'reviewer' belongs to another SDK session in this workspace.`, and Bob carries on.

- **With a `ModeDefinition`**, the SDK also sets the mode's `allowed_subagents` to your own subagents, the project's own subagents and Bob's built-in ones. Bob then does not show the other subagents to the model at all. Set `allowed_subagents` yourself to override this list.
- **Not restricted:** the project's own files in `.bob`, Bob's built-in modes and subagents, and anything that both sessions define the same way.
- **File edits are still shared.** Two sessions can change the same file.
- **Needs hooks,** so it works on macOS and Linux only.

### `isolation="worktree"`

```python
options = BobAgentOptions(cwd="/repos/shop", isolation="worktree")

async with BobSDKClient(options) as client:
    print(client.worktree.path)  # ~/.bob-agent-sdk/worktrees/shop-3f9a1c2e
    print(client.worktree.branch)  # bob/3f9a1c2e
    ...
```

The SDK creates a git worktree from the current commit and runs the session there. Nothing is written to your checkout. With `query()`, the first `SystemMessage` (`subtype == "init"`) carries the path and branch in `data["worktree"]`.

When the session closes:

- **No work left:** the SDK removes the worktree and the branch. `client.worktree.removed` is `True`.
- **Work left** (changed files, new files or new commits): the SDK keeps both. Take the work with `git merge <branch>`.

Remove a kept worktree when you are done with it:

```python
from bob_agent_sdk import remove_worktree

remove_worktree(client.worktree)  # refuses if work would be lost
remove_worktree(client.worktree, force=True)  # discards the work
```

Things to know:

- **The worktree starts from the last commit.** The agent does not see your uncommitted changes.
- **Ignored files are missing,** for example `.env` and `node_modules`.
- **It needs a git repository** with at least one commit.
- **To resume the session later,** pass the worktree path as `cwd` and leave `isolation` at its default.
- **If a session is killed,** its worktree stays. The next worktree session in that repository removes it if it holds no work.

## Hooks

A hook is an async Python function that Bob calls at a fixed point of its work. Hooks see every tool call, also the calls that Bob approves on its own. A hook can block a call, change its input, or add context for the model.

```python
from bob_agent_sdk import BobAgentOptions, HookMatcher


async def block_rm(event):
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

The function receives Bob's event as a dict and returns Bob's hook output as a dict, or `None` for no decision. `matcher` is a regular expression on the tool name.

| Event | When Bob calls it | What a hook can do |
|---|---|---|
| `PreToolUse` | Before a tool call | Block it, change `tool_input`, add context |
| `PostToolUse` | After a tool call succeeds | Add context beside the result |
| `PermissionRequest` | Before Bob asks for approval | Allow or deny the call |
| `PermissionResolved` | After an approval is answered | Observe |
| `UserPromptSubmit` | Before a prompt is processed | Block it, add context |
| `SessionStart` | When the session starts or resumes | Add context |
| `Stop` | When a turn ends | Observe |
| `PreCompact`, `PostCompact`, `Notification`, `SessionEnd` | See Bob's hooks documentation | Observe |

Setting `hooks` to any value other than `None`, also `{}`, turns hooks on. Tool names in `ToolUseBlock.name` come from hooks, so `hooks={}` is the way to get names without callbacks.

Hooks need `curl` and a POSIX shell.

## Limits and usage

```python
options = BobAgentOptions(
    max_turns=10,  # model requests per turn
    max_cost=0.50,  # Bobcoins for the session
)
```

When a limit is reached, the SDK stops the turn. The `ResultMessage` then has `stop_reason` `max_turns` or `max_cost`, and `is_error` is `True`. The SDK checks the limits each time Bob starts a tool call, so a turn can pass a limit by one model request. When the cost budget is used up, the SDK does not start another turn.

Every `ResultMessage` reports what the turn used:

| Field | Meaning |
|---|---|
| `num_turns` | Model requests in this turn, including those of subagents |
| `cost` | Cost of this turn, in Bobcoins |
| `total_cost` | Cost of the session so far, including subagents |
| `usage` | Bob's raw cost record, for example `contextTokens` |
| `model` | The model that Bob used |

## Typed output

Set `output_format` to get the agent's answer as data. Pass a Pydantic model class, and the answer arrives as an instance of it:

```python
from typing import Literal
from pydantic import BaseModel
from bob_agent_sdk import BobAgentOptions, ResultMessage, query


class Review(BaseModel):
    verdict: Literal["approve", "request_changes"]
    reasons: list[str]


async for message in query(prompt="Review _http.py", options=BobAgentOptions(output_format=Review)):
    if isinstance(message, ResultMessage):
        review: Review = message.structured_output
        print(review.verdict)
```

A JSON Schema works too, in the same form as the Claude Agent SDK. The answer is then plain data:

```python
options = BobAgentOptions(
    output_format={"type": "json_schema", "schema": {"type": "object", "properties": {...}}},
)
```

The SDK checks the answer against the schema:

- **Invalid output is corrected.** Output that does not match goes back to the model with the reasons, and the model tries again.
- **A missing answer is asked for.** The SDK reminds the agent up to two times. After that the run ends with `stop_reason == "error_max_structured_output_retries"` and `is_error=True`.
- **`ResultMessage.result` still has Bob's last text.**

How the agent reports the answer depends on its mode:

| Mode | How the answer is reported |
|---|---|
| Has the `mcp` tool group (the built-in modes, or a `ModeDefinition` that lists it) | Through a `structured_output` tool that the SDK adds. Its input is your schema. It needs no `auto_approve` or `can_use_tool`. |
| A `ModeDefinition` without `mcp`, for example `groups=["read"]` | As JSON at the end of the reply. The SDK reads it from the text. |

The SDK does not add the `mcp` group for you. That group also gives the agent every MCP server that is configured in Bob, so a read-only reviewer stays read-only. The tool is the more reliable of the two ways. To use it without Bob's own MCP servers, list `mcp` and set `disable_mcp=True`.

## Options

| Option | Purpose |
|---|---|
| `cwd` | Workspace directory. Default: the current directory. |
| `mode` | Starting mode: `agent`, `plan`, `ask`, a custom mode slug, or a `ModeDefinition`. |
| `agents` | Subagents, by name. |
| `skills` | Skill folders to attach. |
| `hooks` | Python callbacks for Bob's hook events. |
| `hook_scope` | `workspace` (default) or `global`: where the SDK registers its hook command. See [Files in your workspace](#files-in-your-workspace). |
| `isolation` | `shared`, `enforced` or `worktree`. See above. |
| `max_turns`, `max_cost` | Limits. See above. |
| `output_format` | A Pydantic model class or a JSON Schema for the answer. See above. |
| `team_id` | Bob team, for accounts with more than one team. |
| `mcp_servers` | In-process tools and external MCP servers. |
| `can_use_tool`, `auto_approve` | Tool approval. See above. |
| `resume` | Session ID of an earlier session to continue. |
| `include_partial_messages` | Also yield a `StreamEvent` for each raw update. |
| `api_key` | Bob API key. Default: the `BOB_API_KEY` environment variable. See [Login](#login). |
| `trust`, `accept_license` | Pass `--trust` or `--accept-license` to Bob. |
| `disable_mcp`, `disable_subagents` | Turn off Bob's own MCP servers or subagents. |
| `cli_path`, `env`, `log_level`, `stderr` | Control the Bob process. |

## Errors

All errors extend `BobSDKError`.

| Error | Cause |
|---|---|
| `CLINotFoundError` | `bob` is not on `PATH`. |
| `AuthenticationError` | There is no API key, or Bob did not accept the login. |
| `ACPError` | Bob rejected a request. The message says why. |
| `ProcessError` | Bob exited unexpectedly. See `exit_code` and `stderr`. |

## Examples

| File | Shows |
|---|---|
| [`examples/quick_start.py`](examples/quick_start.py) | `query()`, options, and reading tool calls. |
| [`examples/multi_turn.py`](examples/multi_turn.py) | Several turns, an interrupt, and resuming a session. |
| [`examples/custom_tools.py`](examples/custom_tools.py) | Python functions as tools. |
| [`examples/tool_permission_callback.py`](examples/tool_permission_callback.py) | Approving and denying tool calls. |
| [`examples/plan_then_build.py`](examples/plan_then_build.py) | Plan mode, then agent mode. |
| [`examples/custom_agent.py`](examples/custom_agent.py) | A read-only reviewer with its own role. |
| [`examples/subagents.py`](examples/subagents.py) | Subagents defined in Python. |
| [`examples/skills.py`](examples/skills.py) | Attaching a skill folder. |
| [`examples/hooks.py`](examples/hooks.py) | Blocking and rewriting tool calls with hooks. |
| [`examples/isolation.py`](examples/isolation.py) | Enforced and worktree isolation. |
| [`examples/typed_output.py`](examples/typed_output.py) | The answer as a Pydantic model. |
| [`examples/limits_and_usage.py`](examples/limits_and_usage.py) | Turn and cost limits, and usage numbers. |

```bash
uv run --env-file .env examples/quick_start.py
```

## Agno and AgentOS

The [`agent`](agent/README.md) folder has `BobAgent`, an adapter that runs a Bob agent as an Agno external agent. Use it to call Bob through Agno's `run()` and `print_response()`, or to serve it with AgentOS.


## How it works

The SDK starts `bob acp` and talks to it with the [Agent Client Protocol](https://agentclientprotocol.com) (ACP), through the official [ACP Python SDK](https://github.com/agentclientprotocol/python-sdk). ACP is the interface that editors use to drive an interactive Bob session. It supports several turns, tool approval and interrupts.

`bob acp` does not accept the options of the non-interactive `bob run` command, such as `--max-turns`, `--max-cost` and `--disable-tool-groups`, and it reports no usage. The SDK provides them in other ways:

| Feature | How the SDK provides it |
|---|---|
| Usage and cost | Bob saves the cost with each task. The SDK reads it through Bob's ACP extension `_bob/task/export`. |
| `max_turns`, `max_cost` | The SDK reads the usage when Bob starts a tool call, and cancels the turn at the limit. |
| Role and tool groups | The SDK writes a custom mode to `.bob/plugins/bob-agent-sdk-<id>/custom_modes.yaml`. |
| Subagents | The SDK writes `.bob/agents/<name>.md`. |
| Skills | The SDK links each skill folder as `.bob/skills/<name>`. |
| Hooks and tool names | The SDK adds one hook command per event to `.bob/settings.json`, or with `hook_scope="global"` to Bob's global `~/.bob/settings/settings.json`. The command sends the event to your Python process. |
| Team | The SDK sets Bob's `team` session option. |

### Files in your workspace

The rows for role, subagents, skills and hooks write files to the `.bob` folder of the workspace (`cwd`). The SDK writes them only when you use the feature, and removes them when the session closes.

Several sessions can use one workspace at the same time:

- **Each session has its own mode.** Sessions do not share mode files.
- **Sessions share a subagent, a skill or the hook entry** when they define it the same way. The file stays until the last session that uses it closes.
- **A session is refused** when it defines a subagent, a skill or a mode slug differently from a session that is running. The error says which one. The refused session writes nothing.
- **Hook events go only to the session that owns the Bob process.** Two sessions with hooks do not see each other's events.
- **Bob shows every session all subagents, skills and modes in the workspace**, also those of other sessions. Bob has no per-session scope for them. Use [`isolation`](#isolation-between-sessions) to restrict this.

The SDK never overwrites or deletes your own files:

- It refuses to start if a subagent file or skill with the same name already exists.
- It keeps your other settings in `.bob/settings.json`. When nothing else in the file changed during the session, the file gets its exact bytes back.
- It deletes a file only if the file still has the content that the SDK wrote.

Bob reads subagents only from the workspace, so they must be there. Hooks need not be: with `hook_scope="global"` the SDK registers its hook command in Bob's global settings instead, and a session that uses only hooks writes nothing to the workspace. The command does nothing in Bob sessions that the SDK did not start. Sessions in all workspaces share the one entry, and the last one to close removes it. Bob uses global hooks, like workspace hooks, only in a trusted folder (see `trust`).

If a session is killed before it can clean up, its files stay. The next SDK session that starts or closes in that workspace removes them. You can also remove them yourself:

```python
from bob_agent_sdk import cleanup_workspace

cleanup_workspace("/path/to/workspace")
```

The SDK tracks its files outside the workspace, in `~/.bob-agent-sdk/workspaces/<key>/`, and removes that folder when no session is left. Its lock files are in your temporary directory. So Bob, `find` and `git status` never show the SDK's bookkeeping. Earlier versions kept it in `.bob/.bob-agent-sdk/` of the workspace; the SDK still reads that folder, cleans up after the sessions it lists, and removes it.

The locking works on Windows, macOS and Linux. On Windows, hooks are not supported, because the hook command needs a POSIX shell. Skills use symbolic links, which Windows allows only in Developer Mode or for administrators. Nothing has been run on Windows yet.

## Limits

- **No model selection for the main agent.** Bob does not offer it over ACP. A subagent can set `model_tier`.
- **No token counts.** Bob 2.1.0 saves the cost and the context size for this account, not input and output tokens. `usage` passes on whatever Bob saves.
- **Limits can be passed by one model request**, because the SDK checks them between requests.
- **`team_id` is not tested against a real account** with more than one team.

## Development

```bash
uv sync
uv run pytest          # tests
uv run ruff format .   # format
uv run ruff check .    # lint
uv run mypy            # type check
```

The tests use `tests/fake_bob.py`, a stand-in for `bob acp`, so they need no Bob login.

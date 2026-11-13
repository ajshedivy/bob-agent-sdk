---
name: use-bob-agents-sdk
description: Write custom AI agents in Python with the Bob Agent SDK (bob_agent_sdk), which drives IBM Bob Shell. Use when the user wants to build, script or automate an agent on Bob or Bob Shell; mentions bob_agent_sdk, BobAgentOptions, BobSDKClient or BobAgent; wants a Bob agent with custom tools, hooks, subagents, skills, a custom role, tool approval, turn or cost limits, or session isolation; or wants to serve a Bob agent with Agno or AgentOS. Covers installing the SDK from source (it is not on PyPI), login, and the patterns for each feature.
---

# Use the Bob Agent SDK

`bob_agent_sdk` is a Python SDK for building agents on IBM Bob Shell. An agent is a Python file: it sends a prompt, and Bob reads and edits files, runs commands and calls tools. The API mirrors the Claude Agent SDK (`query()`, a client class, options, typed messages), but the names and several behaviors differ. Do not assume Claude Agent SDK options exist here.

## 1. Set up

The SDK is not on PyPI. Install it from source with the bundled script, run from the project folder:

```bash
python3 <this skill's folder>/scripts/setup_sdk.py
```

The script clones the SDK to `~/.bob-agent-sdk/sdk`, installs it into the project in editable mode, and checks for Bob Shell and a login. It uses uv when uv is installed (it adds the SDK to `pyproject.toml`, creating one if needed), and a `.venv` with pip otherwise. Options: `--project DIR`, `--sdk-dir DIR`, `--update` (pull the latest source), `--no-uv`.

Read its output. Lines that start with `MISSING` must be fixed before an agent can run:

- **Bob Shell:** `bob` 2.1 or later must be on `PATH`. The SDK cannot install it. Ask the user to install it.
- **Login:** the SDK needs a Bob API key in `BOB_API_KEY`. Never write a key into source code. Ask the user to put `BOB_API_KEY=<key>` in a `.env` file, and make sure `.env` is in `.gitignore`. Keys are created in Bobalytics: https://bob.ibm.com/docs/ide/features/bobalytics. To use the SSO login that `bob` already saved on the machine, set `BOB_USE_SSO=1` instead.

Manual setup, if the script cannot be used:

```bash
git clone https://github.com/ajshedivy/bob-agent-sdk.git ~/.bob-agent-sdk/sdk
uv add --editable ~/.bob-agent-sdk/sdk                # with uv
python3 -m venv .venv && .venv/bin/pip install -e ~/.bob-agent-sdk/sdk    # without uv
```

## 2. Write the agent

Start from `assets/agent_template.py`: copy it into the project and adapt it. The core pattern:

```python
import asyncio
from bob_agent_sdk import AssistantMessage, BobAgentOptions, ResultMessage, TextBlock, query


async def main() -> None:
    options = BobAgentOptions(cwd=".", auto_approve=True, max_turns=20)
    async for message in query(prompt="Summarize this project", options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
        elif isinstance(message, ResultMessage):
            print(message.stop_reason, message.cost)


asyncio.run(main())
```

Choose the entry point:

- **`query()`**: one prompt in a new session. Use it for scripts and batch jobs.
- **`BobSDKClient`**: several turns in one session, with `interrupt()` and `set_mode()`. Use it for chat-like or multi-step agents.

Then add what the task needs. Read the reference for a feature before using it:

| The agent needs | Use | Read |
|---|---|---|
| Its own role, or fewer tools | `mode=ModeDefinition(...)` | [features.md](references/features.md#custom-role-and-tool-groups) |
| Python functions as tools | `@tool`, `create_sdk_mcp_server` | [features.md](references/features.md#custom-tools) |
| To allow or block tool calls | `auto_approve`, `can_use_tool`, hooks | [features.md](references/features.md#tool-approval) |
| To see, block or rewrite every tool call | `hooks` | [features.md](references/features.md#hooks) |
| Helper agents | `agents={...}` | [features.md](references/features.md#subagents) |
| Skill folders | `skills=[...]` | [features.md](references/features.md#skills) |
| A turn or cost cap, or usage numbers | `max_turns`, `max_cost`, `ResultMessage` | [features.md](references/features.md#limits-and-usage) |
| Follow-up prompts, interrupt, resume | `BobSDKClient` | [features.md](references/features.md#multi-turn-interrupt-resume) |
| Several sessions at once | `isolation` | [features.md](references/features.md#isolation-between-sessions) |
| To run under Agno or AgentOS | `BobAgent` | [agno.md](references/agno.md) |
| Every option, message field and error | | [api.md](references/api.md) |

The cloned repository has a runnable example for each feature in `~/.bob-agent-sdk/sdk/examples/`.

## 3. Rules that prevent common mistakes

- **Decide tool approval on purpose.** Without `auto_approve=True` or `can_use_tool`, the SDK rejects every tool call that Bob asks approval for, and the agent appears unable to edit files, run commands or use your custom tools. For an unattended agent, set `auto_approve=True` and limit the tools with a `ModeDefinition`. Tell the user which one you chose.
- **Limit tools with tool groups.** There are no per-tool allow lists on the main agent. Use `ModeDefinition(groups=[...])`: `read`, `edit`, `execute`, `mcp`, `skill`, `todo`, `subagent`, `mode`. A read-only agent is `groups=["read"]`.
- **`ToolUseBlock.name` is `None` unless hooks are on.** Use `block.title` and `block.kind` for display. Pass `hooks={}` when the code needs Bob's tool names.
- **A call that a hook denies has no `ToolUseBlock`.** It shows only as a failed `ToolResultMessage`. To log every call, print inside a `PreToolUse` hook.
- **Role text is guidance, not a guarantee.** The model can ignore `custom_instructions`. Enforce hard rules with tool groups or hooks.
- **The SDK is async only.** Wrap the entry point in `asyncio.run()`.
- **One turn at a time.** With `BobSDKClient`, read `receive_response()` to the end before the next `query()`.
- **Check how a turn ended.** `ResultMessage.is_error` is true when a limit or a Bob error stopped the turn. `stop_reason` says which.
- **No model choice, system prompt option or token counts.** The role comes from `ModeDefinition`. Costs are in Bobcoins.
- **Some features write to the workspace.** `ModeDefinition`, `agents`, `skills` and `hooks` put files under `<cwd>/.bob` for the session and remove them afterwards. Hooks, and `isolation="enforced"`, work on macOS and Linux only.
- **Do not call the `bob` command yourself** to work around the SDK. In particular, `bob run` hangs unless its standard input is closed.

## 4. Run and check

```bash
uv run --env-file .env agent.py        # with uv
set -a; . ./.env; set +a; .venv/bin/python agent.py     # without uv
```

Run the agent once with a small prompt before reporting it as done. Each run calls Bob and costs Bobcoins, so keep test prompts short. If it fails:

| Error or symptom | Cause and fix |
|---|---|
| `AuthenticationError: No Bob API key` | `BOB_API_KEY` is not set in the process. Pass `--env-file .env`, export it, or set `BOB_USE_SSO=1`. |
| `CLINotFoundError` | `bob` is not on `PATH`. |
| Tool calls end as `failed` with no output | Approval was rejected. See the first rule above. |
| `ModuleNotFoundError: bob_agent_sdk` | Run with the project's environment (`uv run`, or `.venv/bin/python`), or run the setup script again. |
| `... already exists. The SDK does not overwrite it.` | A subagent or skill with that name is already in `<cwd>/.bob`. Rename it, or call `cleanup_workspace(cwd)` if a killed session left it. |

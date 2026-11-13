"""A stand-in for `bob acp`. The prompt text selects the behaviour."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import acp
from acp import schema


def text(value: str) -> schema.TextContentBlock:
    return schema.TextContentBlock(type="text", text=value)


SCOPED = {"spawn_subagent": "name", "use_skill": "skill_name", "switch_mode": "mode_id"}


def run_hooks(cwd: str, event: str, fields: dict) -> dict:
    """Run the hook commands in `.bob/settings.json` the way Bob does."""
    settings = Path(cwd) / ".bob" / "settings.json"
    if not settings.exists():
        return {}
    payload = {"session_id": "session-1", "cwd": cwd, "hook_event_name": event, **fields}
    output: dict = {}
    for group in json.loads(settings.read_text()).get("hooks", {}).get(event, []):
        for handler in group["hooks"]:
            done = subprocess.run(
                ["sh", "-c", handler["command"]],
                input=json.dumps(payload).encode(),
                capture_output=True,
                cwd=cwd,
            )
            if done.stdout.strip():
                output = json.loads(done.stdout)
    return output


class FakeBob:
    def on_connect(self, conn):
        self.conn = conn
        self.cancelled = asyncio.Event()
        self.session = {}
        self.cwd = ""
        self.cost = 0.0
        self.scoped_calls = 0
        self.saved = []  # Assistant messages, as Bob's task store would hold them.

    def spend(self):
        """Bob records the cost when the model answers, before it saves the message."""
        self.cost = round(self.cost + 0.01, 6)

    def save_request(self, tool_call_id=None):
        calls = [{"id": tool_call_id, "name": "write_file"}] if tool_call_id else []
        self.saved.append({"role": "assistant", "data": {"toolCalls": calls}})

    async def ext_method(self, method, params):
        if method != "bob/task/export":
            raise acp.RequestError.method_not_found(method)
        task = {
            "id": params["sessionId"],
            "costs": {"cost": self.cost, "contextTokens": 100},
            "env": {"model": {"id": "fake-model"}},
        }
        return {"version": 1, "tasks": [{"task": task, "messages": self.saved}]}

    async def set_config_option(self, config_id, session_id, value, **kwargs):
        if (config_id, value) != ("team", "team-1"):
            raise acp.RequestError.invalid_params({"configId": config_id, "value": value})
        self.session["team"] = value
        return schema.SetSessionConfigOptionResponse(config_options=[])

    async def initialize(self, protocol_version, **kwargs):
        return schema.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_info=schema.Implementation(name="fake-bob", version="0"),
            agent_capabilities=schema.AgentCapabilities(
                field_meta=None
                if os.environ.get("FAKE_BOB_OLD")
                else {"bob/extensions": {"version": 1}}
            ),
        )

    async def new_session(self, cwd, mcp_servers=None, **kwargs):
        if os.environ.get("BOB_API_KEY") == "bad-key":
            raise acp.RequestError.auth_required()
        bob_dir = Path(cwd) / ".bob"
        self.cwd = cwd
        self.session = {
            "cwd": cwd,
            "mode": kwargs.get("bob/mode"),
            "mcp_servers": [server.name for server in mcp_servers or []],
            "mode_files": [
                json.loads(f.read_text()) for f in bob_dir.glob("plugins/*/custom_modes.yaml")
            ],
            "agent_files": {f.name: f.read_text() for f in bob_dir.glob("agents/*.md")},
            "skills": {f.parent.name: f.read_text() for f in bob_dir.glob("skills/*/SKILL.md")},
        }
        return schema.NewSessionResponse(session_id="session-1")

    async def resume_session(self, session_id, cwd, mcp_servers=None, **kwargs):
        self.session = {"resumed": session_id}
        self.cwd = cwd
        self.cost = 0.5  # The resumed task has a history.
        self.saved = [{"role": "assistant", "data": {}}] * 4
        return schema.ResumeSessionResponse()

    async def set_session_mode(self, session_id, mode_id, **kwargs):
        self.session["mode"] = mode_id
        return schema.SetSessionModeResponse()

    async def cancel(self, session_id, **kwargs):
        self.cancelled.set()

    async def prompt(self, session_id, prompt, **kwargs):
        command = prompt[0].text

        def send(update):
            return self.conn.session_update(session_id=session_id, update=update)

        def say(value):
            return send(
                schema.AgentMessageChunk(session_update="agent_message_chunk", content=text(value))
            )

        if command == "touch":
            (Path(self.cwd) / "made.txt").write_text("made by the agent")
            await say("touched")
        elif command.partition(":")[0] in SCOPED:
            # "spawn_subagent:reviewer" calls that tool with that name, through the hooks.
            tool, _, name = command.partition(":")
            tool_input = {SCOPED[tool]: name}
            self.scoped_calls += 1
            tool_id = f"s{self.scoped_calls}"  # Bob never uses a tool call ID twice.
            fields = {"tool_name": tool, "tool_input": tool_input, "tool_use_id": tool_id}
            decision = await asyncio.to_thread(run_hooks, self.cwd, "PreToolUse", fields)
            specific = decision.get("hookSpecificOutput", {})
            denied = specific.get("permissionDecision") == "deny"
            await send(
                schema.ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=tool_id,
                    title=tool,
                    status="failed" if denied else "completed",
                    raw_input=tool_input,
                    content=[
                        schema.ContentToolCallContent(
                            type="content",
                            content=text(specific.get("permissionDecisionReason", "ran")),
                        )
                    ],
                )
            )
        elif command == "work":
            # Five model requests. Each one writes a file through the hooks.
            for number in range(1, 6):
                tool_id = f"w{number}"
                tool_input = {"path": f"{number}.txt", "content": "hi"}
                hook_fields = {
                    "tool_name": "write_file", "tool_input": tool_input, "tool_use_id": tool_id,
                }  # fmt: skip
                decision = await asyncio.to_thread(run_hooks, self.cwd, "PreToolUse", hook_fields)
                specific = decision.get("hookSpecificOutput", {})
                tool_input = specific.get("updatedInput", tool_input)
                self.spend()
                call = dict(tool_call_id=tool_id, title="Writing file", kind="edit")
                await send(
                    schema.ToolCallStart(
                        session_update="tool_call", status="pending", raw_input=tool_input, **call
                    )
                )
                await asyncio.sleep(0.05)
                self.save_request(tool_id)
                denied = specific.get("permissionDecision") == "deny"
                output = (
                    specific.get("permissionDecisionReason") if denied else json.dumps(tool_input)
                )
                await send(
                    schema.ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=tool_id,
                        status="failed" if denied else "completed",
                        content=[
                            schema.ContentToolCallContent(type="content", content=text(output))
                        ],
                    )
                )
                if not denied:
                    fields = {**hook_fields, "tool_input": tool_input, "tool_response": "ok"}
                    await asyncio.to_thread(run_hooks, self.cwd, "PostToolUse", fields)
                await asyncio.sleep(0.05)
                if self.cancelled.is_set():
                    self.cancelled.clear()
                    return schema.PromptResponse(stop_reason="cancelled")
            self.spend()
            self.save_request()
            await say("finished")
            await asyncio.to_thread(
                run_hooks, self.cwd, "Stop", {"last_assistant_message": "finished"}
            )
        elif command == "echo":
            self.spend()
            self.save_request()
            await send(
                schema.AgentThoughtChunk(session_update="agent_thought_chunk", content=text("hm"))
            )
            state = {
                "argv": sys.argv[1:],
                "api_key": os.environ.get("BOB_API_KEY"),
                "session": self.session,
            }
            payload = json.dumps(state)
            await say(payload[:10])
            await say(payload[10:])
        elif command == "tool":
            call = dict(
                tool_call_id="t1", title="rm x", kind="execute", raw_input={"command": "rm x"}
            )
            await send(schema.ToolCallStart(session_update="tool_call", status="pending", **call))
            response = await self.conn.request_permission(
                session_id=session_id,
                tool_call=schema.ToolCallUpdate(**call),
                options=[
                    schema.PermissionOption(
                        option_id="allow", name="Allow once", kind="allow_once"
                    ),
                    schema.PermissionOption(option_id="always", name="Always", kind="allow_always"),
                    schema.PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
                ],
            )
            choice = getattr(response.outcome, "option_id", "cancelled")
            final = schema.ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id="t1",
                status="completed" if choice in ("allow", "always") else "failed",
                content=[schema.ContentToolCallContent(type="content", content=text(choice))],
            )
            await send(final)
            await send(final)  # Real Bob can repeat the final status.
            await say("done")
        elif command == "plan":
            await send(
                schema.AgentPlanUpdate(
                    session_update="plan",
                    entries=[schema.PlanEntry(content="step", priority="high", status="pending")],
                )
            )
        elif command == "hang":
            await say("working")
            await self.cancelled.wait()
            return schema.PromptResponse(stop_reason="cancelled")
        elif command == "die":
            sys.stderr.write("boom\n")
            sys.stderr.flush()
            os._exit(3)
        return schema.PromptResponse(stop_reason="end_turn")


if __name__ == "__main__":
    asyncio.run(acp.run_agent(FakeBob()))

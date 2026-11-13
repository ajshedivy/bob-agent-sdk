"""`BobAgent`: a Bob agent as an Agno external agent.

The adapter wraps `bob_agent_sdk.query()`, so a Bob agent can run through
Agno's `run()` and `print_response()`, and be served by AgentOS next to native
Agno agents.

    from bob_agent import BobAgent

    agent = BobAgent(name="Bob Coder", cwd="/path/to/project", auto_approve=True)
    agent.print_response("Summarize this project", stream=True)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agno.agents.base import BaseExternalAgent
from agno.models.response import ToolExecution
from agno.run.agent import (
    RunContentEvent,
    RunOutputEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)

import bob_agent_sdk as sdk


@dataclass
class BobAgent(BaseExternalAgent):
    """Adapter for the Bob Agent SDK.

    Bob runs its tools itself. You choose what the agent may do with `mode`,
    `auto_approve` and `mcp_servers`.

    Args:
        name: Display name of the agent.
        id: Unique ID. Generated from the name when not set.
        mode: `agent`, `plan`, `ask`, a custom mode slug, or a `ModeDefinition`
            with the agent's own role, instructions and tool groups.
        cwd: Workspace directory. Defaults to the current directory.
        auto_approve: Let Bob approve tool calls itself. An AgentOS run has
            nobody to ask, so without this Bob's approval requests are rejected.
        max_turns: Stop a run after this number of model requests.
        max_cost: Stop a run when the session has spent this many Bobcoins.
        mcp_servers: In-process tools and external MCP servers.
        agents: Subagents, by name.
        skills: Skill folders to attach.
        isolation: `shared`, `enforced` or `worktree`.
        options_kwargs: Any other `BobAgentOptions` field, for example `hooks`.
    """

    mode: str | sdk.ModeDefinition | None = None
    cwd: str | Path | None = None
    auto_approve: bool = False
    max_turns: int | None = None
    max_cost: float | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, sdk.AgentDefinition] = field(default_factory=dict)
    skills: Sequence[str | Path] = ()
    isolation: str = "shared"
    options_kwargs: dict[str, Any] = field(default_factory=dict)
    framework: str = "bob-agent-sdk"

    # Agno session ID -> (Bob session ID, the directory that session ran in).
    # A Bob session can only be resumed in the directory where it started.
    _bob_sessions: dict[str, tuple[str, str]] = field(default_factory=dict, init=False, repr=False)

    def _build_options(self, *, streaming: bool = False, **kwargs: Any) -> sdk.BobAgentOptions:
        options: dict[str, Any] = {
            "mode": self.mode,
            "cwd": self.cwd,
            "auto_approve": self.auto_approve,
            "max_turns": self.max_turns,
            "max_cost": self.max_cost,
            "mcp_servers": self.mcp_servers,
            "agents": self.agents,
            "skills": self.skills,
            "isolation": self.isolation,
            # Text arrives in chunks only with partial messages.
            "include_partial_messages": streaming,
            **self.options_kwargs,
        }
        earlier = self._bob_sessions.get(kwargs.get("session_id") or "")
        if earlier:
            # Continue the Bob session of this Agno session, where it ran before.
            # If that was a worktree, it already exists, so do not make a new one.
            options["resume"], options["cwd"] = earlier
            if options["isolation"] == "worktree":
                options["isolation"] = "shared"
        return sdk.BobAgentOptions(**options)

    def _remember_session(self, message: sdk.SystemMessage, agno_session_id: str | None) -> None:
        if message.subtype == "init" and agno_session_id:
            self._bob_sessions[agno_session_id] = (message.data["session_id"], message.data["cwd"])

    @staticmethod
    def _check_result(message: sdk.ResultMessage) -> None:
        """Raise if the run did not end normally, so that Agno marks it as failed."""
        if message.is_error:
            raise RuntimeError(f"Bob stopped the run: {message.stop_reason}")

    async def _arun_adapter(
        self, input: Any, *, history: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> str:
        """Non-streaming: run to the end and return Bob's final text."""
        agno_session_id = kwargs.get("session_id")
        final_text = ""
        async for message in sdk.query(prompt=str(input), options=self._build_options(**kwargs)):
            if isinstance(message, sdk.SystemMessage):
                self._remember_session(message, agno_session_id)
            elif isinstance(message, sdk.ResultMessage):
                self._check_result(message)
                final_text = message.result or ""
        return final_text

    async def _arun_adapter_stream(
        self, input: Any, *, history: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> AsyncIterator[RunOutputEvent]:
        """Streaming: yield text chunks and tool call events as Bob works."""
        run_id = kwargs.get("run_id", "")
        agno_session_id = kwargs.get("session_id")
        identity = {"run_id": run_id, "agent_id": self.get_id(), "agent_name": self.name or ""}
        started: dict[str, ToolExecution] = {}

        options = self._build_options(streaming=True, **kwargs)
        async for message in sdk.query(prompt=str(input), options=options):
            if isinstance(message, sdk.StreamEvent):
                update = message.event
                content = update.get("content") or {}
                if update.get("sessionUpdate") == "agent_message_chunk" and content.get("text"):
                    yield RunContentEvent(content=content["text"], **identity)

            elif isinstance(message, sdk.SystemMessage):
                self._remember_session(message, agno_session_id)

            elif isinstance(message, sdk.AssistantMessage):
                # The text was already sent in chunks. Only the tool calls are new.
                for block in message.content:
                    if isinstance(block, sdk.ToolUseBlock):
                        tool = ToolExecution(
                            tool_call_id=block.id,
                            # Bob's tool name is only known with hooks. The title
                            # (for example the shell command) is always there.
                            tool_name=block.name or block.title or block.kind or "tool",
                            tool_args=block.input,
                        )
                        started[block.id] = tool
                        yield ToolCallStartedEvent(tool=tool, **identity)

            elif isinstance(message, sdk.ToolResultMessage):
                tool = started.get(message.tool_use_id) or ToolExecution(
                    tool_call_id=message.tool_use_id, tool_name="tool"
                )
                tool.result = message.output or ""
                tool.tool_call_error = message.is_error
                yield ToolCallCompletedEvent(tool=tool, content=tool.result, **identity)

            elif isinstance(message, sdk.ResultMessage):
                self._check_result(message)

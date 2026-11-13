"""`BobSDKClient`: an interactive, multi-turn Bob session."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any, cast

import acp
from acp import schema
from acp.client.connection import ClientSideConnection

from ._errors import (
    ACPError,
    AuthenticationError,
    BobSDKError,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
)
from ._hooks import TOKEN_ENV, URL_ENV, HookDispatcher, scope_guard
from ._http import LoopbackServer
from ._messages import MessageAssembler, to_dict, tool_use_block
from ._usage import UsageSnapshot, parse_export
from ._version import __version__
from ._workspace import WorkspaceFiles
from ._worktree import close_worktree, create_worktree
from .tools import McpEndpoint, SdkMcpServer
from .types import (
    BobAgentOptions,
    HookEvent,
    HookMatcher,
    McpHttpServerConfig,
    McpStdioServerConfig,
    Message,
    ModeDefinition,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
    Worktree,
)

# One protocol message is one line. Tool output and diffs can be large.
_LINE_LIMIT = 64 * 1024 * 1024
# Bob's own subagents. A mode with a list of allowed subagents must name them
# to keep them, so `isolation="enforced"` adds them to the list it builds.
_BUILT_IN_SUBAGENTS = ("general", "explore")
# Set to 1 to use Bob's saved SSO login instead of an API key.
USE_SSO_ENV = "BOB_USE_SSO"
_END = object()
_CANCELLED = schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))


class BobSDKClient:
    """A Bob session that stays open across turns.

    async with BobSDKClient(options) as client:
        await client.query("Explain this project")
        async for message in client.receive_response():
            ...
        await client.query("Now add tests")   # same session, full context
        async for message in client.receive_response():
            ...
    """

    def __init__(self, options: BobAgentOptions | None = None) -> None:
        self.options = options or BobAgentOptions()
        self.session_id: str | None = None
        self.worktree: Worktree | None = None
        """Set when the session runs with `isolation="worktree"`."""
        self._conn: ClientSideConnection | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stack = AsyncExitStack()
        self._closing = False
        self._background: list[asyncio.Task[None]] = []
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._assembler: MessageAssembler | None = None
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._prompt_task: asyncio.Task[None] | None = None
        self._permission_calls: set[asyncio.Future[Any]] = set()
        self._server: LoopbackServer | None = None
        self._mode: str | None = None
        self._tool_names: dict[str, str] = {}  # Tool call ID to Bob tool name.
        # Usage. Bob reports totals for the session, so the SDK keeps baselines.
        self._usage_supported = False
        self._cost_at_connect = 0.0
        self._cost_before_turn = 0.0
        self._requests_before_turn = 0
        self._limit_hit: str | None = None
        self._limit_task: asyncio.Task[None] | None = None
        self._unchecked_tool_call: str | None = None

    async def __aenter__(self) -> BobSDKClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Start Bob and open (or resume) a session."""
        if self._conn is not None:
            return
        opts = self.options
        cwd = str(Path(opts.cwd or os.getcwd()).resolve())
        env = self._environment()
        command = self._command()

        try:
            # Cleanup callbacks run in reverse order: Bob exits first, then the
            # session files are removed, then the worktree is checked for work.
            if opts.isolation == "worktree":
                if opts.resume:
                    raise BobSDKError(
                        "A resumed session must run where it ran before. Pass the "
                        "worktree path as cwd, without isolation='worktree'."
                    )
                self.worktree, cwd, worktree_lock = create_worktree(cwd)
                self._stack.callback(close_worktree, self.worktree, worktree_lock)
            enforced = opts.isolation == "enforced"
            workspace = WorkspaceFiles(cwd)
            self._stack.callback(workspace.cleanup)

            hooks: dict[HookEvent, list[HookMatcher]] | None = (
                dict(opts.hooks) if opts.hooks is not None else {} if enforced else None
            )
            if enforced and hooks is not None:
                guard = HookMatcher(hooks=[scope_guard(workspace.foreign_names)])
                hooks["PreToolUse"] = [guard, *hooks.get("PreToolUse", [])]

            mode = opts.mode
            if isinstance(mode, ModeDefinition):
                if enforced and mode.allowed_subagents is None:
                    # With a list, Bob does not show the other subagents to the
                    # model at all. That is stronger than refusing them.
                    allowed = [*_BUILT_IN_SUBAGENTS, *opts.agents, *workspace.project_agents()]
                    mode = dataclasses.replace(mode, allowed_subagents=list(dict.fromkeys(allowed)))
                self._mode = workspace.add_mode(mode)
            else:
                self._mode = mode
            workspace.add_agents(opts.agents)
            workspace.add_skills(opts.skills)
            if hooks is not None:
                workspace.add_hook_bridge()
            workspace.install()

            mcp_servers = await self._mcp_servers()
            if hooks is not None:
                dispatcher = HookDispatcher(hooks, self._on_hook_event, self._queue.put_nowait)
                server = await self._loopback_server()
                server.route("/hook", dispatcher.handle)
                env[URL_ENV] = server.url("/hook")
                env[TOKEN_ENV] = server.token

            try:
                conn, self._process = await self._stack.enter_async_context(
                    acp.spawn_agent_process(
                        # Bob only calls the two client methods that _AcpClient has.
                        cast(acp.Client, _AcpClient(self)),
                        *command,
                        env=env,
                        cwd=cwd,
                        transport_kwargs={"limit": _LINE_LIMIT},
                    )
                )
            except FileNotFoundError as exc:
                raise CLINotFoundError(f"Could not start Bob: {exc}") from exc
            self._conn = conn
            stderr_task = asyncio.create_task(self._read_stderr())
            self._background = [stderr_task, asyncio.create_task(self._watch_exit(stderr_task))]

            init = await conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_info=schema.Implementation(name="bob-agent-sdk", version=__version__),
            )
            capabilities = init.agent_capabilities
            self._usage_supported = "bob/extensions" in (
                (capabilities.field_meta if capabilities else None) or {}
            )
            if (opts.max_turns or opts.max_cost) and not self._usage_supported:
                raise BobSDKError(
                    "This Bob version cannot report usage over ACP, "
                    "so max_turns and max_cost cannot be enforced."
                )
            if opts.resume:
                session: Any = await conn.resume_session(
                    session_id=opts.resume, cwd=cwd, mcp_servers=mcp_servers
                )
                session_id = opts.resume
            else:
                meta: dict[str, Any] = {"bob/mode": self._mode} if self._mode else {}
                session = await conn.new_session(cwd=cwd, mcp_servers=mcp_servers, **meta)
                session_id = session.session_id
            # Bob sends updates as soon as the session exists. Updates that arrive
            # before this line are not tied to a turn and are dropped.
            self._assembler = MessageAssembler(
                session_id, opts.include_partial_messages, self._tool_names
            )
            self.session_id = session_id
            if opts.team_id:
                await conn.set_config_option(
                    config_id="team", session_id=session_id, value=opts.team_id
                )
            if opts.resume:
                if self._mode:
                    await self.set_mode(self._mode)
                if earlier := await self._usage():
                    self._cost_at_connect = self._cost_before_turn = earlier.total_cost
                    self._requests_before_turn = earlier.model_requests
        except BaseException as exc:
            error = await self._translate(exc)
            await self.disconnect()
            if error is exc:
                raise
            raise error from exc

        self._queue.put_nowait(
            SystemMessage(
                "init",
                {
                    "session_id": session_id,
                    "cwd": cwd,
                    "agent_info": to_dict(init.agent_info) if init.agent_info else None,
                    "modes": to_dict(session.modes) if session.modes else None,
                    "worktree": (
                        {"path": str(self.worktree.path), "branch": self.worktree.branch}
                        if self.worktree
                        else None
                    ),
                },
            )
        )

    async def query(self, prompt: str | list[dict[str, Any]]) -> None:
        """Send a prompt. Read the answer with `receive_response()`.

        `prompt` is text, or a list of ACP content blocks (for example to
        attach an image).
        """
        conn, _ = self._connected()
        if self._prompt_task is not None and not self._prompt_task.done():
            raise BobSDKError("A turn is already in progress. Read it to the end or interrupt it.")
        blocks: list[Any] = (
            [schema.TextContentBlock(type="text", text=prompt)]
            if isinstance(prompt, str)
            else prompt
        )
        self._prompt_task = asyncio.create_task(self._run_turn(conn, blocks))

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Yield every message until the session is closed."""
        while True:
            item = await self._queue.get()
            if item is _END:
                self._queue.put_nowait(_END)
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def receive_response(self) -> AsyncIterator[Message]:
        """Yield messages up to and including the `ResultMessage` of the current turn."""
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return

    async def interrupt(self) -> None:
        """Stop the current turn. Its `ResultMessage` has `stop_reason == "cancelled"`."""
        conn, session_id = self._connected()
        for call in list(self._permission_calls):
            call.cancel()
        await conn.cancel(session_id=session_id)

    async def set_mode(self, mode: str) -> None:
        """Switch the session to another mode, for example from `plan` to `agent`."""
        conn, session_id = self._connected()
        try:
            await conn.set_session_mode(session_id=session_id, mode_id=mode)
        except acp.RequestError as exc:
            raise ACPError.from_request_error(exc) from exc

    async def disconnect(self) -> None:
        """Stop Bob. The session is saved and can be continued with `resume`."""
        self._closing = True
        self._conn = None
        # Closes the connection, stops the Bob process and stops the tool servers.
        await self._stack.aclose()
        tasks = [
            *self._background,
            *(task for task in (self._prompt_task, self._limit_task) if task),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._queue.put_nowait(_END)

    # ------------------------------------------------------------ internals

    def _connected(self) -> tuple[ClientSideConnection, str]:
        if self._conn is None or self.session_id is None:
            raise CLIConnectionError("Not connected. Call connect() first.")
        return self._conn, self.session_id

    def _environment(self) -> dict[str, str]:
        """The environment of the Bob process, with the login it must use.

        Bob uses `BOB_API_KEY` when it is set, and its saved SSO login otherwise.
        The SDK requires a key unless SSO is asked for, because an SSO login
        needs a browser on this machine, which a server does not have.
        """
        opts = self.options
        env = {**os.environ, **opts.env}
        if opts.api_key:
            env["BOB_API_KEY"] = opts.api_key
        elif env.get(USE_SSO_ENV, "").strip().lower() in ("1", "true", "yes"):
            env.pop("BOB_API_KEY", None)  # Else Bob would use the key and not the SSO login.
        elif not env.get("BOB_API_KEY"):
            raise AuthenticationError(
                "No Bob API key. Set the BOB_API_KEY environment variable or pass "
                "BobAgentOptions(api_key=...). To use Bob's saved SSO login instead, "
                f"set {USE_SSO_ENV}=1."
            )
        return env

    def _command(self) -> list[str]:
        opts = self.options
        cli = str(opts.cli_path) if opts.cli_path else shutil.which("bob")
        if not cli:
            raise CLINotFoundError(
                "Bob Shell was not found on PATH. Install it, or set BobAgentOptions.cli_path."
            )
        cmd = [cli, "acp"]
        for enabled, flag in (
            (opts.auto_approve, "--auto-approve"),
            (opts.trust, "--trust"),
            (opts.accept_license, "--accept-license"),
            (opts.disable_mcp, "--disable-mcp"),
            (opts.disable_subagents, "--disable-subagents"),
        ):
            if enabled:
                cmd.append(flag)
        if opts.log_level:
            cmd += ["--log-level", opts.log_level]
        return cmd

    async def _loopback_server(self) -> LoopbackServer:
        """The local server that Bob calls for in-process tools and hooks."""
        if self._server is None:
            self._server = LoopbackServer()
            await self._server.start()
            self._stack.push_async_callback(self._server.stop)
        return self._server

    async def _mcp_servers(self) -> list[Any]:
        """Convert `options.mcp_servers` to ACP models. Mounts in-process tool servers."""
        servers: list[Any] = []
        for index, (name, config) in enumerate(self.options.mcp_servers.items()):
            if isinstance(config, SdkMcpServer):
                server = await self._loopback_server()
                path = f"/mcp/{index}"
                server.route(path, McpEndpoint(config).handle)
                servers.append(
                    schema.HttpMcpServer(
                        type="http",
                        name=name,
                        url=server.url(path),
                        headers=[
                            schema.HttpHeader(name="Authorization", value=f"Bearer {server.token}")
                        ],
                    )
                )
            elif config.get("type") == "http":
                http = cast(McpHttpServerConfig, config)
                servers.append(
                    schema.HttpMcpServer(
                        type="http",
                        name=name,
                        url=http["url"],
                        headers=[
                            schema.HttpHeader(name=key, value=value)
                            for key, value in http.get("headers", {}).items()
                        ],
                    )
                )
            else:
                stdio = cast(McpStdioServerConfig, config)
                servers.append(
                    schema.McpServerStdio(
                        name=name,
                        command=stdio["command"],
                        args=list(stdio.get("args", [])),
                        env=[
                            schema.EnvVariable(name=key, value=value)
                            for key, value in stdio.get("env", {}).items()
                        ],
                    )
                )
        return servers

    async def _run_turn(self, conn: ClientSideConnection, blocks: list[Any]) -> None:
        assert self._assembler is not None and self.session_id is not None
        opts = self.options
        started = time.monotonic()
        self._limit_hit = None
        self._unchecked_tool_call = None
        try:
            spent = self._cost_before_turn - self._cost_at_connect
            if opts.max_cost is not None and spent >= opts.max_cost:
                # The budget is already used up. Do not start another turn.
                stop_reason = "max_cost"
            else:
                response = await conn.prompt(session_id=self.session_id, prompt=blocks)
                if self._limit_task is not None:
                    await self._limit_task
                stop_reason = response.stop_reason
                if stop_reason == "cancelled" and self._limit_hit:
                    stop_reason = self._limit_hit
            usage: dict[str, Any] = {}
            if snapshot := await self._usage():
                usage = {
                    "num_turns": snapshot.model_requests - self._requests_before_turn,
                    "cost": round(snapshot.total_cost - self._cost_before_turn, 6),
                    "total_cost": snapshot.total_cost,
                    "usage": snapshot.costs,
                    "model": snapshot.model,
                }
                self._cost_before_turn = snapshot.total_cost
                self._requests_before_turn = snapshot.model_requests
            duration_ms = int((time.monotonic() - started) * 1000)
            messages = self._assembler.finish(stop_reason, duration_ms, **usage)
        except ConnectionError:
            return  # Bob exited. _watch_exit reports it.
        except Exception as exc:
            # Nothing awaits this task, so the reader is the only place to report to.
            self._queue.put_nowait(await self._translate(exc))
            return
        for message in messages:
            self._queue.put_nowait(message)

    async def _usage(self) -> UsageSnapshot | None:
        """Read the session's cost and request count from Bob's task export."""
        if not self._usage_supported or self._conn is None or self.session_id is None:
            return None
        try:
            export = await self._conn.ext_method("bob/task/export", {"sessionId": self.session_id})
        except acp.RequestError:
            return None  # Bob has not saved the task yet.
        return parse_export(export, self.session_id)

    async def _check_limits(self) -> None:
        """Stop the turn when it has reached `max_turns` or `max_cost`.

        Runs when Bob starts a tool call, which is the point between two model
        requests. A turn can therefore pass a limit by one request at most.
        """
        opts = self.options
        try:
            # A new tool call can start while a check is running. Check it too.
            while (tool_call_id := self._unchecked_tool_call) and not self._limit_hit:
                self._unchecked_tool_call = None
                snapshot = await self._usage()
                if snapshot is None:
                    continue
                requests = snapshot.model_requests - self._requests_before_turn
                if tool_call_id not in snapshot.tool_call_ids:
                    requests += 1  # Bob has not yet saved the response that made this call.
                if opts.max_turns is not None and requests >= opts.max_turns:
                    self._limit_hit = "max_turns"
                elif (
                    opts.max_cost is not None
                    and snapshot.total_cost - self._cost_at_connect >= opts.max_cost
                ):
                    self._limit_hit = "max_cost"
            if self._limit_hit:
                await self.interrupt()
        except (ConnectionError, CLIConnectionError):
            pass  # The session closed while the check ran.
        except Exception as exc:
            self._queue.put_nowait(exc)

    def _on_update(self, update: Any) -> None:
        if self._assembler is None:
            return
        for message in self._assembler.feed(update):
            self._queue.put_nowait(message)
        opts = self.options
        if update.session_update == "tool_call" and (
            opts.max_turns is not None or opts.max_cost is not None
        ):
            self._unchecked_tool_call = update.tool_call_id
            if self._limit_task is None or self._limit_task.done():
                self._limit_task = asyncio.create_task(self._check_limits())

    def _on_hook_event(self, payload: dict[str, Any]) -> None:
        # PreToolUse arrives before the ACP tool call, and is the only place
        # where Bob gives the tool's name.
        if payload.get("hook_event_name") == "PreToolUse" and payload.get("tool_use_id"):
            self._tool_names[payload["tool_use_id"]] = payload.get("tool_name", "")

    async def _decide_permission(
        self, session_id: str, tool_call: Any, options: list[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        allow = False
        always = False
        if self.options.can_use_tool is not None:
            call = asyncio.ensure_future(
                self.options.can_use_tool(
                    tool_use_block(tool_call, self._tool_names.get(tool_call.tool_call_id)),
                    ToolPermissionContext(session_id, [to_dict(option) for option in options]),
                )
            )
            self._permission_calls.add(call)
            try:
                decision = await call
            except asyncio.CancelledError:
                if call.cancelled():  # interrupt() was called.
                    return _CANCELLED
                raise
            except Exception as exc:
                # Surface the callback's failure to whoever reads the messages.
                self._queue.put_nowait(exc)
                return _CANCELLED
            finally:
                self._permission_calls.discard(call)
            allow = isinstance(decision, PermissionResultAllow)
            always = isinstance(decision, PermissionResultAllow) and decision.always

        if allow:
            wanted = ["allow_always", "allow_once"] if always else ["allow_once"]
        else:
            wanted = ["reject_once", "reject_always"]
        for kind in wanted:
            for option in options:
                if option.kind == kind:
                    return schema.RequestPermissionResponse(
                        outcome=schema.AllowedOutcome(
                            outcome="selected", option_id=option.option_id
                        )
                    )
        return _CANCELLED

    async def _read_stderr(self) -> None:
        # The pipe must be drained, or Bob blocks when it fills up.
        assert self._process is not None and self._process.stderr is not None
        with suppress(Exception):
            while line := await self._process.stderr.readline():
                text = line.decode(errors="replace")
                self._stderr_tail.append(text)
                if self.options.stderr is not None:
                    self.options.stderr(text.rstrip("\n"))

    async def _watch_exit(self, stderr_task: asyncio.Task[None]) -> None:
        """Report it to the message reader when Bob exits on its own."""
        assert self._process is not None
        exit_code = await self._process.wait()
        await stderr_task
        if not self._closing:
            self._queue.put_nowait(ProcessError(exit_code, "".join(self._stderr_tail).strip()))
            self._queue.put_nowait(_END)

    async def _translate(self, exc: BaseException) -> BaseException:
        """Map ACP SDK errors to this SDK's errors."""
        if isinstance(exc, acp.RequestError):
            return ACPError.from_request_error(exc)
        if isinstance(exc, ConnectionError) and self._process is not None:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._background[1]), 2)
            return ProcessError(self._process.returncode, "".join(self._stderr_tail).strip())
        return exc


class _AcpClient:
    """The client side of ACP: receives Bob's updates and permission requests."""

    def __init__(self, owner: BobSDKClient) -> None:
        self._owner = owner

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        # No awaits here: the ACP SDK runs one task per update, and they must
        # reach the assembler in arrival order.
        self._owner._on_update(update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: schema.ToolCallUpdate,
        options: list[schema.PermissionOption],
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        return await self._owner._decide_permission(session_id, tool_call, options)

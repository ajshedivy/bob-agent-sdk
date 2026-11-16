"""Public types: options, messages and permission results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Union

from pydantic import BaseModel

if TYPE_CHECKING:
    from .tools import SdkMcpServer

# ---------------------------------------------------------------- messages


@dataclass
class TextBlock:
    text: str


@dataclass
class ThinkingBlock:
    thinking: str


@dataclass
class ToolUseBlock:
    """A tool call that Bob started."""

    id: str
    title: str
    """Human-readable label, for example the shell command."""
    kind: str | None
    """ACP category: `read`, `edit`, `execute`, ..."""
    input: dict[str, Any]
    locations: list[str] = field(default_factory=list)
    name: str | None = None
    """Bob's tool name, for example `execute_command`. Only known when hooks
    are enabled (`BobAgentOptions.hooks` is not None)."""


ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock


@dataclass
class SubagentRef:
    """The subagent that a message or a permission request comes from.

    Bob does not stream a subagent's work over ACP. The SDK reports a subagent's
    tool calls and successful results from Bob's hook events while it runs, when
    only one subagent runs. Everything else (its text, failed results, and the
    work of subagents that ran at the same time as another one) comes from Bob's
    task export at the end of the turn, before the `ResultMessage`.
    """

    tool_use_id: str | None
    """The `spawn_subagent` tool call that started the subagent. None only on a
    permission request while several subagents run: Bob does not say which one asks."""
    name: str | None
    """The subagent's name. None only on a permission request while several
    subagents with different names run."""
    at: float | None = None
    """When Bob recorded the message, in Unix seconds. Messages from the end of
    the turn arrive later than this."""


@dataclass
class AssistantMessage:
    """Text, reasoning and tool calls from Bob."""

    content: list[ContentBlock]
    subagent: SubagentRef | None = None
    """Set when a subagent wrote the message, not the agent itself."""


@dataclass
class ToolResultMessage:
    """The final outcome of one tool call."""

    tool_use_id: str
    status: Literal["completed", "failed"]
    output: str | None
    """The text output of the tool, if it produced any."""
    content: list[dict[str, Any]]
    """Raw ACP tool content. File edits appear here as `{"type": "diff", ...}`."""
    raw_output: Any = None
    subagent: SubagentRef | None = None
    """Set when the tool call is a subagent's, not the agent's own."""

    @property
    def is_error(self) -> bool:
        return self.status == "failed"


@dataclass
class PlanEntry:
    content: str
    status: str
    priority: str | None = None


@dataclass
class PlanMessage:
    """Bob's current todo list. Each update replaces the previous one."""

    entries: list[PlanEntry]


@dataclass
class SystemMessage:
    """Session metadata.

    `subtype` is `init` when the session opens. Otherwise it is the ACP update
    name, for example `current_mode_update` or `session_info_update`.
    """

    subtype: str
    data: dict[str, Any]


@dataclass
class StreamEvent:
    """A raw ACP `session/update`. Only sent with `include_partial_messages`."""

    session_id: str
    event: dict[str, Any]


@dataclass
class ResultMessage:
    """Marks the end of one turn."""

    session_id: str
    stop_reason: str
    """`end_turn`, `cancelled`, `max_tokens`, `max_turn_requests` or `refusal` from
    Bob. `max_turns` or `max_cost` when a limit from `BobAgentOptions` stopped the turn.
    `error_max_structured_output_retries` when the agent never reported its typed output."""
    duration_ms: int
    num_tool_calls: int
    result: str | None
    """The last text that Bob wrote in this turn."""
    is_error: bool = False
    """True when the turn did not end normally or by `interrupt()`."""
    num_turns: int | None = None
    """Model requests that Bob made in this turn, including those of its subagents."""
    cost: float | None = None
    """Cost of this turn, in Bobcoins."""
    total_cost: float | None = None
    """Cost of the whole session so far, in Bobcoins. Includes subagents."""
    usage: dict[str, Any] | None = None
    """Bob's raw cost record for the session, for example `contextTokens`."""
    model: str | None = None
    structured_output: Any = None
    """The agent's answer in the form that `BobAgentOptions.output_format` asked
    for: an instance of the Pydantic model, or data that matches the JSON Schema."""


@dataclass
class SubagentResultMessage:
    """How one subagent of the turn ended. Sent at the end of the turn, after the
    subagent's messages and before the `ResultMessage`."""

    tool_use_id: str
    """The `spawn_subagent` tool call that started the subagent."""
    name: str
    subagent_id: str | None
    """Bob's ID of the subagent. Bob's `resume_id` takes it."""
    stop_reason: str | None
    """Bob's reason for the end of the subagent's loop, for example `stop`,
    `loop_condition`, `cancelled` or `error`."""
    is_error: bool
    duration_ms: int | None
    num_turns: int
    """Model requests that the subagent made. Also in the turn's `ResultMessage.num_turns`."""
    num_tool_calls: int
    cost: float | None
    """Cost of the subagent, in Bobcoins. Also counted in the turn's `ResultMessage.cost`."""


Message = (
    AssistantMessage
    | ToolResultMessage
    | PlanMessage
    | SystemMessage
    | StreamEvent
    | SubagentResultMessage
    | ResultMessage
)

# ------------------------------------------------------------- permissions


@dataclass
class ToolPermissionContext:
    session_id: str
    options: list[dict[str, Any]]
    """The raw choices that Bob offered (`optionId`, `name`, `kind`)."""
    subagent: SubagentRef | None = None
    """Set when a subagent asks, not the agent itself."""


@dataclass
class PermissionResultAllow:
    always: bool = False
    """Also approve similar calls for the rest of the task, when Bob offers that."""


@dataclass
class PermissionResultDeny:
    pass


PermissionResult = PermissionResultAllow | PermissionResultDeny
CanUseTool = Callable[[ToolUseBlock, ToolPermissionContext], Awaitable[PermissionResult]]

# ------------------------------------------------------------------- hooks

HookEvent = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "PreCompact",
    "PostCompact",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "PermissionResolved",
    "Notification",
    "Stop",
    "SessionEnd",
]

HookCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
"""Receives Bob's hook payload. Returns Bob's structured hook output, or None."""


@dataclass
class HookMatcher:
    hooks: list[HookCallback]
    matcher: str | None = None
    """Regular expression. Tested against the tool name for tool events, and
    against the source, trigger, type or reason for the other events that
    have one. None matches everything."""
    timeout: float | None = 60.0
    """Seconds that each callback may take."""


# --------------------------------------------------------- modes and agents

DEFAULT_TOOL_GROUPS = ("read", "edit", "execute", "mcp", "skill", "todo", "subagent", "mode")


@dataclass
class ModeDefinition:
    """A custom Bob mode: the role of the agent and the tool groups it may use."""

    role_definition: str
    """Who the agent is. This opens Bob's system prompt."""
    custom_instructions: str | None = None
    """How the agent must work. Added to the system prompt."""
    groups: Sequence[str] = DEFAULT_TOOL_GROUPS
    """Tool groups the agent may use. Leave one out to disable its tools."""
    name: str = "SDK Agent"
    description: str | None = None
    allowed_subagents: Sequence[str] | None = None
    slug: str | None = None
    """Mode ID. Generated when None."""


@dataclass
class AgentDefinition:
    """A subagent that Bob can start with its `spawn_subagent` tool."""

    description: str
    """Tells Bob when to use the subagent."""
    prompt: str
    """The subagent's system prompt."""
    groups: Sequence[str] | None = None
    """Tool groups. Bob's default is `read`, `edit`, `execute`."""
    allow_tools: Sequence[str] | None = None
    deny_tools: Sequence[str] | None = None
    model_tier: str | None = None
    max_turns: int | None = None


@dataclass
class Worktree:
    """The git worktree of a session that runs with `isolation="worktree"`."""

    path: Path
    branch: str
    repo: Path
    """The main checkout that the worktree belongs to."""
    removed: bool = False
    """True once the session has closed and left no work, so the SDK removed
    the worktree and its branch."""


# ----------------------------------------------------------------- options


class McpStdioServerConfig(TypedDict, total=False):
    command: str  # required
    args: list[str]
    env: dict[str, str]


class McpHttpServerConfig(TypedDict, total=False):
    type: Literal["http"]  # required
    url: str  # required
    headers: dict[str, str]


McpServerConfig = Union[McpStdioServerConfig, McpHttpServerConfig, "SdkMcpServer"]


@dataclass
class BobAgentOptions:
    cwd: str | Path | None = None
    """Workspace directory. Defaults to the current directory."""
    mode: str | ModeDefinition | None = None
    """Starting mode: `agent`, `plan`, `ask`, the slug of an existing custom mode,
    or a `ModeDefinition` that the SDK creates for this session."""
    agents: dict[str, AgentDefinition] = field(default_factory=dict)
    """Subagents for this session, by name."""
    skills: Sequence[str | Path] = ()
    """Skill folders (each with a `SKILL.md`) to make available in this session."""
    isolation: Literal["shared", "enforced", "worktree"] = "shared"
    """How the session is separated from other SDK sessions.
    `shared`: it works in `cwd`, and sessions there see each other's subagents and skills.
    `enforced`: as `shared`, but it cannot use what only another session defined.
    `worktree`: it works in a git worktree of its own, on a new branch."""
    hooks: dict[HookEvent, list[HookMatcher]] | None = None
    """Python callbacks for Bob's hook events. Any value other than None, also
    `{}`, turns hooks on and so makes `ToolUseBlock.name` available."""
    hook_scope: Literal["workspace", "global"] = "workspace"
    """Where the SDK registers its hook command. `workspace`: in `.bob/settings.json`
    of the workspace. `global`: in Bob's global `~/.bob/settings/settings.json`, so
    nothing is written to the workspace. The command does nothing in Bob sessions
    that the SDK did not start."""
    output_format: dict[str, Any] | type[BaseModel] | None = None
    """Make the agent answer with data, not only text. Pass a Pydantic model class,
    or `{"type": "json_schema", "schema": {...}}`. The answer is in
    `ResultMessage.structured_output`. The agent reports it through a tool when its
    mode has the `mcp` tool group, and as JSON text otherwise."""
    max_turns: int | None = None
    """Stop a turn after this number of model requests."""
    max_cost: float | None = None
    """Stop when the session has spent this many Bobcoins since it connected."""
    team_id: str | None = None
    """Bob team to run under, for accounts with more than one team."""
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)
    """Extra MCP servers for this session, by name."""
    can_use_tool: CanUseTool | None = None
    """Called when Bob asks for approval. Without it, such tool calls are rejected."""
    auto_approve: bool = False
    """Let Bob approve tool calls itself when its safety rules allow."""
    resume: str | None = None
    """Session ID of an earlier session to continue."""
    include_partial_messages: bool = False
    """Also yield a `StreamEvent` for every raw update (text deltas, tool progress)."""
    api_key: str | None = None
    """Bob API key. Defaults to the `BOB_API_KEY` environment variable. Without a
    key the session does not start, unless `BOB_USE_SSO=1` is set: then
    Bob uses its saved SSO login."""
    trust: bool = False
    """Save the workspace as trusted."""
    accept_license: bool = False
    disable_mcp: bool = False
    """Do not start the MCP servers from Bob's own configuration."""
    disable_subagents: bool = False
    log_level: str | None = None
    cli_path: str | Path | None = None
    """Path to the `bob` executable. Defaults to the one on PATH."""
    env: dict[str, str] = field(default_factory=dict)
    """Extra environment variables for the Bob process."""
    stderr: Callable[[str], None] | None = None
    """Called with each line that Bob writes to stderr."""

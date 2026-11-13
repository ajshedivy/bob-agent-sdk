"""Python hook callbacks, built on Bob's native hooks.

Bob runs a shell command for each hook event and passes the event as JSON on
stdin. The SDK registers one fixed command for every event in the workspace's
`.bob/settings.json`. That command forwards the event to this process over the
loopback server, and prints the reply for Bob to read.

The command finds the server through two environment variables that the SDK
sets on the Bob process it starts. In any other Bob session the variables are
missing and the command does nothing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from .types import HookCallback, HookEvent, HookMatcher

URL_ENV = "BOB_AGENT_SDK_HOOK_URL"
TOKEN_ENV = "BOB_AGENT_SDK_HOOK_TOKEN"

# Bob stops a handler after its timeout. Python callbacks may wait for a person,
# so the bridge asks for a long one and each HookMatcher sets its own limit.
BRIDGE_TIMEOUT_SECONDS = 3600

# `|| true`: if this process is gone, the hook must not block Bob.
BRIDGE_COMMAND = (
    f'[ -z "${URL_ENV}" ] || curl -s -m {BRIDGE_TIMEOUT_SECONDS} -X POST '
    f'-H "Authorization: Bearer ${TOKEN_ENV}" -H "Content-Type: application/json" '
    f'--data-binary @- "${URL_ENV}" || true'
)

HOOK_EVENTS: tuple[HookEvent, ...] = (
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
)

# The payload field that a matcher pattern is tested against, per event.
_MATCH_FIELD = {
    "SessionStart": "source",
    "PreCompact": "trigger",
    "PostCompact": "trigger",
    "PreToolUse": "tool_name",
    "PostToolUse": "tool_name",
    "PermissionRequest": "tool_name",
    "PermissionResolved": "tool_name",
    "Notification": "notification_type",
    "SessionEnd": "reason",
}


# The tools that use a subagent, a skill or a mode by name, the input field
# that holds the name, and the word for it in the refusal.
SCOPED_TOOLS = {
    "spawn_subagent": ("name", "Subagent"),
    "use_skill": ("skill_name", "Skill"),
    "switch_mode": ("mode_id", "Mode"),
}


def scope_guard(foreign_names: Callable[[], dict[str, set[str]]]) -> HookCallback:
    """A PreToolUse callback for `isolation="enforced"`.

    It refuses a call that uses a subagent, skill or mode which only another
    SDK session in the workspace defined. `foreign_names` returns those names
    per tool. It is called for every check, because sessions come and go.
    """

    async def guard(event: dict[str, Any]) -> dict[str, Any] | None:
        tool = event.get("tool_name", "")
        if tool not in SCOPED_TOOLS:
            return None
        field, kind = SCOPED_TOOLS[tool]
        name = (event.get("tool_input") or {}).get(field)
        if name not in foreign_names()[tool]:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{kind} {name!r} belongs to another SDK session in this workspace."
                ),
            }
        }

    return guard


def bridge_group() -> dict[str, Any]:
    """The matcher group that the SDK adds to each event in `.bob/settings.json`."""
    return {
        "hooks": [{"type": "command", "command": BRIDGE_COMMAND, "timeout": BRIDGE_TIMEOUT_SECONDS}]
    }


def _blocks(output: dict[str, Any]) -> bool:
    specific = output.get("hookSpecificOutput") or {}
    return (
        output.get("decision") == "block"
        or specific.get("permissionDecision") == "deny"
        or (specific.get("decision") or {}).get("behavior") == "deny"
    )


class HookDispatcher:
    """Receives hook events from Bob and runs the matching Python callbacks."""

    def __init__(
        self,
        hooks: dict[HookEvent, list[HookMatcher]],
        on_event: Callable[[dict[str, Any]], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._hooks = hooks
        self._on_event = on_event
        self._on_error = on_error

    async def handle(self, payload: Any) -> tuple[str, dict[str, Any] | None]:
        if not isinstance(payload, dict):
            return "400 Bad Request", None
        self._on_event(payload)
        try:
            return "200 OK", await self._run(payload)
        except Exception as exc:
            # Bob treats an empty reply as "no decision", so the turn goes on.
            # The failure is reported to whoever reads the messages.
            self._on_error(exc)
            return "200 OK", None

    async def _run(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        event = payload.get("hook_event_name", "")
        value = str(payload.get(_MATCH_FIELD.get(event, ""), ""))
        merged: dict[str, Any] = {}
        for matcher in self._hooks.get(event, []):
            if matcher.matcher not in (None, "*") and not re.search(matcher.matcher, value):
                continue
            for callback in matcher.hooks:
                output = await asyncio.wait_for(callback(payload), matcher.timeout)
                if not output:
                    continue
                if _blocks(output):
                    return output
                specific = output.get("hookSpecificOutput") or {}
                if "updatedInput" in specific:
                    # Later callbacks see the rewritten tool input, as in Bob.
                    payload = {**payload, "tool_input": specific["updatedInput"]}
                merged_specific = {**merged.get("hookSpecificOutput", {}), **specific}
                merged = {**merged, **output}
                if merged_specific:
                    merged["hookSpecificOutput"] = merged_specific
        return merged or None

"""Custom tools that run inside your Python process.

Bob can only reach tools through MCP. The SDK serves each `SdkMcpServer` as an
MCP endpoint (Streamable HTTP, JSON responses only) on its loopback server and
registers it with the Bob session.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}
_DEFAULT_PROTOCOL_VERSION = "2025-03-26"
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class SdkMcpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler


@dataclass
class SdkMcpServer:
    name: str
    version: str
    tools: list[SdkMcpTool]


def tool(
    name: str, description: str, input_schema: dict[str, Any]
) -> Callable[[ToolHandler], SdkMcpTool]:
    """Turn an async function into a tool that Bob can call.

    `input_schema` is either a full JSON Schema object, or a shorthand mapping
    of argument names to Python types, where every argument is required:

        @tool("add", "Add two numbers", {"a": float, "b": float})
        async def add(args):
            return str(args["a"] + args["b"])

    The function receives the arguments as a dict. It can return a string, or an
    MCP result dict such as `{"content": [...], "isError": True}`.
    """

    def decorator(handler: ToolHandler) -> SdkMcpTool:
        return SdkMcpTool(name, description, _json_schema(input_schema), handler)

    return decorator


def create_sdk_mcp_server(
    name: str, version: str = "1.0.0", tools: list[SdkMcpTool] | None = None
) -> SdkMcpServer:
    """Group tools into a server for `BobAgentOptions.mcp_servers`."""
    return SdkMcpServer(name, version, list(tools or []))


def _json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if isinstance(schema.get("type"), str) and "properties" in schema:
        return schema
    return {
        "type": "object",
        "properties": {key: {"type": _PY_TO_JSON[py]} for key, py in schema.items()},
        "required": list(schema),
    }


def _tool_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "content" in value:
        return value
    text = value if isinstance(value, str) else json.dumps(value)
    return {"content": [{"type": "text", "text": text}]}


class McpEndpoint:
    """Answers MCP requests for one `SdkMcpServer`. Mounted on the SDK's loopback server."""

    def __init__(self, server: SdkMcpServer) -> None:
        self._tools = {t.name: t for t in server.tools}
        self._info = {"name": server.name, "version": server.version}

    async def handle(self, message: Any) -> tuple[str, dict[str, Any] | None]:
        if not isinstance(message, dict) or "method" not in message:
            return "400 Bad Request", None
        if "id" not in message:
            return "202 Accepted", None  # A notification.
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            reply["result"] = await self._call(message["method"], message.get("params") or {})
        except _RpcError as exc:
            reply["error"] = {"code": exc.code, "message": exc.message}
        return "200 OK", reply

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion", _DEFAULT_PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": self._info,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                    for t in self._tools.values()
                ]
            }
        if method == "tools/call":
            tool_def = self._tools.get(params.get("name", ""))
            if tool_def is None:
                raise _RpcError(_INVALID_PARAMS, f"Unknown tool: {params.get('name')}")
            try:
                return _tool_result(await tool_def.handler(params.get("arguments") or {}))
            except Exception as exc:
                # Report the failure to the model so that it can react.
                return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
        raise _RpcError(_METHOD_NOT_FOUND, f"Method not found: {method}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message

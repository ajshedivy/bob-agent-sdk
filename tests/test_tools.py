import asyncio
import json
import urllib.error
import urllib.request

from bob_agent_sdk import create_sdk_mcp_server, tool
from bob_agent_sdk._http import LoopbackServer
from bob_agent_sdk.tools import McpEndpoint


@tool("add", "Add two numbers", {"a": float, "b": float})
async def add(args):
    return str(args["a"] + args["b"])


@tool("fail", "Always fails", {"type": "object", "properties": {}})
async def fail(args):
    raise ValueError("no luck")


def post(url: str, token: str, body: dict) -> tuple[int, dict | None]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        return error.code, None


def call(server: LoopbackServer, method: str, params: dict | None = None, token=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return asyncio.to_thread(post, server.url("/mcp"), token or server.token, body)


def test_tool_schema_shorthand() -> None:
    assert add.input_schema == {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    }
    assert fail.input_schema == {"type": "object", "properties": {}}


def test_mcp_server_round_trip() -> None:
    async def run() -> None:
        server = LoopbackServer()
        server.route("/mcp", McpEndpoint(create_sdk_mcp_server("calc", tools=[add, fail])).handle)
        await server.start()
        try:
            status, reply = await call(server, "initialize", {"protocolVersion": "2025-06-18"})
            assert status == 200
            assert reply["result"]["protocolVersion"] == "2025-06-18"
            assert reply["result"]["serverInfo"]["name"] == "calc"

            _, reply = await call(server, "tools/list")
            assert [t["name"] for t in reply["result"]["tools"]] == ["add", "fail"]

            _, reply = await call(
                server, "tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}
            )
            assert reply["result"] == {"content": [{"type": "text", "text": "5"}]}

            _, reply = await call(server, "tools/call", {"name": "fail", "arguments": {}})
            assert reply["result"]["isError"] is True
            assert "no luck" in reply["result"]["content"][0]["text"]

            _, reply = await call(server, "tools/call", {"name": "nope"})
            assert reply["error"]["code"] == -32602

            _, reply = await call(server, "resources/list")
            assert reply["error"]["code"] == -32601

            status, _ = await call(server, "tools/list", token="wrong")
            assert status == 401

            notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            status, _ = await asyncio.to_thread(
                post, server.url("/mcp"), server.token, notification
            )
            assert status == 202

            status, _ = await asyncio.to_thread(
                post, server.url("/other"), server.token, notification
            )
            assert status == 404
        finally:
            await server.stop()

    asyncio.run(run())

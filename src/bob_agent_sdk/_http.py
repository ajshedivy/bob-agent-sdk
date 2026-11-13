"""A small HTTP server on a loopback port.

Bob reaches the SDK's in-process tools and hook callbacks through it. Every
request must carry a random bearer token, so other local processes cannot
call in.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

# A handler takes the decoded JSON body and returns (HTTP status line, JSON reply or None).
Handler = Callable[[Any], Awaitable[tuple[str, Any]]]

# Tool results in hook payloads can be large.
_MAX_BODY_BYTES = 64 * 1024 * 1024
_MAX_HEADERS = 100


class LoopbackServer:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self._routes: dict[str, Handler] = {}
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    def route(self, path: str, handler: Handler) -> None:
        self._routes[path] = handler

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    def url(self, path: str) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}{path}"

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        await self._server.wait_closed()
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while request_line := await reader.readline():
                method, path = request_line.decode("latin-1").split(" ")[:2]
                headers: dict[str, str] = {}
                while (line := await reader.readline()) not in (b"\r\n", b"\n", b""):
                    key, _, value = line.decode("latin-1").partition(":")
                    headers[key.strip().lower()] = value.strip()
                    if len(headers) > _MAX_HEADERS:
                        raise ValueError("too many headers")
                # Check the token before reading the body, so that a caller
                # without it cannot make this process buffer data.
                authorized = secrets.compare_digest(
                    headers.get("authorization", "").encode("latin-1"),
                    f"Bearer {self.token}".encode(),
                )
                length = int(headers.get("content-length", 0))
                if not authorized or length > _MAX_BODY_BYTES:
                    status = "401 Unauthorized" if not authorized else "413 Content Too Large"
                    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
                    await writer.drain()
                    return
                body = await reader.readexactly(length)
                status, payload = await self._respond(method, path, body)
                data = b"" if payload is None else json.dumps(payload).encode()
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n".encode()
                    + data
                )
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _respond(self, method: str, path: str, body: bytes) -> tuple[str, Any]:
        handler = self._routes.get(path)
        if handler is None:
            return "404 Not Found", None
        if method != "POST":
            # No server-initiated stream. The MCP spec allows 405 for GET.
            return "405 Method Not Allowed", None
        return await handler(json.loads(body))

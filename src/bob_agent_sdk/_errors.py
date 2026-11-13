"""Exceptions raised by the SDK."""

from __future__ import annotations

from typing import Any

from acp import RequestError

_AUTH_REQUIRED = -32000


class BobSDKError(Exception):
    """Base class for all SDK errors."""


class CLINotFoundError(BobSDKError):
    """The `bob` executable could not be found."""


class CLIConnectionError(BobSDKError):
    """The SDK is not connected to a Bob process, or the connection broke."""


class ProcessError(BobSDKError):
    """The Bob process exited unexpectedly."""

    def __init__(self, exit_code: int | None, stderr: str) -> None:
        self.exit_code = exit_code
        self.stderr = stderr
        message = f"Bob exited unexpectedly (exit code {exit_code})"
        if stderr:
            message += f"\n{stderr}"
        super().__init__(message)


class ACPError(BobSDKError):
    """Bob answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"{message} (code {code})")

    @classmethod
    def from_request_error(cls, error: RequestError) -> ACPError:
        kind = AuthenticationError if error.code == _AUTH_REQUIRED else ACPError
        return kind(error.code, str(error), error.data)


class AuthenticationError(ACPError):
    """Bob has no valid login. Run `bob` once to log in, or set BOB_API_KEY."""

"""HTTP client for the bus REST API, used by the CLI and the monitor. Stdlib only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class BusUnreachable(Exception):
    """Nothing answers at the bus URL (the server is not running, or the port is wrong)."""


class BusRequestError(Exception):
    """The bus answered with an error (unknown operation, invalid arguments, a rule violation)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path: str, payload: dict[str, Any] | None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                message = json.loads(raw)["error"]
            except (ValueError, KeyError, TypeError):
                message = raw or e.reason
            raise BusRequestError(e.code, str(message)) from None
        except (urllib.error.URLError, OSError) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            raise BusUnreachable(f"agent bus unreachable at {self.base_url}: {reason}") from None

    def call(self, op: str, args: dict[str, Any] | None = None) -> Any:
        """Runs a bus operation (see `agentbus --help` for the list) and returns its result."""
        return self._request(f"/api/call/{op}", args or {})["result"]

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = self._request("/api/health", None)
        return result

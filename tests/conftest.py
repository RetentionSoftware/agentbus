from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from agentbus.server import ServerConfig, create_app
from agentbus.store import Store

PROTOCOL = "docs/agents/PROTOCOL.md"


class FakeClock:
    """A controllable time source for the store."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> Iterator[Store]:
    s = Store(tmp_path / "bus.db", protocol=PROTOCOL, clock=clock)
    yield s
    s.close()


@dataclass
class RunningBus:
    url: str
    port: int
    store: Store

    def post(self, path: str, payload: Any, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """Raw POST; returns (status, parsed JSON body)."""
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            headers=headers if headers is not None else {"Content-Type": "application/json"},
        )
        return _send(req)

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        return _send(urllib.request.Request(f"{self.url}{path}", headers=headers or {}))

    def call(self, op: str, **args: Any) -> Any:
        status, body = self.post(f"/api/call/{op}", args)
        assert status == 200, body
        return body["result"]


def _send(req: urllib.request.Request) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[RunningBus]:
    """A real server (MCP + REST) on a free loopback port, running in a background thread."""
    store = Store(tmp_path / "bus.db", protocol=PROTOCOL)
    config = ServerConfig(host="127.0.0.1", port=0, db=store.db_path, project_name="demo", protocol=PROTOCOL)
    server = uvicorn.Server(uvicorn.Config(create_app(store, config), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        assert thread.is_alive(), "server thread died"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield RunningBus(url=f"http://127.0.0.1:{port}", port=port, store=store)
    finally:
        server.should_exit = True
        thread.join(15)
        store.close()


def free_port() -> int:
    """A loopback port that is free right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@pytest.fixture
def dead_port() -> int:
    """A loopback port on which nothing listens."""
    return free_port()

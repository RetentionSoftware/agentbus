from __future__ import annotations

import threading
import time
from typing import Any

import anyio
import pytest
from mcp import Client

from agentbus import __version__
from agentbus.schema import SCHEMA_VERSION

from .conftest import PROTOCOL, RunningBus

# The MCP tool set is a public API used by agents' protocols: changing it is a breaking change.
TOOLS = {
    "register",
    "set_status",
    "agents",
    "send",
    "inbox",
    "wait_for_messages",
    "history",
    "task_list",
    "task_get",
    "task_update",
    "task_create",
    "claim",
    "release",
    "claims",
    "board",
}


# ---------------------------------------------------------------------- REST
def test_health(bus: RunningBus) -> None:
    status, body = bus.get("/api/health")
    assert status == 200
    assert body == {
        "ok": True,
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "project": "demo",
        "db": str(bus.store.db_path),
        "agents": 0,
    }


def test_call_operations(bus: RunningBus) -> None:
    assert bus.call("register", name="a", role="x")["registered"] == "a"
    bus.call("register", name="b", role="x")
    bus.call("send", agent="a", to="b", body="hi")
    assert bus.call("unread_count", agent="b") == 1
    assert bus.call("agents")[0]["name"] == "a"
    assert bus.call("events", after=-1, limit=1)["events"][0]["type"] == "message"


def test_call_converts_string_arguments(bus: RunningBus) -> None:
    # The CLI passes key=value pairs as strings; the server converts them to the declared types.
    bus.call("register", name="a", role="x")
    bus.call("register", name="b", role="x")
    for i in range(3):
        bus.call("send", agent="a", to="b", body=f"m{i}")
    assert len(bus.call("inbox", agent="b", limit="2", include_read="false")["messages"]) == 2


@pytest.mark.parametrize(
    ("op", "payload", "status", "error"),
    [
        ("nope", {}, 404, "unknown operation 'nope'; available: register"),
        ("agents", b"{not json", 400, "invalid JSON"),
        ("agents", b"[1, 2]", 400, "must be a JSON object"),
        ("send", {"agent": "a"}, 400, "to: Missing required argument"),
        ("inbox", {"agent": "a", "limit": "many"}, 400, "limit: Input should be a valid integer"),
        ("inbox", {"agent": "a", "colour": "red"}, 400, "colour: Unexpected keyword argument"),
        ("inbox", {"agent": "ghost"}, 400, "Agent 'ghost' is not registered"),
    ],
)
def test_call_errors(bus: RunningBus, op: str, payload: Any, status: int, error: str) -> None:
    got_status, body = bus.post(f"/api/call/{op}", payload)
    assert got_status == status
    assert error in body["error"]


@pytest.mark.parametrize("path", ["/api/call/board", "/api/hook"])
def test_browser_requests_are_rejected(bus: RunningBus, path: str) -> None:
    # A web page can send a "simple" cross-origin POST (text/plain, no preflight) or reach the API via DNS rebinding.
    json_type = {"Content-Type": "application/json"}
    assert bus.post(path, {}, headers={"Content-Type": "text/plain"}) == (
        415,
        {"error": "Content-Type must be application/json"},
    )
    assert bus.post(path, {}, headers={})[0] == 415
    assert bus.post(path, {}, headers={**json_type, "Origin": "https://example.com"}) == (
        403,
        {"error": "browser requests are not allowed"},
    )
    assert bus.post(path, {}, headers={**json_type, "Host": "attacker.example:8765"}) == (
        403,
        {"error": "forbidden host"},
    )


def test_health_rejects_browsers_too(bus: RunningBus) -> None:
    assert bus.get("/api/health", headers={"Origin": "https://example.com"})[0] == 403
    assert bus.get("/api/health", headers={"Host": "attacker.example"})[0] == 403


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_host_names_are_accepted(bus: RunningBus, host: str) -> None:
    assert bus.get("/api/health", headers={"Host": f"{host}:{bus.port}"})[0] == 200


def test_mcp_endpoint_rejects_foreign_origins(bus: RunningBus) -> None:
    status, _ = bus.post(
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Origin": "https://example.com",
        },
    )
    assert status == 403


def test_hook_endpoint(bus: RunningBus) -> None:
    bus.call("register", name="api", role="x", session_key="s1")
    bus.call("register", name="director", role="x")
    bus.call("send", agent="director", to="api", body="ping")
    status, body = bus.post("/api/hook", {"event": "post_tool", "session_id": "s1", "cwd": "/w"})
    assert status == 200
    assert "ping" in body["context"]
    assert bus.post("/api/hook", {"event": "post_tool", "session_id": "s1"}) == (200, {"context": ""})
    status, body = bus.post("/api/hook", {"event": "stop", "session_id": "s1"})
    assert status == 400
    assert "Invalid hook event" in body["error"]


# ---------------------------------------------------------------------- MCP
def mcp_session(bus: RunningBus, scenario: Any) -> Any:
    async def run() -> Any:
        async with Client(f"{bus.url}/mcp") as client:
            return await scenario(client)

    return anyio.run(run)


def text_of(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def test_mcp_tools_and_instructions(bus: RunningBus) -> None:
    async def scenario(client: Client) -> tuple[set[str], str | None]:
        tools = await client.list_tools()
        return {t.name for t in tools.tools}, client.instructions

    names, instructions = mcp_session(bus, scenario)
    assert names == TOOLS
    assert instructions is not None
    assert "'demo'" in instructions
    assert PROTOCOL in instructions


def test_mcp_calls(bus: RunningBus) -> None:
    async def scenario(client: Client) -> list[Any]:
        await client.call_tool("register", {"name": "a", "role": "x"})
        await client.call_tool("register", {"name": "b", "role": "x"})
        sent = await client.call_tool("send", {"agent": "a", "to": "b", "body": "over MCP", "priority": "urgent"})
        inbox = await client.call_tool("inbox", {"agent": "b"})
        return [sent, inbox]

    sent, inbox = mcp_session(bus, scenario)
    assert not sent.is_error
    assert not inbox.is_error
    assert inbox.structured_content["messages"][0]["body"] == "over MCP"


def test_mcp_rule_violations_are_shown_to_the_agent(bus: RunningBus) -> None:
    async def scenario(client: Client) -> Any:
        return await client.call_tool("send", {"agent": "ghost", "to": "all", "body": "x"})

    result = mcp_session(bus, scenario)
    assert result.is_error
    assert "Agent 'ghost' is not registered. Call register() first." in text_of(result)


def test_wait_for_messages_wakes_up_on_mail(bus: RunningBus) -> None:
    bus.call("register", name="a", role="x")
    bus.call("register", name="b", role="x")

    def send_later() -> None:
        time.sleep(1.5)
        bus.call("send", agent="a", to="b", body="wake up")

    threading.Thread(target=send_later).start()

    async def scenario(client: Client) -> Any:
        started = time.monotonic()
        result = await client.call_tool("wait_for_messages", {"agent": "b", "timeout_s": 30})
        return result, time.monotonic() - started

    result, waited = mcp_session(bus, scenario)
    assert result.structured_content["messages"][0]["body"] == "wake up"
    assert 1.0 < waited < 10


def test_wait_for_messages_of_unknown_agent(bus: RunningBus) -> None:
    async def scenario(client: Client) -> Any:
        return await client.call_tool("wait_for_messages", {"agent": "ghost", "timeout_s": 1})

    result = mcp_session(bus, scenario)
    assert result.is_error
    assert "not registered" in text_of(result)


def test_oversized_body_is_refused(bus: RunningBus) -> None:
    status, body = bus.post("/api/call/agents", b"{" + b" " * (1 << 20) + b"}")
    assert status == 413
    assert "larger than" in body["error"]

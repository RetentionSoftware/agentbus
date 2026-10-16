"""The bus server: MCP over streamable HTTP at /mcp (for agents) and a small REST API at /api (for the hook, the CLI
and the monitor), both over one Store.
"""

from __future__ import annotations

import functools
import json
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

import anyio.to_thread
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError, validate_call
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .config import REST_OPERATIONS, base_url, check_host, check_port
from .schema import SCHEMA_VERSION
from .store import BusError, Json, Store

P = ParamSpec("P")
R = TypeVar("R")

INSTRUCTIONS = """\
Coordination bus for the agent team of the project '{project}'. Agent protocol: {protocol}.
Essentials: register() once per session (pass the session_key the bus gave you at session start);
keep set_status() current; claim() paths outside your task's owned area before editing them and
release() them when done; move your task through task_update(); talk via send(). New messages are
also delivered automatically into your context after tool calls. Every call takes your agent name.
"""

# Host names a request to the REST API may be addressed to. Anything else is a DNS-rebinding attempt.
REST_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_REST_BODY = 1 << 20


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    db: Path
    project_name: str
    protocol: str


def _bus_errors_as_tool_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """MCP shows the text of ToolError to the agent and hides every other exception; BusError is meant to be seen."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except BusError as e:
            raise ToolError(str(e)) from e

    return wrapper


def build_mcp(store: Store, project_name: str) -> MCPServer:
    """The MCP server with the agent-facing tools. Tool names, parameters and results are a public API."""
    mcp = MCPServer(
        "agentbus",
        version=__version__,
        log_level="WARNING",
        instructions=INSTRUCTIONS.format(project=project_name, protocol=store.protocol),
    )
    tool = mcp.tool

    @tool()
    @_bus_errors_as_tool_errors
    def register(name: str, role: str, session_key: str = "", cwd: str = "", branch: str = "") -> Json:
        """Join the bus (or rejoin after a restart). name: short unique id such as 'backend-api'.
        role: one line about what you do. session_key: the key the bus printed at your session start.
        cwd: your working directory (absolute path). branch: your git branch."""
        return store.register(name, role, session_key, cwd, branch)

    @tool()
    @_bus_errors_as_tool_errors
    def set_status(agent: str, state: str, summary: str, task_id: str = "") -> Json:
        """Publish what you are doing. state: idle | working | blocked | review | done.
        summary: one or two sentences, concrete (what, where). Update it whenever your focus changes."""
        return store.set_status(agent, state, summary, task_id)

    @tool()
    @_bus_errors_as_tool_errors
    def agents() -> list[Json]:
        """Roster: every agent with state, summary, task and whether it was seen in the last 15 minutes."""
        return store.agents()

    @tool()
    @_bus_errors_as_tool_errors
    def send(
        agent: str,
        to: str,
        body: str,
        subject: str = "",
        kind: str = "note",
        priority: str = "normal",
        reply_to: int = 0,
        task_id: str = "",
    ) -> Json:
        """Send a message. to: an agent name, 'director', or 'all'.
        kind: note | question | answer | contract_change | review_request | decision | alert.
        priority: normal | urgent (urgent = the recipient should act before continuing).
        reply_to: id of the message you answer."""
        return store.send(agent, to, body, subject, kind, priority, reply_to, task_id)

    @tool()
    @_bus_errors_as_tool_errors
    def inbox(agent: str, include_read: bool = False, limit: int = 20) -> Json:
        """Read your messages (unread first, urgent first) and mark them read.
        include_read=True returns your recent history instead."""
        return store.inbox(agent, include_read, limit)

    @tool()
    async def wait_for_messages(agent: str, timeout_s: int = 120) -> Json:
        """Block until a message arrives for you or the timeout passes (max 600 s), then return the inbox.
        Use when you are blocked on an answer and have nothing else to do."""
        try:
            deadline = anyio.current_time() + max(1, min(timeout_s, 600))
            while await anyio.to_thread.run_sync(store.unread_count, agent) == 0 and anyio.current_time() < deadline:
                await anyio.sleep(1.0)
            return await anyio.to_thread.run_sync(store.inbox, agent)
        except BusError as e:
            raise ToolError(str(e)) from e

    @tool()
    @_bus_errors_as_tool_errors
    def history(kind: str = "", agent: str = "", task_id: str = "", limit: int = 30) -> Json:
        """Search past messages, e.g. kind='decision' for team decisions or task_id='T-003' for a task thread."""
        return store.history(kind, agent, task_id, limit)

    @tool()
    @_bus_errors_as_tool_errors
    def task_list(state: str = "", owner: str = "") -> Json:
        """List tasks, optionally filtered by state (todo | in_progress | blocked | review | done | cancelled)
        or owner."""
        return store.task_list(state, owner)

    @tool()
    @_bus_errors_as_tool_errors
    def task_get(task_id: str) -> Json:
        """One task with its full state history. The task specification itself is the markdown file at spec_path."""
        return store.task_get(task_id)

    @tool()
    @_bus_errors_as_tool_errors
    def task_update(agent: str, task_id: str, state: str, note: str = "", owner: str = "") -> Json:
        """Move a task: todo | in_progress | blocked | review | done | cancelled. Taking an unowned task to
        in_progress makes you its owner. Moving to review/blocked/done notifies the director automatically;
        put the details (branch, what changed, what is blocking) in note."""
        return store.task_update(agent, task_id, state, note, owner)

    @tool()
    @_bus_errors_as_tool_errors
    def task_create(
        agent: str,
        task_id: str,
        title: str,
        spec_path: str = "",
        depends_on: list[str] | None = None,
        owner: str = "",
        wave: str = "",
    ) -> Json:
        """Create a task (normally the director does this). spec_path: repo-relative path of the task markdown."""
        return store.task_create(agent, task_id, title, spec_path, depends_on, owner, wave)

    @tool()
    @_bus_errors_as_tool_errors
    def claim(agent: str, path: str, reason: str, ttl_hours: float = 8, shared: bool = False) -> Json:
        """Announce that you are editing a repo-relative path or glob (e.g. 'src/net/**').
        Returns ok=False with the conflicting claims if someone else holds an overlapping one; do not edit then.
        Re-claiming the same path extends it. shared=True allows overlap with other shared claims."""
        return store.claim(agent, path, reason, ttl_hours, shared)

    @tool()
    @_bus_errors_as_tool_errors
    def release(agent: str, path: str = "", claim_id: int = 0) -> Json:
        """Release one claim (by path or claim_id) or, with neither given, all of your claims."""
        return store.release(agent, path, claim_id)

    @tool()
    @_bus_errors_as_tool_errors
    def claims(agent: str = "") -> Json:
        """Active claims of everyone (or of one agent)."""
        return store.claims(agent)

    @tool()
    @_bus_errors_as_tool_errors
    def board() -> Json:
        """Whole-team overview: agents, tasks by state, active claims, recent decisions and contract changes."""
        return store.board()

    return mcp


def _host_name(host_header: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '[::1]', 'localhost' -> 'localhost'."""
    if host_header.startswith("["):
        return host_header[: host_header.find("]") + 1]
    return host_header.rsplit(":", 1)[0]


class RestError(Exception):
    """A failed REST request: HTTP status and a message for the caller."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

    def response(self) -> JSONResponse:
        return JSONResponse({"error": str(self)}, status_code=self.status)


async def _read_request(request: Request, *, post: bool) -> dict[str, Any]:
    """Validates a REST request and returns its JSON object body ({} for GET or an empty body).

    Keeps web browsers out: a page open in a browser on this machine can send "simple" cross-origin POSTs to
    127.0.0.1 (text/plain, no CORS preflight) and could otherwise inject messages into agents' contexts or erase
    data; DNS rebinding could reach the API under a foreign Host. The CLI and the hook send no Origin, use
    application/json and a loopback Host. (The MCP endpoint is protected the same way by the MCP SDK.)
    """
    # The body is read before any rejection: closing a connection with unread request data makes some systems
    # (Windows) reset it, and the client would see a connection error instead of the reason.
    body, size = bytearray(), 0
    async for chunk in request.stream():
        size += len(chunk)
        if size <= MAX_REST_BODY:
            body += chunk
    if size > MAX_REST_BODY:
        raise RestError(413, f"request body larger than {MAX_REST_BODY} bytes")
    if _host_name(request.headers.get("host", "")) not in REST_HOSTS:
        raise RestError(403, "forbidden host")
    if "origin" in request.headers:
        raise RestError(403, "browser requests are not allowed")
    if not post:
        return {}
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise RestError(415, "Content-Type must be application/json")
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError as e:
        raise RestError(400, f"invalid JSON: {e}") from None
    if not isinstance(data, dict):
        raise RestError(400, "the request body must be a JSON object")
    return data


async def _invoke(fn: Callable[..., Any], args: dict[str, Any]) -> Any:
    """Runs a validated store operation in a worker thread (it may wait for the store lock)."""
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, **args))
    except ValidationError as e:
        raise RestError(
            400, "; ".join(f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}" for err in e.errors())
        ) from None
    except BusError as e:
        raise RestError(400, str(e)) from None


def rest_routes(store: Store, config: ServerConfig) -> list[Route]:
    """GET /api/health, POST /api/call/<op> (body: the operation's arguments), POST /api/hook (the hook's endpoint).

    Responses are {"result": ...} or {"error": "..."} with a 4xx status; /api/hook answers {"context": "..."}.
    """
    ops: dict[str, Callable[..., Any]] = {name: validate_call(getattr(store, name)) for name in REST_OPERATIONS}
    hook = validate_call(store.hook)

    async def api_health(request: Request) -> JSONResponse:
        try:
            await _read_request(request, post=False)
            agents = await _invoke(store.agents, {})
        except RestError as e:
            return e.response()
        return JSONResponse(
            {
                "ok": True,
                "version": __version__,
                "schema_version": SCHEMA_VERSION,
                "project": config.project_name,
                "db": str(store.db_path),
                "agents": len(agents),
            }
        )

    async def api_call(request: Request) -> JSONResponse:
        try:
            args = await _read_request(request, post=True)
            name = request.path_params["op"]
            if name not in ops:
                raise RestError(404, f"unknown operation '{name}'; available: {', '.join(REST_OPERATIONS)}")
            return JSONResponse({"result": await _invoke(ops[name], args)})
        except RestError as e:
            return e.response()

    async def api_hook(request: Request) -> JSONResponse:
        try:
            args = await _read_request(request, post=True)
            context = await _invoke(hook, {k: args.get(k, "") for k in ("event", "session_id", "cwd")})
        except RestError as e:
            return e.response()
        return JSONResponse({"context": context})

    return [
        Route("/api/health", api_health, methods=["GET"]),
        Route("/api/call/{op}", api_call, methods=["POST"]),
        Route("/api/hook", api_hook, methods=["POST"]),
    ]


def create_app(store: Store, config: ServerConfig) -> Starlette:
    """The ASGI application: MCP at /mcp plus the REST API, all over `store`."""
    app = build_mcp(store, config.project_name).streamable_http_app(stateless_http=True, host=config.host)
    app.router.routes.extend(rest_routes(store, config))
    return app


class ServeError(Exception):
    """The server cannot start; the message says why."""


def _check_port_free(host: str, port: int) -> None:
    """Fails early, before the pid file of a server that may already run on this port is overwritten."""
    if port == 0:
        return
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as e:
            raise ServeError(
                f"cannot listen on {base_url(host, port)}: {e.strerror or e}; is another bus running there? "
                "(`agentbus health` shows its project and database)"
            ) from None


def pid_file(db: Path) -> Path:
    """Where a running server records its process id: next to its database."""
    return db.parent / "server.pid"


def serve(config: ServerConfig) -> None:
    """Runs the bus in the foreground until interrupted. Writes the process id next to the database while running."""
    check_host(config.host)
    check_port(config.port)
    _check_port_free(config.host, config.port)
    store = Store(config.db, protocol=config.protocol)
    pid_path = pid_file(config.db)
    try:
        for note in store.migration_notes:
            print(f"agentbus: {note}", flush=True)
        app = create_app(store, config)
        print(
            f"agentbus {__version__}: project '{config.project_name}', db {config.db}, "
            f"{base_url(config.host, config.port)}/mcp",
            flush=True,
        )
        pid_path.write_text(str(os.getpid()), encoding="ascii")
        uvicorn.run(app, host=config.host, port=config.port, log_level="warning")
    finally:
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
        store.close()

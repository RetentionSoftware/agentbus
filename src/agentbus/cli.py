"""The `agentbus` command: runs the server, the monitor and the hook, and calls bus operations from a shell.

Everything except `serve` uses only the standard library, so client commands start fast.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .client import BusRequestError, BusUnreachable, Client
from .config import (
    DEFAULT_DB,
    DEFAULT_PROTOCOL,
    ENV_DB,
    ENV_PROJECT_NAME,
    ENV_PROTOCOL,
    REST_OPERATIONS,
    ConfigError,
    base_url,
    check_port,
    env_host,
    env_port,
)
from .hook import main as hook_main
from .schema import SchemaError
from .watch import run as watch_run

USAGE = """\
usage: agentbus [--host HOST] [--port PORT] <command> [arguments]

server
  serve [--db PATH] [--project-name NAME] [--protocol PATH]
                               run the bus in the foreground (MCP at /mcp, REST at /api)
clients
  watch [--history N] [--quiet]
                               live activity monitor (--quiet hides reads, deliveries, session starts)
  hook session_start|prompt|post_tool
                               the Claude Code hook (reads the hook payload from stdin)
  health                       is the bus up; its version, project and database
  waitmail agent=NAME [timeout=SECONDS]
                               block until NAME has unread mail (does not mark it read); default 3600 s
  <operation> key=value ...    call a bus operation, e.g.  agentbus send agent=director to=all body="hello"
  <operation> --json '{...}'   the same with the arguments as one JSON object
  version                      print the agentbus version

operations
  register set_status agents send inbox history task_list task_get task_update task_create
  claim release claims board unread_count events purge
  Values starting with '[' or '{' are parsed as JSON (e.g. depends_on='["T-1"]'); everything else is passed as
  a string and converted by the server (limit=5, include_read=true).

options
  --host HOST   bus address, default $AGENTBUS_HOST or 127.0.0.1 (the server only accepts loopback addresses)
  --port PORT   bus port, default $AGENTBUS_PORT or 8765

server environment
  AGENTBUS_DB            database path, default .agentbus/bus.db under the current directory
  AGENTBUS_PROJECT_NAME  project name shown to agents, default the name of the current directory
  AGENTBUS_PROTOCOL      repo-relative path of the agent protocol document, default docs/agents/PROTOCOL.md

exit codes: 0 success, 1 the bus reported an error or is unreachable, 2 usage error
"""


class UsageError(Exception):
    pass


def parse_op_args(items: list[str]) -> dict[str, Any]:
    """`key=value ...` or `--json '{...}'` -> arguments of a bus operation."""
    if items[:1] == ["--json"]:
        if len(items) != 2:
            raise UsageError("--json takes exactly one argument: a JSON object")
        try:
            args = json.loads(items[1])
        except ValueError as e:
            raise UsageError(f"--json: {e}") from None
        if not isinstance(args, dict):
            raise UsageError("--json: expected a JSON object")
        return args
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise UsageError(f"bad argument '{item}', expected key=value")
        out[key] = value
        if value[:1] in ("[", "{"):
            # Not JSON after all (e.g. body="[WIP] ...") keeps the string.
            with contextlib.suppress(ValueError):
                out[key] = json.loads(value)
    return out


def resolve_address(host: str | None, port: int | None) -> tuple[str, int]:
    """Bus address for every command: --host/--port flag, else AGENTBUS_HOST/AGENTBUS_PORT, else the default.

    The hook applies the same precedence on its own (hook.bus_url), since it must not import this module.
    """
    return host or env_host(), check_port(port) if port is not None else env_port()


def print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def cmd_serve(host: str, port: int, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentbus serve", description="Run the bus in the foreground.")
    parser.add_argument(
        "--db",
        default=os.environ.get(ENV_DB) or DEFAULT_DB,
        help=f"database path (default ${ENV_DB} or {DEFAULT_DB} under the current directory)",
    )
    parser.add_argument(
        "--project-name",
        default=os.environ.get(ENV_PROJECT_NAME) or Path.cwd().name,
        help=f"project name shown to agents (default ${ENV_PROJECT_NAME} or the directory name)",
    )
    parser.add_argument(
        "--protocol",
        default=os.environ.get(ENV_PROTOCOL) or DEFAULT_PROTOCOL,
        help=f"repo-relative path of the agent protocol (default ${ENV_PROTOCOL} or {DEFAULT_PROTOCOL})",
    )
    args = parser.parse_args(argv)
    # Imported here: the server pulls in the MCP SDK, which the client commands (and the hook) do not need.
    from .server import ServeError, ServerConfig, serve  # noqa: PLC0415

    try:
        serve(
            ServerConfig(
                host=host, port=port, db=Path(args.db).resolve(), project_name=args.project_name, protocol=args.protocol
            )
        )
    except (SchemaError, ServeError) as e:
        print(f"agentbus: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_watch(client: Client, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentbus watch", description="Live activity monitor of the bus.")
    parser.add_argument("--history", type=int, default=40, help="past events to show on start (default 40)")
    parser.add_argument("--quiet", action="store_true", help="hide message reads, deliveries and session starts")
    args = parser.parse_args(argv)
    return watch_run(client, args.history, args.quiet)


def cmd_waitmail(client: Client, argv: list[str]) -> int:
    args = parse_op_args(argv)
    agent = args.pop("agent", "")
    try:
        timeout = float(args.pop("timeout", 3600))
    except (TypeError, ValueError):
        raise UsageError("waitmail: timeout must be a number of seconds") from None
    if not agent or args:
        raise UsageError("usage: agentbus waitmail agent=NAME [timeout=SECONDS]")
    deadline = time.monotonic() + timeout
    warned = False
    while True:
        try:
            n = client.call("unread_count", {"agent": agent})
            warned = False
            if n:
                print(f"{n} unread message(s) for {agent}")
                return 0
        except BusUnreachable as e:
            if not warned:
                print(f"{e}; retrying", file=sys.stderr)
                warned = True
        if time.monotonic() >= deadline:
            print("timeout, no mail")
            return 0
        time.sleep(2.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentbus", usage=USAGE, add_help=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("command", nargs="?")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if ns.help or not ns.command:
        print(USAGE)
        return 0 if ns.help else 2
    command, rest = ns.command, ns.args
    if command == "hook":
        # First, before anything that can fail: the hook must always exit 0 (exit code 2 blocks the user's prompt).
        return hook_main(rest, host=ns.host, port=ns.port)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # JSON with non-ASCII text, OS error messages in the local language
            stream.reconfigure(encoding="utf-8")
    try:
        if command == "version":
            print(__version__)
            return 0
        host, port = resolve_address(ns.host, ns.port)
        if command == "serve":
            return cmd_serve(host, port, rest)
        client = Client(base_url(host, port))
        if command == "watch":
            return cmd_watch(client, rest)
        if command == "waitmail":
            return cmd_waitmail(client, rest)
        if command == "health":
            print_json(client.health())
            return 0
        if command not in REST_OPERATIONS:
            raise UsageError(f"unknown command '{command}'; run 'agentbus --help'")
        print_json(client.call(command, parse_op_args(rest)))
        return 0
    except (UsageError, ConfigError) as e:
        print(f"agentbus: {e}", file=sys.stderr)
        return 2
    except (BusRequestError, BusUnreachable) as e:
        print(f"agentbus: {e}", file=sys.stderr)
        return 1

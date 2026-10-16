"""Claude Code hook for agentbus: heartbeat and delivery of new messages into the session's context.

This file is self-contained (Python standard library only, no imports from the agentbus package), so it can be
copied into a project and run directly - the fastest way to run a hook that fires after every tool call:

    python .claude/hooks/agentbus_hook.py <session_start|prompt|post_tool>

`agentbus hook <event>` runs the same code. The hook reads the hook payload from stdin, asks the bus what to tell
this session and prints it as additionalContext. It never fails the session: it always exits with code 0, prints
nothing when there is nothing to say and stays silent when the bus is down (except at session start, where it
warns). The bus address comes from AGENTBUS_HOST and AGENTBUS_PORT (default 127.0.0.1:8765).
"""

from __future__ import annotations

import http.client
import json
import os
import sys

EVENTS = {"session_start": "SessionStart", "prompt": "UserPromptSubmit", "post_tool": "PostToolUse"}
TIMEOUT_S = 2.0


def bus_address(host: str | None = None, port: int | None = None) -> tuple[str, str]:
    """The bus address: explicit arguments first, then AGENTBUS_HOST / AGENTBUS_PORT, then 127.0.0.1:8765."""
    name = host or os.environ.get("AGENTBUS_HOST") or "127.0.0.1"
    number = str(port) if port is not None else os.environ.get("AGENTBUS_PORT") or "8765"
    return name, number


def bus_url(host: str | None = None, port: int | None = None) -> str:
    name, number = bus_address(host, port)
    return f"http://[{name}]:{number}" if ":" in name else f"http://{name}:{number}"


def context_for(event: str, payload: dict[str, object], host: str | None = None, port: int | None = None) -> str:
    """The text to add to the session's context for this event ('' = nothing)."""
    # The session's own directory (its worktree), not CLAUDE_PROJECT_DIR, which may point at the main checkout.
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    body = json.dumps({"event": event, "session_id": payload.get("session_id") or "", "cwd": cwd}).encode("utf-8")
    name, number = bus_address(host, port)
    try:
        # http.client rather than urllib.request: it imports in about half the time, and this runs after every tool
        # call of every agent.
        conn = http.client.HTTPConnection(name, int(number), timeout=TIMEOUT_S)
        try:
            conn.request("POST", "/api/hook", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        return str(json.loads(data.decode("utf-8")).get("context") or "")
    except Exception:
        if event != "session_start":
            return ""
        return (
            f"[agent-bus] The agent bus is OFFLINE ({bus_url(host, port)}). Tell the user; do not edit files outside "
            "your own task until it is back."
        )


def main(argv: list[str] | None = None, host: str | None = None, port: int | None = None) -> int:
    """Entry point. argv: [event]; host/port override the environment (`agentbus --host/--port hook ...`)."""
    args = sys.argv[1:] if argv is None else argv
    try:
        event = args[0] if args else ""
        if event not in EVENTS:
            return 0
        try:
            payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        text = context_for(event, payload, host, port)
        if text:
            out = {"hookSpecificOutput": {"hookEventName": EVENTS[event], "additionalContext": text}}
            sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
            sys.stdout.buffer.flush()
    except Exception:
        # A failing hook must never break the agent's session; exit code 2 would even block the user's prompt.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

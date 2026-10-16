"""Live activity monitor: who joined, who wrote to whom (with the text), status, task and claim changes, conflicts.

Read-only, stdlib only; closing it does not affect the bus, and it survives bus restarts.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Any

from .client import BusRequestError, BusUnreachable, Client

RESET, DIM, BOLD, RED = "\033[0m", "\033[2m", "\033[1m", "\033[91m"
TYPE_STYLE = {
    "register": ("JOIN", "\033[92m"),
    "status": ("STATUS", "\033[96m"),
    "message": ("MSG", "\033[93m"),
    "task": ("TASK", "\033[95m"),
    "claim": ("CLAIM", "\033[94m"),
    "release": ("RELEASE", "\033[94m"),
    "conflict": ("CONFLICT", RED),
    "delivered": ("deliver", DIM),
    "read": ("read", DIM),
    "session": ("session", DIM),
}
LOW_LEVEL = {"delivered", "read", "session"}
AGENT_COLORS = [
    "\033[38;5;39m",
    "\033[38;5;208m",
    "\033[38;5;141m",
    "\033[38;5;77m",
    "\033[38;5;204m",
    "\033[38;5;220m",
    "\033[38;5;45m",
    "\033[38;5;170m",
]
POLL_S = 1.0


def paint_agent(name: str) -> str:
    if not name:
        return ""
    if name == "director":
        return f"{BOLD}\033[97m{name}{RESET}"
    return f"{AGENT_COLORS[sum(map(ord, name)) % len(AGENT_COLORS)]}{name}{RESET}"


def render(ev: dict[str, Any], width: int) -> str:
    """One journal event as one colored line, cut to the terminal width."""
    label, color = TYPE_STYLE.get(ev["type"], (ev["type"], ""))
    text = " ".join(ev["text"].split())
    if ev["type"] == "message" and "URGENT" in text:
        color = RED
    who = paint_agent(ev["agent"]) or f"{DIM}?{RESET}"
    if ev["type"] == "message":
        who = f"{who} → {paint_agent(ev['target'])}"
    elif ev["target"]:
        who = f"{who} {DIM}[{ev['target']}]{RESET}"
    prefix = f"{DIM}{ev['time']}{RESET} {color}{label:<8}{RESET} {who}  "
    visible = len(ev["time"]) + 10 + len(ev["agent"]) + len(ev["target"]) + 6
    room = max(40, width - visible)
    if len(text) > room:
        text = text[: room - 1] + "…"
    return prefix + (f"{DIM}{text}{RESET}" if ev["type"] in LOW_LEVEL else text)


def print_roster(client: Client) -> None:
    agents = client.call("agents")
    if not agents:
        print(f"{DIM}  (no agents registered yet){RESET}")
    for a in agents:
        dot = "\033[92m●" if a["online"] else "\033[90m○"
        task = f" [{a['task_id']}]" if a["task_id"] else ""
        print(f"  {dot}{RESET} {paint_agent(a['name'])}{task} {a['state']}: {DIM}{a['summary'] or a['role']}{RESET}")


def run(client: Client, history: int, quiet: bool) -> int:
    """Prints the roster and the last `history` events, then follows the journal until Ctrl+C."""
    if os.name == "nt":
        os.system("")  # enables ANSI escape processing in the Windows console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    last_id, online = -1, None
    try:
        while True:
            try:
                if online is not True:
                    health = client.health()
                    project = "".join(ch for ch in str(health["project"]) if ch.isprintable())
                    print(f"\033]0;agentbus monitor - {project}\007", end="")  # terminal window title
                    print(f"{BOLD}── agentbus · {project} · {client.base_url} ──{RESET}")
                    print_roster(client)
                    print(f"{BOLD}── events ──{RESET}")
                batch = client.call("events", {"after": last_id, "limit": history if last_id < 0 else 200})["events"]
                online = True
                width = shutil.get_terminal_size((140, 40)).columns
                for ev in batch:
                    last_id = max(last_id, ev["id"])
                    if not (quiet and ev["type"] in LOW_LEVEL):
                        print(render(ev, width), flush=True)
                last_id = max(last_id, 0)
            except (BusUnreachable, BusRequestError):
                if online is not False:
                    print(f"{RED}{time.strftime('%H:%M:%S')} bus offline, waiting…{RESET}", flush=True)
                online = False
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        return 0

"""Configuration shared by the server and the clients: defaults, environment variables, the bus URL. Stdlib only."""

from __future__ import annotations

import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DB = os.path.join(".agentbus", "bus.db")
DEFAULT_PROTOCOL = "docs/agents/PROTOCOL.md"

# The bus has no authentication, so it only ever listens on the loopback interface.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

# Store operations reachable through POST /api/call/<op> and the CLI. purge, events and unread_count are not MCP
# tools: they serve the operator's CLI and the monitor, not the agents.
REST_OPERATIONS = (
    "register",
    "set_status",
    "agents",
    "send",
    "inbox",
    "history",
    "task_list",
    "task_get",
    "task_update",
    "task_create",
    "claim",
    "release",
    "claims",
    "board",
    "unread_count",
    "events",
    "purge",
)

ENV_HOST = "AGENTBUS_HOST"
ENV_PORT = "AGENTBUS_PORT"
ENV_DB = "AGENTBUS_DB"
ENV_PROJECT_NAME = "AGENTBUS_PROJECT_NAME"
ENV_PROTOCOL = "AGENTBUS_PROTOCOL"


class ConfigError(Exception):
    """Invalid configuration; the message is meant for the user."""


def env_host() -> str:
    return os.environ.get(ENV_HOST) or DEFAULT_HOST


def env_port() -> int:
    raw = os.environ.get(ENV_PORT)
    if not raw:
        return DEFAULT_PORT
    try:
        return check_port(int(raw))
    except ValueError:
        raise ConfigError(f"{ENV_PORT}={raw!r} is not a port number") from None


def check_port(port: int) -> int:
    if not 0 <= port <= 65535:
        raise ConfigError(f"port {port} is out of range 0-65535")
    return port


def check_host(host: str) -> str:
    """Accepts only loopback addresses: exposing the bus to a network would let anyone act as any agent."""
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"refusing to listen on {host!r}: agentbus has no authentication and only binds to loopback "
            f"({', '.join(LOOPBACK_HOSTS)})"
        )
    return host


def base_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"

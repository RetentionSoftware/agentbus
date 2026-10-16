"""The hook as Claude Code runs it: a separate process, payload on stdin, JSON on stdout, always exit code 0."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import agentbus.hook

from .conftest import RunningBus

HOOK = Path(agentbus.hook.__file__)


def run_hook(
    args: list[str], stdin: bytes, port: int | None, *, script: Path = HOOK, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBUS_")}
    if port is not None:
        environ["AGENTBUS_PORT"] = str(port)
    environ.update(env or {})
    return subprocess.run(
        [sys.executable, str(script), *args], input=stdin, capture_output=True, env=environ, timeout=30, check=False
    )


def payload(session_id: str = "s1", cwd: str = "/work") -> bytes:
    return json.dumps({"session_id": session_id, "cwd": cwd, "hook_event_name": "PostToolUse"}).encode()


def context(proc: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    assert proc.returncode == 0, proc.stderr
    out: dict[str, Any] = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    return out


@pytest.fixture
def with_mail(bus: RunningBus) -> RunningBus:
    bus.call("register", name="api", role="x", session_key="s1")
    bus.call("register", name="director", role="x")
    bus.call("send", agent="director", to="api", body="Привет, review please")
    return bus


@pytest.mark.parametrize(
    ("event", "hook_name"),
    [("post_tool", "PostToolUse"), ("prompt", "UserPromptSubmit"), ("session_start", "SessionStart")],
)
def test_delivers_mail_as_additional_context(with_mail: RunningBus, event: str, hook_name: str) -> None:
    out = context(run_hook([event], payload(), with_mail.port))
    assert out["hookEventName"] == hook_name
    assert "Привет, review please" in out["additionalContext"]
    assert with_mail.call("unread_count", agent="api") == 0


def test_payload_with_bom(with_mail: RunningBus) -> None:
    out = context(run_hook(["post_tool"], b"\xef\xbb\xbf" + payload(), with_mail.port))
    assert "review please" in out["additionalContext"]


def test_nothing_to_say_prints_nothing(bus: RunningBus) -> None:
    bus.call("register", name="api", role="x", session_key="s1")
    proc = run_hook(["post_tool"], payload(), bus.port)
    assert (proc.returncode, proc.stdout) == (0, b"")


def test_bus_down_is_silent_except_at_session_start(dead_port: int) -> None:
    proc = run_hook(["post_tool"], payload(), dead_port)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, b"", b"")
    out = context(run_hook(["session_start"], payload(), dead_port))
    assert "OFFLINE" in out["additionalContext"]
    assert f"127.0.0.1:{dead_port}" in out["additionalContext"]


@pytest.mark.parametrize("stdin", [b"", b"not json", b"[1, 2]", b"\xff\xfe garbage"])
def test_bad_payloads_never_fail(bus: RunningBus, stdin: bytes) -> None:
    proc = run_hook(["session_start"], stdin, bus.port)
    assert "NOT registered" in context(proc)["additionalContext"]


@pytest.mark.parametrize("args", [[], ["stop"], ["post_tool", "extra"]])
def test_unknown_or_missing_event(bus: RunningBus, args: list[str]) -> None:
    proc = run_hook(args, payload(), bus.port)
    assert proc.returncode == 0
    if args[:1] != ["post_tool"]:
        assert proc.stdout == b""


def test_invalid_environment_never_fails() -> None:
    proc = run_hook(["prompt"], payload(), None, env={"AGENTBUS_PORT": "not-a-port", "AGENTBUS_HOST": "::1"})
    assert (proc.returncode, proc.stdout) == (0, b"")


def test_standalone_copy_works(with_mail: RunningBus, tmp_path: Path) -> None:
    # The file is meant to be copied into a project; it must not depend on the agentbus package.
    copy = tmp_path / ".claude" / "hooks" / "agentbus_hook.py"
    copy.parent.mkdir(parents=True)
    shutil.copy(HOOK, copy)
    out = context(run_hook(["post_tool"], payload(), with_mail.port, script=copy))
    assert "review please" in out["additionalContext"]


def test_hook_source_uses_only_the_standard_library() -> None:
    imports = [
        line.split()[1]
        for line in HOOK.read_text(encoding="utf-8").splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert set(imports) <= {"__future__", "http.client", "json", "os", "sys"}


@pytest.mark.parametrize(
    ("host", "port", "env", "expected"),
    [
        (None, None, {}, "http://127.0.0.1:8765"),
        (None, None, {"AGENTBUS_HOST": "localhost", "AGENTBUS_PORT": "9000"}, "http://localhost:9000"),
        ("::1", 9100, {"AGENTBUS_HOST": "localhost", "AGENTBUS_PORT": "9000"}, "http://[::1]:9100"),
        (None, 9100, {"AGENTBUS_PORT": "9000"}, "http://127.0.0.1:9100"),
    ],
)
def test_bus_url_precedence(
    monkeypatch: pytest.MonkeyPatch, host: str | None, port: int | None, env: dict[str, str], expected: str
) -> None:
    for key in ("AGENTBUS_HOST", "AGENTBUS_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert agentbus.hook.bus_url(host, port) == expected

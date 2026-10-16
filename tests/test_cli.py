from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from agentbus import __version__, cli
from agentbus.cli import UsageError, parse_op_args, resolve_address

from .conftest import RunningBus, free_port


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("AGENTBUS_"):
            monkeypatch.delenv(key)
    # Never the default port: a real bus may be running on this machine.
    monkeypatch.setenv("AGENTBUS_PORT", str(free_port()))


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any, str]:
    """Runs the CLI in-process; returns (exit code, stdout parsed as JSON when possible, stderr)."""
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    try:
        return code, json.loads(out), err
    except ValueError:
        return code, out, err


def env_without_agentbus(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTBUS_")}
    env.update(extra)
    return env


# ---------------------------------------------------------------------- argument parsing
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], {}),
        (["agent=a", "body=hello world", "limit=5"], {"agent": "a", "body": "hello world", "limit": "5"}),
        (['depends_on=["T-1", "T-2"]'], {"depends_on": ["T-1", "T-2"]}),
        (["body=[WIP] not json"], {"body": "[WIP] not json"}),
        (["body=a=b"], {"body": "a=b"}),
        (["body="], {"body": ""}),
        (["--json", '{"limit": 5, "include_read": true}'], {"limit": 5, "include_read": True}),
    ],
)
def test_parse_op_args(argv: list[str], expected: dict[str, Any]) -> None:
    assert parse_op_args(argv) == expected


@pytest.mark.parametrize(
    "argv", [["novalue"], ["=x"], ["--json"], ["--json", "[1]"], ["--json", "{bad"], ["--json", "{}", "extra"]]
)
def test_parse_op_args_errors(argv: list[str]) -> None:
    with pytest.raises(UsageError):
        parse_op_args(argv)


# ---------------------------------------------------------------------- address precedence: flag > env > default
def test_resolve_address_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTBUS_PORT")
    assert resolve_address(None, None) == ("127.0.0.1", 8765)
    monkeypatch.setenv("AGENTBUS_HOST", "localhost")
    monkeypatch.setenv("AGENTBUS_PORT", "9000")
    assert resolve_address(None, None) == ("localhost", 9000)
    assert resolve_address("::1", 9100) == ("::1", 9100)
    assert resolve_address(None, 9100) == ("localhost", 9100)


@pytest.mark.parametrize("command", [["health"], ["agents"], ["waitmail", "agent=a", "timeout=0"]])
def test_every_client_command_honours_flag_over_env(
    bus: RunningBus,
    dead_port: int,
    command: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus.call("register", name="a", role="x")

    def reached_bus(*argv: str) -> bool:
        code, _, err = run(capsys, *argv)
        return code == 0 and "unreachable" not in err

    monkeypatch.setenv("AGENTBUS_PORT", str(dead_port))
    assert not reached_bus(*command)  # env points at nothing
    assert reached_bus("--port", str(bus.port), *command)  # the flag wins
    monkeypatch.setenv("AGENTBUS_PORT", str(bus.port))
    assert reached_bus(*command)  # env is used without a flag


def test_watch_honours_flag_over_env(bus: RunningBus, dead_port: int) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentbus", "--port", str(bus.port), "watch", "--history", "5"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env_without_agentbus(AGENTBUS_PORT=str(dead_port)),
    )
    try:
        time.sleep(3)
    finally:
        proc.kill()
    out = proc.communicate()[0].decode("utf-8")
    assert f"127.0.0.1:{bus.port}" in out
    assert "offline" not in out


def test_hook_honours_flag_over_env(bus: RunningBus, dead_port: int) -> None:
    bus.call("register", name="api", role="x", session_key="s1")
    bus.call("register", name="director", role="x")
    bus.call("send", agent="director", to="api", body="via flag")
    proc = subprocess.run(
        [sys.executable, "-m", "agentbus", "--port", str(bus.port), "hook", "post_tool"],
        input=json.dumps({"session_id": "s1"}).encode(),
        capture_output=True,
        timeout=30,
        env=env_without_agentbus(AGENTBUS_PORT=str(dead_port)),
        check=False,
    )
    assert proc.returncode == 0
    assert "via flag" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_hook_never_fails_even_with_bad_environment() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "agentbus", "hook", "prompt"],
        input=b"{}",
        capture_output=True,
        timeout=30,
        env=env_without_agentbus(AGENTBUS_PORT="not-a-port"),
        check=False,
    )
    assert (proc.returncode, proc.stdout) == (0, b"")


def wait_health(port: int, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as resp:
                health: dict[str, Any] = json.loads(resp.read())
                return health
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def test_serve_honours_flags_over_env(tmp_path: Path, dead_port: int) -> None:
    port = free_port()
    db = tmp_path / "state" / "bus.db"
    env = env_without_agentbus(
        AGENTBUS_PORT=str(dead_port),
        AGENTBUS_DB=str(tmp_path / "env.db"),
        AGENTBUS_PROJECT_NAME="from-env",
        AGENTBUS_PROTOCOL="ENV.md",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agentbus",
            "--port",
            str(port),
            "serve",
            "--db",
            str(db),
            "--project-name",
            "from-flag",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        health = wait_health(port)
        assert health["project"] == "from-flag"
        assert health["db"] == str(db)
        pid = int((db.parent / "server.pid").read_text(encoding="ascii"))
        if os.name != "nt":  # on Windows a venv's python.exe is a launcher that runs the interpreter as a child
            assert pid == proc.pid
        assert not (tmp_path / "env.db").exists()
    finally:
        proc.kill()
        out = proc.communicate()[0].decode("utf-8", "replace")
    assert "created schema v1" in out


def test_serve_defaults_come_from_env_then_current_directory(tmp_path: Path) -> None:
    port = free_port()
    project = tmp_path / "my-project"
    project.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentbus", "serve"],
        cwd=project,
        env=env_without_agentbus(AGENTBUS_PORT=str(port), AGENTBUS_PROTOCOL="AGENTS.md"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        health = wait_health(port)
    finally:
        proc.kill()
        proc.wait()
    assert health["project"] == "my-project"
    assert health["db"] == str(project / ".agentbus" / "bus.db")


@pytest.mark.parametrize(
    "argv", [["--host", "0.0.0.0", "serve"], ["--host", "192.168.1.5", "serve"], ["--port", "70000", "serve"]]
)
def test_serve_refuses_non_loopback_and_bad_ports(
    argv: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = run(capsys, *argv)
    assert code == 2
    assert "refusing to listen" in err or "out of range" in err
    assert not (tmp_path / ".agentbus").exists()


def test_serve_refuses_non_loopback_host_from_env(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_HOST", "0.0.0.0")
    assert run(capsys, "serve")[0] == 2


def test_serve_reports_a_foreign_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "bus.db"
    db.write_bytes(b"not sqlite" * 100)
    code, _, err = run(capsys, "serve", "--db", str(db))
    assert code == 1
    assert "cannot use" in err


# ---------------------------------------------------------------------- commands
def test_operations(bus: RunningBus, capsys: pytest.CaptureFixture[str]) -> None:
    port = ["--port", str(bus.port)]
    assert run(capsys, *port, "register", "name=a", "role=x")[1]["registered"] == "a"
    run(capsys, *port, "register", "name=b", "role=x")
    code, out, _ = run(capsys, *port, "send", "agent=a", "to=b", "body=привет, [b]", "priority=urgent")
    assert (code, out["ok"]) == (0, True)
    code, out, _ = run(capsys, *port, "inbox", "--json", '{"agent": "b", "limit": 1}')
    assert out["messages"][0]["body"] == "привет, [b]"
    code, out, _ = run(capsys, *port, "task_create", "agent=a", "task_id=T-1", "title=t", 'depends_on=["T-0"]')
    assert out["task"]["depends_on"] == ["T-0"]


def test_operation_errors(bus: RunningBus, dead_port: int, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "--port", str(bus.port), "inbox", "agent=ghost")
    assert code == 1
    assert "not registered" in err
    code, _, err = run(capsys, "--port", str(dead_port), "board")
    assert code == 1
    assert "unreachable" in err
    assert run(capsys, "frobnicate")[0] == 2
    assert run(capsys, "send", "oops")[0] == 2


def test_waitmail(bus: RunningBus, capsys: pytest.CaptureFixture[str]) -> None:
    port = ["--port", str(bus.port)]
    bus.call("register", name="director", role="x")
    bus.call("register", name="api", role="x")
    assert run(capsys, *port, "waitmail", "agent=director", "timeout=0") == (0, "timeout, no mail\n", "")

    def send_later() -> None:
        time.sleep(1)
        bus.call("send", agent="api", to="director", body="done")

    threading.Thread(target=send_later).start()
    assert run(capsys, *port, "waitmail", "agent=director", "timeout=30")[1] == "1 unread message(s) for director\n"
    assert bus.call("unread_count", agent="director") == 1  # waitmail does not consume the mail


def test_waitmail_errors(bus: RunningBus, dead_port: int, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "--port", str(bus.port), "waitmail", "agent=ghost", "timeout=5")[0] == 1
    assert run(capsys, "waitmail")[0] == 2
    assert run(capsys, "waitmail", "agent=a", "timeout=soon")[0] == 2
    code, out, err = run(capsys, "--port", str(dead_port), "waitmail", "agent=a", "timeout=0")
    assert (code, out) == (0, "timeout, no mail\n")
    assert "unreachable" in err


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--help")
    assert code == 0
    assert "usage: agentbus" in out
    assert run(capsys)[0] == 2
    assert run(capsys, "version") == (0, f"{__version__}\n", "")


def test_serve_refuses_a_busy_port_without_touching_the_pid_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "bus.db"
    (tmp_path / "server.pid").write_text("12345", encoding="ascii")  # the pid file of a server that runs already
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, _, err = run(capsys, "--port", str(port), "serve", "--db", str(db))
    assert code == 1
    assert "is another bus running there" in err
    assert (tmp_path / "server.pid").read_text(encoding="ascii") == "12345"
    assert not db.exists()

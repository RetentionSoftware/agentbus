"""SQLite-backed state of the bus. All business rules live here; server.py only exposes them. Stdlib only.

Every public method runs in one transaction under one lock, so concurrent callers (MCP tools run in worker threads)
always see and leave a consistent state, and a crash never leaves half of an operation behind.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import DEFAULT_PROTOCOL
from .schema import SchemaError, migrate

AGENT_STATES = ("idle", "working", "blocked", "review", "done")
TASK_STATES = ("todo", "in_progress", "blocked", "review", "done", "cancelled")
MSG_KINDS = ("note", "question", "answer", "contract_change", "review_request", "decision", "alert")
PRIORITIES = ("normal", "urgent")

# The agent that coordinates the team. By convention it is called "director": it may move tasks it does not own,
# and task transitions to review/blocked/done are reported to it automatically.
DIRECTOR = "director"
BROADCAST = "*"

AGENT_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
ONLINE_WINDOW_S = 15 * 60
HOOK_BODY_LIMIT = 1500
HOOK_MAX_MESSAGES = 8
HOOK_EVENTS = ("session_start", "prompt", "post_tool")
CLAIM_TTL_RANGE_H = (0.25, 72.0)

Row = sqlite3.Row
Json = dict[str, Any]


class BusError(Exception):
    """A rule violation. Its text is shown to the caller as is, so it says what to do instead."""


def norm_dir(path: str) -> str:
    """Normalises a working directory for comparison: forward slashes, no trailing slash, lower case."""
    return path.replace("\\", "/").rstrip("/").lower()


def norm_claim_path(path: str) -> str:
    """Normalises a repo-relative claim path or glob: forward slashes, no leading './' or slashes, lower case.

    Lower case makes claims conservative on case-sensitive file systems ('Src' and 'src' are treated as the same).
    """
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/").lower()


def claim_prefix(path: str) -> str:
    """Literal directory part of a claim path before the first glob character ('' = the whole repository)."""
    p = norm_claim_path(path)
    glob_at = next((i for i, ch in enumerate(p) if ch in "*?["), None)
    if glob_at is None:
        return p
    # Cut back to the enclosing directory: "src/core/mem*" and "src/core/**" both cover "src/core".
    head = p[:glob_at]
    return head[: head.rfind("/")] if "/" in head else ""


def claims_overlap(a: str, b: str) -> bool:
    """True if two claim paths may cover a common file (conservative: globs are widened to their directory)."""
    pa, pb = claim_prefix(a), claim_prefix(b)
    if not pa or not pb or pa == pb:
        return True
    return pa.startswith(pb + "/") or pb.startswith(pa + "/")


def _fmt_ts(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def _require(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise BusError(f"Invalid {what} '{value}'. Allowed: {', '.join(allowed)}")
    return value


class Store:
    """The bus state. Thread-safe: every public method is atomic."""

    def __init__(
        self, db_path: Path, *, protocol: str = DEFAULT_PROTOCOL, clock: Callable[[], float] = time.time
    ) -> None:
        """Opens (creating if needed) and migrates the database at db_path.

        protocol: repo-relative path of the agent protocol document, quoted in hook messages.
        clock: source of the current time in seconds (tests substitute it).
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.protocol = protocol
        self._clock = clock
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self.migration_notes = migrate(self._db, db_path)
        except sqlite3.DatabaseError as e:
            self._db.close()
            raise SchemaError(f"cannot use {db_path} as the bus database: {e}") from e
        except BaseException:
            self._db.close()
            raise
        self._lock = threading.RLock()
        self._depth = 0
        self._nudged_sessions: set[str] = set()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def _tx(self) -> Iterator[None]:
        """One transaction per public operation; nested calls join the outer one."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self._db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")
            finally:
                self._depth = 0

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[Row]:
        assert self._depth, "store query outside of a transaction"
        return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        assert self._depth, "store write outside of a transaction"
        return self._db.execute(sql, args)

    def _agent(self, name: str) -> Row:
        rows = self._q("SELECT * FROM agents WHERE name = ?", (name,))
        if not rows:
            raise BusError(f"Agent '{name}' is not registered. Call register() first.")
        return rows[0]

    def _event(self, type_: str, agent: str = "", target: str = "", text: str = "") -> None:
        """Appends to the human-readable activity journal shown by `agentbus watch`."""
        self._x(
            "INSERT INTO events(ts, type, agent, target, text) VALUES (?, ?, ?, ?, ?)",
            (self._clock(), type_, agent, target, text),
        )

    def _touch(self, name: str) -> None:
        self._x("UPDATE agents SET last_seen = ? WHERE name = ?", (self._clock(), name))

    def _agent_view(self, r: Row) -> Json:
        idle_s = int(self._clock() - r["last_seen"])
        return {
            "name": r["name"],
            "role": r["role"],
            "state": r["state"],
            "summary": r["summary"],
            "task_id": r["task_id"],
            "branch": r["branch"],
            "online": idle_s < ONLINE_WINDOW_S,
            "last_seen": _fmt_ts(r["last_seen"]),
            "seconds_since_seen": idle_s,
        }

    @staticmethod
    def _msg_view(r: Row) -> Json:
        return {
            "id": r["id"],
            "time": _fmt_ts(r["ts"]),
            "from": r["sender"],
            "to": "all" if r["recipient"] == BROADCAST else r["recipient"],
            "kind": r["kind"],
            "priority": r["priority"],
            "subject": r["subject"],
            "body": r["body"],
            "reply_to": r["reply_to"],
            "task_id": r["task_id"],
        }

    @staticmethod
    def _task_view(r: Row) -> Json:
        return {
            "id": r["id"],
            "title": r["title"],
            "state": r["state"],
            "owner": r["owner"],
            "spec_path": r["spec_path"],
            "depends_on": json.loads(r["depends_on"]),
            "wave": r["wave"],
            "updated": _fmt_ts(r["updated_at"]),
        }

    def _claim_view(self, c: Row) -> Json:
        return {
            "claim_id": c["id"],
            "agent": c["agent"],
            "path": c["path"],
            "reason": c["reason"],
            "shared": bool(c["shared"]),
            "expires": _fmt_ts(c["expires_at"]),
        }

    def _task_row(self, task_id: str) -> Row:
        rows = self._q("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise BusError(f"Task {task_id} does not exist.")
        return rows[0]

    def _unread_rows(self, agent: str) -> list[Row]:
        return self._q(
            """SELECT m.* FROM messages m
               WHERE (m.recipient = ? OR m.recipient = ?) AND m.sender != ?
                 AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id = m.id AND r.agent = ?)
               ORDER BY (m.priority = 'urgent') DESC, m.id""",
            (agent, BROADCAST, agent, agent),
        )

    def _mark_read(self, agent: str, ids: list[int], via: str) -> None:
        now = self._clock()
        self._db.executemany(
            "INSERT OR IGNORE INTO reads(message_id, agent, read_at, via) VALUES (?, ?, ?, ?)",
            [(i, agent, now, via) for i in ids],
        )

    def _active_claims(self) -> list[Row]:
        return self._q(
            "SELECT * FROM claims WHERE released_at IS NULL AND expires_at > ? ORDER BY id", (self._clock(),)
        )

    def _post(
        self,
        sender: str,
        recipient: str,
        kind: str,
        priority: str,
        subject: str,
        body: str,
        reply_to: int | None,
        task_id: str,
    ) -> int:
        cur = self._x(
            """INSERT INTO messages(ts, sender, recipient, kind, priority, subject, body, reply_to, task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._clock(), sender, recipient, kind, priority, subject, body, reply_to, task_id),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    # ------------------------------------------------------------------ agents
    def register(self, name: str, role: str, session_key: str = "", cwd: str = "", branch: str = "") -> Json:
        """Adds an agent or updates it on re-registration. A session key belongs to exactly one agent.

        On the first registration, team-wide messages sent before it are marked read ('preexisting'): an old
        broadcast (say, an urgent "stop all builds" that has long been lifted) is more harmful to a newcomer than
        useful. Their number is returned, and history() shows them.
        """
        name = name.strip()
        if not AGENT_NAME_RE.fullmatch(name) or name == "all":
            raise BusError("Agent name must be 1-64 characters of letters, digits, '.', '_' or '-', and not 'all'.")
        with self._tx():
            now = self._clock()
            existing = bool(self._q("SELECT 1 FROM agents WHERE name = ?", (name,)))
            cwd_norm = norm_dir(cwd) if cwd else ""
            if existing:
                self._x(
                    """UPDATE agents SET role = ?, session_key = COALESCE(NULLIF(?, ''), session_key),
                       cwd = COALESCE(NULLIF(?, ''), cwd), branch = COALESCE(NULLIF(?, ''), branch),
                       last_seen = ? WHERE name = ?""",
                    (role, session_key, cwd_norm, branch, now, name),
                )
                skipped = 0
            else:
                self._x(
                    """INSERT INTO agents(name, role, session_key, cwd, branch, registered_at, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name, role, session_key, cwd_norm, branch, now, now),
                )
                skipped = self._x(
                    """INSERT OR IGNORE INTO reads(message_id, agent, read_at, via)
                       SELECT id, ?, ?, 'preexisting' FROM messages WHERE recipient = ?""",
                    (name, now, BROADCAST),
                ).rowcount
            if session_key:
                self._x("UPDATE agents SET session_key = '' WHERE session_key = ? AND name != ?", (session_key, name))
            self._event("register", name, "", ("rejoined: " if existing else "joined: ") + role)
            tasks = self._q(
                "SELECT * FROM tasks WHERE owner = ? AND state NOT IN ('done', 'cancelled') ORDER BY id", (name,)
            )
            result: Json = {
                "registered": name,
                "rejoined": existing,
                "unread_messages": len(self._unread_rows(name)),
                "your_open_tasks": [self._task_view(t) for t in tasks],
                "agents_online": [a["name"] for a in self.agents() if a["online"] and a["name"] != name],
                "earlier_broadcasts_skipped": skipped,
            }
            if skipped:
                result["hint"] = (
                    f"{skipped} team-wide message(s) were sent before you joined; they are marked read. "
                    "Use history(kind='decision') to catch up on team decisions."
                )
            return result

    def set_status(self, agent: str, state: str, summary: str, task_id: str = "") -> Json:
        _require(state, AGENT_STATES, "state")
        with self._tx():
            prev = self._agent(agent)
            if (prev["state"], prev["summary"], prev["task_id"]) != (state, summary, task_id):
                self._event("status", agent, task_id, f"{state}: {summary}")
            self._x(
                "UPDATE agents SET state = ?, summary = ?, task_id = ?, last_seen = ? WHERE name = ?",
                (state, summary, task_id, self._clock(), agent),
            )
            return {"ok": True, "state": state, "summary": summary, "task_id": task_id}

    def agents(self) -> list[Json]:
        with self._tx():
            return [self._agent_view(r) for r in self._q("SELECT * FROM agents ORDER BY name")]

    # ------------------------------------------------------------------ messages
    def send(
        self,
        agent: str,
        to: str,
        body: str,
        subject: str = "",
        kind: str = "note",
        priority: str = "normal",
        reply_to: int = 0,
        task_id: str = "",
    ) -> Json:
        _require(kind, MSG_KINDS, "kind")
        _require(priority, PRIORITIES, "priority")
        if not body.strip():
            raise BusError("Message body is empty.")
        with self._tx():
            self._agent(agent)
            recipient = BROADCAST if to in ("all", BROADCAST) else to
            if recipient != BROADCAST:
                self._agent(recipient)
                if recipient == agent:
                    raise BusError("Cannot send a message to yourself.")
            if reply_to and not self._q("SELECT 1 FROM messages WHERE id = ?", (reply_to,)):
                raise BusError(f"reply_to: message {reply_to} does not exist.")
            mid = self._post(agent, recipient, kind, priority, subject, body, reply_to or None, task_id)
            tags = kind + (" URGENT" if priority == "urgent" else "") + (f" re#{reply_to}" if reply_to else "")
            target = "all" if recipient == BROADCAST else recipient
            self._event("message", agent, target, f"#{mid} [{tags}] " + (f"{subject}: " if subject else "") + body)
            self._touch(agent)
            return {"ok": True, "message_id": mid, "to": target}

    def inbox(self, agent: str, include_read: bool = False, limit: int = 20) -> Json:
        limit = max(1, min(limit, 100))
        with self._tx():
            self._agent(agent)
            if include_read:
                rows = self._q(
                    """SELECT * FROM messages WHERE (recipient = ? OR recipient = ?) AND sender != ?
                       ORDER BY id DESC LIMIT ?""",
                    (agent, BROADCAST, agent, limit),
                )
            else:
                rows = self._unread_rows(agent)[:limit]
            self._mark_read(agent, [r["id"] for r in rows], "inbox")
            if rows and not include_read:
                self._event("read", agent, "", "read " + ", ".join(f"#{r['id']}" for r in rows))
            self._touch(agent)
            return {"messages": [self._msg_view(r) for r in rows], "remaining_unread": len(self._unread_rows(agent))}

    def unread_count(self, agent: str) -> int:
        with self._tx():
            self._agent(agent)
            return len(self._unread_rows(agent))

    def history(self, kind: str = "", agent: str = "", task_id: str = "", limit: int = 30) -> Json:
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if kind:
            _require(kind, MSG_KINDS, "kind")
            sql += " AND kind = ?"
            args.append(kind)
        if agent:
            sql += " AND (sender = ? OR recipient = ?)"
            args += [agent, agent]
        if task_id:
            sql += " AND task_id = ?"
            args.append(task_id)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._tx():
            return {"messages": [self._msg_view(r) for r in self._q(sql, (*args, max(1, min(limit, 200))))]}

    # ------------------------------------------------------------------ tasks
    def task_create(
        self,
        agent: str,
        task_id: str,
        title: str,
        spec_path: str = "",
        depends_on: list[str] | None = None,
        owner: str = "",
        wave: str = "",
    ) -> Json:
        task_id = task_id.strip()
        if not task_id or not title.strip():
            raise BusError("task_id and title must not be empty.")
        deps = list(depends_on or [])
        if task_id in deps:
            raise BusError(f"Task {task_id} cannot depend on itself.")
        with self._tx():
            self._agent(agent)
            if self._q("SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
                raise BusError(f"Task {task_id} already exists.")
            if owner:
                self._agent(owner)
            now = self._clock()
            self._x(
                """INSERT INTO tasks(id, title, spec_path, owner, depends_on, wave, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, title, spec_path, owner, json.dumps(deps), wave, now, now),
            )
            self._x(
                "INSERT INTO task_log(task_id, ts, agent, state, note) VALUES (?, ?, ?, 'todo', 'created')",
                (task_id, now, agent),
            )
            self._event("task", agent, task_id, f"created '{title}'" + (f", owner {owner}" if owner else ""))
            if owner and owner != agent:
                self._post(
                    agent,
                    owner,
                    "note",
                    "normal",
                    f"Task {task_id} assigned to you",
                    f"{title}\nSpec: {spec_path}",
                    None,
                    task_id,
                )
            self._touch(agent)
            return {"ok": True, "task": self._task_view(self._task_row(task_id))}

    def task_update(self, agent: str, task_id: str, state: str, note: str = "", owner: str = "") -> Json:
        """Moves a task. Only its owner or the director may move an owned task (or anyone, when reassigning it
        with owner=...). Taking an unowned task to in_progress makes the caller its owner; in_progress requires
        all dependencies to be done. Moving to review/blocked/done notifies the director, if one is registered."""
        _require(state, TASK_STATES, "task state")
        with self._tx():
            self._agent(agent)
            task = self._task_row(task_id)
            if owner:
                self._agent(owner)
            if task["owner"] and task["owner"] != agent and agent != DIRECTOR and not owner:
                raise BusError(f"Task {task_id} is owned by {task['owner']}. Ask them or the {DIRECTOR}.")
            new_owner = owner or task["owner"] or (agent if state == "in_progress" else "")
            if state == "in_progress":
                deps: list[str] = json.loads(task["depends_on"])
                if deps:
                    done = {
                        r["id"]
                        for r in self._q(
                            f"SELECT id FROM tasks WHERE state = 'done' AND id IN ({','.join('?' * len(deps))})",
                            tuple(deps),
                        )
                    }
                    pending = [d for d in deps if d not in done]
                    if pending:
                        raise BusError(f"Task {task_id} depends on unfinished tasks: {', '.join(pending)}")
            now = self._clock()
            self._x(
                "UPDATE tasks SET state = ?, owner = ?, updated_at = ? WHERE id = ?", (state, new_owner, now, task_id)
            )
            self._x(
                "INSERT INTO task_log(task_id, ts, agent, state, note) VALUES (?, ?, ?, ?, ?)",
                (task_id, now, agent, state, note),
            )
            self._event("task", agent, task_id, f"{task['state']} -> {state}" + (f": {note}" if note else ""))
            if (
                state in ("review", "blocked", "done")
                and agent != DIRECTOR
                and self._q("SELECT 1 FROM agents WHERE name = ?", (DIRECTOR,))
            ):
                self._post(
                    agent,
                    DIRECTOR,
                    "review_request" if state == "review" else "note",
                    "urgent" if state == "blocked" else "normal",
                    f"Task {task_id} -> {state}",
                    note or f"{agent} moved {task_id} to {state}.",
                    None,
                    task_id,
                )
            self._touch(agent)
            return {"ok": True, "task": self._task_view(self._task_row(task_id))}

    def task_list(self, state: str = "", owner: str = "") -> Json:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if state:
            _require(state, TASK_STATES, "task state")
            sql += " AND state = ?"
            args.append(state)
        if owner:
            sql += " AND owner = ?"
            args.append(owner)
        with self._tx():
            return {"tasks": [self._task_view(r) for r in self._q(sql + " ORDER BY id", tuple(args))]}

    def task_get(self, task_id: str) -> Json:
        with self._tx():
            task = self._task_row(task_id)
            log = self._q("SELECT * FROM task_log WHERE task_id = ? ORDER BY id", (task_id,))
            return {
                "task": self._task_view(task),
                "log": [
                    {"time": _fmt_ts(r["ts"]), "agent": r["agent"], "state": r["state"], "note": r["note"]} for r in log
                ],
            }

    # ------------------------------------------------------------------ claims
    def claim(self, agent: str, path: str, reason: str, ttl_hours: float = 8, shared: bool = False) -> Json:
        """Claims a path or glob, or returns ok=False with the overlapping claims of others (nothing is created)."""
        npath = norm_claim_path(path)
        if not claim_prefix(npath):
            raise BusError(
                "Claiming the whole repository (or a glob at its root, which covers everything) is not "
                "allowed; claim a directory or file."
            )
        ttl_hours = max(CLAIM_TTL_RANGE_H[0], min(ttl_hours, CLAIM_TTL_RANGE_H[1]))
        with self._tx():
            self._agent(agent)
            active = self._active_claims()
            conflicts = [
                c
                for c in active
                if c["agent"] != agent and claims_overlap(c["path"], npath) and not (shared and c["shared"])
            ]
            if conflicts:
                self._event(
                    "conflict",
                    agent,
                    npath,
                    "claim refused, held by " + ", ".join(f"{c['agent']} ({c['path']})" for c in conflicts),
                )
                self._touch(agent)
                return {
                    "ok": False,
                    "conflicts": [
                        {
                            "claim_id": c["id"],
                            "agent": c["agent"],
                            "path": c["path"],
                            "reason": c["reason"],
                            "expires": _fmt_ts(c["expires_at"]),
                        }
                        for c in conflicts
                    ],
                    "hint": f"Coordinate with the owner via send(), or ask the {DIRECTOR}.",
                }
            now = self._clock()
            expires = now + ttl_hours * 3600
            mine = [c for c in active if c["agent"] == agent and c["path"] == npath]
            if mine:
                cid = int(mine[0]["id"])
                self._x(
                    "UPDATE claims SET reason = ?, shared = ?, expires_at = ? WHERE id = ?",
                    (reason, int(shared), expires, cid),
                )
            else:
                cur = self._x(
                    """INSERT INTO claims(agent, path, reason, shared, created_at, expires_at)
                                 VALUES (?, ?, ?, ?, ?, ?)""",
                    (agent, npath, reason, int(shared), now, expires),
                )
                assert cur.lastrowid is not None
                cid = cur.lastrowid
            self._event("claim", agent, npath, reason)
            self._touch(agent)
            return {"ok": True, "claim_id": cid, "path": npath, "expires": _fmt_ts(expires)}

    def release(self, agent: str, path: str = "", claim_id: int = 0) -> Json:
        """Releases one claim of the agent (by claim_id or path) or, with neither, all of them."""
        sql = "UPDATE claims SET released_at = ? WHERE agent = ? AND released_at IS NULL"
        with self._tx():
            self._agent(agent)
            args: list[Any] = [self._clock(), agent]
            if claim_id:
                sql += " AND id = ?"
                args.append(claim_id)
            elif path:
                sql += " AND path = ?"
                args.append(norm_claim_path(path))
            n = self._x(sql, tuple(args)).rowcount
            if n:
                what = f"claim {claim_id}" if claim_id else norm_claim_path(path) if path else "all"
                self._event("release", agent, what, f"{n} claim(s)")
            self._touch(agent)
            return {"ok": True, "released": n}

    def claims(self, agent: str = "") -> Json:
        with self._tx():
            return {"claims": [self._claim_view(c) for c in self._active_claims() if not agent or c["agent"] == agent]}

    # ------------------------------------------------------------------ overview and journal
    def board(self) -> Json:
        """Whole-team overview: agents, non-empty task states, active claims, the last decisions/contract changes."""
        with self._tx():
            by_state: dict[str, list[Json]] = {s: [] for s in TASK_STATES}
            for t in self.task_list()["tasks"]:
                by_state[t["state"]].append(t)
            recent = self._q(
                "SELECT * FROM messages WHERE kind IN ('decision', 'contract_change') ORDER BY id DESC LIMIT 10"
            )
            return {
                "agents": self.agents(),
                "tasks": {k: v for k, v in by_state.items() if v},
                "claims": self.claims()["claims"],
                "recent_decisions_and_contract_changes": [self._msg_view(r) for r in recent],
            }

    def events(self, after: int = 0, limit: int = 200) -> Json:
        """Activity journal entries with id > after, oldest first; after < 0 returns the last `limit` entries."""
        limit = max(1, min(limit, 1000))
        with self._tx():
            if after < 0:
                rows = self._q("SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,))
            else:
                rows = self._q("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
            return {
                "events": [
                    {
                        "id": r["id"],
                        "time": time.strftime("%H:%M:%S", time.localtime(r["ts"])),
                        "type": r["type"],
                        "agent": r["agent"],
                        "target": r["target"],
                        "text": r["text"],
                    }
                    for r in rows
                ]
            }

    def purge(self, agent: str = "", task_id: str = "") -> Json:
        """Erases every trace of an agent and/or a task: rows, messages, reads, claims, journal entries.
        Meant for cleaning up after test runs; not exposed over MCP."""
        if not agent and not task_id:
            raise BusError("purge needs agent and/or task_id.")
        n: dict[str, int] = {}
        with self._tx():
            if agent:
                self._x(
                    """DELETE FROM reads WHERE agent = ? OR message_id IN
                           (SELECT id FROM messages WHERE sender = ? OR recipient = ?)""",
                    (agent, agent, agent),
                )
                n["messages"] = self._x(
                    "DELETE FROM messages WHERE sender = ? OR recipient = ?", (agent, agent)
                ).rowcount
                n["claims"] = self._x("DELETE FROM claims WHERE agent = ?", (agent,)).rowcount
                n["events"] = self._x("DELETE FROM events WHERE agent = ? OR target = ?", (agent, agent)).rowcount
                n["agents"] = self._x("DELETE FROM agents WHERE name = ?", (agent,)).rowcount
            if task_id:
                self._x("DELETE FROM reads WHERE message_id IN (SELECT id FROM messages WHERE task_id = ?)", (task_id,))
                n["task_messages"] = self._x("DELETE FROM messages WHERE task_id = ?", (task_id,)).rowcount
                self._x("DELETE FROM task_log WHERE task_id = ?", (task_id,))
                n["task_events"] = self._x("DELETE FROM events WHERE target = ?", (task_id,)).rowcount
                n["tasks"] = self._x("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount
            return {"ok": True, "deleted": n}

    # ------------------------------------------------------------------ hooks
    def _resolve_session(self, session_id: str, cwd: str) -> str | None:
        if session_id:
            rows = self._q("SELECT name FROM agents WHERE session_key = ?", (session_id,))
            if rows:
                return str(rows[0]["name"])
        if cwd:
            # Directory fallback only for an agent that registered without a session_key: an agent already bound
            # to another session is never taken over (a new session in the same folder is a different agent).
            unbound = self._q("SELECT name FROM agents WHERE cwd = ? AND session_key = ''", (norm_dir(cwd),))
            if len(unbound) == 1:
                name = str(unbound[0]["name"])
                if session_id:
                    self._x("UPDATE agents SET session_key = ? WHERE name = ?", (session_id, name))
                return name
        return None

    def hook(self, event: str, session_id: str, cwd: str) -> str:
        """Called by the Claude Code hook. Returns text to add to the session's context ('' = nothing).

        event: 'session_start', 'prompt' or 'post_tool'. Refreshes the agent's last_seen and delivers its unread
        messages (marking them read); an unregistered session is told once how to register.
        """
        _require(event, HOOK_EVENTS, "hook event")
        with self._tx():
            agent = self._resolve_session(session_id, cwd)
            if agent is None:
                return self._unregistered_hint(event, session_id)
            self._touch(agent)
            rows = self._unread_rows(agent)
            head = ""
            if event == "session_start":
                self._event("session", agent, session_id[:8], "session (re)started")
                head = f"[agent-bus] You are registered as '{agent}' (session_key={session_id}).\n"
            if not rows:
                return head.rstrip()
            shown = rows[:HOOK_MAX_MESSAGES]
            self._mark_read(agent, [r["id"] for r in shown], "hook")
            self._event("delivered", agent, "", "auto-delivered " + ", ".join(f"#{r['id']}" for r in shown))
            parts = [f"{head}[agent-bus] {len(rows)} new message(s) for '{agent}':"]
            for r in shown:
                body = r["body"]
                if len(body) > HOOK_BODY_LIMIT:
                    body = body[:HOOK_BODY_LIMIT] + " …(truncated, see inbox(include_read=True))"
                tag = "URGENT " if r["priority"] == "urgent" else ""
                to = "all" if r["recipient"] == BROADCAST else "you"
                subj = f" | {r['subject']}" if r["subject"] else ""
                task = f" | task {r['task_id']}" if r["task_id"] else ""
                parts.append(f"--- #{r['id']} {tag}{r['kind']} from {r['sender']} to {to}{subj}{task}\n{body}")
            if len(rows) > len(shown):
                parts.append(f"... and {len(rows) - len(shown)} more: call inbox().")
            parts.append(
                "[agent-bus] Reply with send(reply_to=<id>) if a response is expected. "
                "Urgent messages take priority over your current step."
            )
            return "\n".join(parts)

    def _unregistered_hint(self, event: str, session_id: str) -> str:
        if event == "session_start":
            self._event("session", "", session_id[:8], "new unregistered session started")
            return (
                "[agent-bus] This project coordinates agents through the local agent bus (MCP server "
                f"'agentbus'). You are NOT registered yet. Read {self.protocol}, then call "
                f'register(name=..., role=..., session_key="{session_id}", cwd=<your working directory>).'
            )
        if session_id and session_id not in self._nudged_sessions:
            self._nudged_sessions.add(session_id)
            return (
                "[agent-bus] You are not registered on the agent bus. If you are an agent of this team, read "
                f'{self.protocol} and call register(..., session_key="{session_id}").'
            )
        return ""

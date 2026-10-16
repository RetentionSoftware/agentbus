from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from agentbus.store import (
    HOOK_BODY_LIMIT,
    HOOK_MAX_MESSAGES,
    ONLINE_WINDOW_S,
    BusError,
    Store,
    claim_prefix,
    claims_overlap,
    norm_claim_path,
    norm_dir,
)

from .conftest import PROTOCOL, FakeClock


def reg(store: Store, *names: str) -> None:
    for name in names:
        store.register(name, f"role of {name}")


def unread(store: Store, agent: str) -> int:
    return store.unread_count(agent)


# ---------------------------------------------------------------------- registration
class TestRegister:
    def test_new_agent(self, store: Store) -> None:
        r = store.register("api", "builds the API", cwd="C:\\Work\\Repo\\", branch="feat/api")
        assert r["registered"] == "api"
        assert r["rejoined"] is False
        assert r["unread_messages"] == 0
        assert r["your_open_tasks"] == []
        assert r["earlier_broadcasts_skipped"] == 0
        assert "hint" not in r
        [a] = store.agents()
        assert a["name"] == "api"
        assert a["role"] == "builds the API"
        assert a["branch"] == "feat/api"
        assert a["state"] == "idle"
        assert a["online"] is True

    def test_rejoin_keeps_unspecified_fields(self, store: Store) -> None:
        store.register("api", "r1", session_key="s1", cwd="/w", branch="b1")
        r = store.register("api", "r2")
        assert r["rejoined"] is True
        assert store.hook("prompt", "s1", "") == ""  # still bound to s1
        assert store.agents()[0]["branch"] == "b1"
        assert store.agents()[0]["role"] == "r2"

    @pytest.mark.parametrize("name", ["", "  ", "two words", "all", "*", "a" * 65, "имя", "a/b", "a:b"])
    def test_invalid_names(self, store: Store, name: str) -> None:
        with pytest.raises(BusError, match="Agent name"):
            store.register(name, "x")

    @pytest.mark.parametrize("name", ["a", "core-jobs", "agent_2", "v1.2", "a" * 64, " padded "])
    def test_valid_names(self, store: Store, name: str) -> None:
        assert store.register(name, "x")["registered"] == name.strip()

    def test_session_key_belongs_to_one_agent(self, store: Store) -> None:
        store.register("old", "x", session_key="s1")
        store.register("new", "x", session_key="s1")
        assert store.hook("post_tool", "s1", "") == ""
        store.send("old", "new", "for new")
        assert "for new" in store.hook("post_tool", "s1", "")

    def test_open_tasks_and_online_peers_returned(self, store: Store, clock: FakeClock) -> None:
        reg(store, "director", "peer")
        store.register("api", "x")
        store.task_create("director", "T-1", "first", owner="api")
        store.task_create("director", "T-2", "second", owner="api")
        store.task_update("api", "T-2", "in_progress")
        store.task_update("api", "T-2", "done")
        clock.advance(ONLINE_WINDOW_S + 1)
        store.register("peer", "x")
        r = store.register("api", "x")
        assert [t["id"] for t in r["your_open_tasks"]] == ["T-1"]
        assert r["agents_online"] == ["peer"]

    def test_set_status(self, store: Store) -> None:
        reg(store, "api")
        assert store.set_status("api", "working", "on it", "T-1")["ok"]
        a = store.agents()[0]
        assert (a["state"], a["summary"], a["task_id"]) == ("working", "on it", "T-1")
        with pytest.raises(BusError, match="Invalid state"):
            store.set_status("api", "sleeping", "x")
        with pytest.raises(BusError, match="not registered"):
            store.set_status("ghost", "idle", "x")

    def test_online_window(self, store: Store, clock: FakeClock) -> None:
        reg(store, "api")
        clock.advance(ONLINE_WINDOW_S - 1)
        assert store.agents()[0]["online"] is True
        clock.advance(2)
        assert store.agents()[0]["online"] is False
        store.hook("post_tool", "", "")  # an unknown session does not touch anyone
        assert store.agents()[0]["online"] is False


class TestEarlierBroadcasts:
    def test_first_registration_skips_earlier_broadcasts(self, store: Store) -> None:
        reg(store, "director")
        store.send("director", "all", "URGENT: stop all builds", priority="urgent", kind="alert")
        store.send("director", "all", "builds allowed again", kind="decision")
        r = store.register("late", "x")
        assert r["earlier_broadcasts_skipped"] == 2
        assert "history(kind='decision')" in r["hint"]
        assert r["unread_messages"] == 0
        assert store.history(kind="decision")["messages"][0]["body"] == "builds allowed again"
        store.send("director", "all", "new broadcast")
        assert unread(store, "late") == 1

    def test_rejoin_does_not_skip_anything(self, store: Store) -> None:
        reg(store, "director", "api")
        store.send("director", "all", "while you were away")
        r = store.register("api", "x")
        assert r["earlier_broadcasts_skipped"] == 0
        assert r["unread_messages"] == 1

    def test_inbox_history_still_shows_skipped_broadcasts(self, store: Store) -> None:
        reg(store, "director")
        store.send("director", "all", "old news")
        reg(store, "late")
        assert [m["body"] for m in store.inbox("late", include_read=True)["messages"]] == ["old news"]


# ---------------------------------------------------------------------- messages
class TestMessages:
    def test_send_and_read(self, store: Store) -> None:
        reg(store, "a", "b")
        r = store.send("a", "b", "hello", subject="greeting", kind="question", task_id="T-1")
        assert r == {"ok": True, "message_id": r["message_id"], "to": "b"}
        assert unread(store, "b") == 1
        inbox = store.inbox("b")
        [m] = inbox["messages"]
        assert (m["from"], m["to"], m["kind"], m["subject"], m["body"], m["task_id"]) == (
            "a",
            "b",
            "question",
            "greeting",
            "hello",
            "T-1",
        )
        assert inbox["remaining_unread"] == 0
        assert store.inbox("b")["messages"] == []

    def test_urgent_first_then_oldest(self, store: Store) -> None:
        reg(store, "a", "b")
        ids = [
            store.send("a", "b", body, priority=p)["message_id"]
            for body, p in [("n1", "normal"), ("u1", "urgent"), ("n2", "normal"), ("u2", "urgent")]
        ]
        assert [m["id"] for m in store.inbox("b")["messages"]] == [ids[1], ids[3], ids[0], ids[2]]

    def test_limit_leaves_the_rest_unread(self, store: Store) -> None:
        reg(store, "a", "b")
        for i in range(5):
            store.send("a", "b", f"m{i}")
        r = store.inbox("b", limit=2)
        assert len(r["messages"]) == 2
        assert r["remaining_unread"] == 3

    def test_broadcast_reaches_everyone_but_the_sender(self, store: Store) -> None:
        reg(store, "a", "b", "c")
        assert store.send("a", "*", "hi all")["to"] == "all"
        assert (unread(store, "a"), unread(store, "b"), unread(store, "c")) == (0, 1, 1)
        assert store.inbox("b")["messages"][0]["to"] == "all"

    def test_include_read_returns_recent_history_newest_first(self, store: Store) -> None:
        reg(store, "a", "b")
        store.send("a", "b", "one")
        store.inbox("b")
        store.send("a", "b", "two")
        r = store.inbox("b", include_read=True)
        assert [m["body"] for m in r["messages"]] == ["two", "one"]
        assert r["remaining_unread"] == 0

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"to": "b", "body": "  "}, "empty"),
            ({"to": "ghost", "body": "x"}, "not registered"),
            ({"to": "a", "body": "x"}, "yourself"),
            ({"to": "b", "body": "x", "kind": "gossip"}, "Invalid kind"),
            ({"to": "b", "body": "x", "priority": "high"}, "Invalid priority"),
            ({"to": "b", "body": "x", "reply_to": 999}, "does not exist"),
        ],
    )
    def test_send_validation(self, store: Store, kwargs: dict[str, Any], error: str) -> None:
        reg(store, "a", "b")
        with pytest.raises(BusError, match=error):
            store.send("a", **kwargs)

    def test_unregistered_sender(self, store: Store) -> None:
        reg(store, "b")
        with pytest.raises(BusError, match="not registered"):
            store.send("ghost", "b", "x")

    def test_reply_to(self, store: Store) -> None:
        reg(store, "a", "b")
        q = store.send("a", "b", "question?", kind="question")["message_id"]
        store.send("b", "a", "answer", kind="answer", reply_to=q)
        assert store.inbox("a")["messages"][0]["reply_to"] == q

    def test_history_filters(self, store: Store) -> None:
        reg(store, "a", "b", "c")
        store.send("a", "b", "n1")
        store.send("a", "all", "d1", kind="decision", task_id="T-1")
        store.send("b", "c", "n2", task_id="T-1")

        def bodies(**kw: Any) -> list[str]:
            return [m["body"] for m in store.history(**kw)["messages"]]

        assert bodies() == ["n2", "d1", "n1"]
        assert bodies(kind="decision") == ["d1"]
        assert bodies(task_id="T-1") == ["n2", "d1"]
        assert bodies(agent="c") == ["n2"]
        assert bodies(limit=1) == ["n2"]
        with pytest.raises(BusError, match="Invalid kind"):
            store.history(kind="gossip")

    def test_unread_count_of_unknown_agent(self, store: Store) -> None:
        with pytest.raises(BusError, match="not registered"):
            store.unread_count("ghost")


# ---------------------------------------------------------------------- tasks
class TestTasks:
    def test_create_and_get(self, store: Store) -> None:
        reg(store, "director", "api")
        r = store.task_create(
            "director", "T-1", "API", spec_path="docs/T-1.md", depends_on=["T-0"], owner="api", wave="1"
        )
        assert r["task"] == {
            "id": "T-1",
            "title": "API",
            "state": "todo",
            "owner": "api",
            "spec_path": "docs/T-1.md",
            "depends_on": ["T-0"],
            "wave": "1",
            "updated": r["task"]["updated"],
        }
        [note] = store.inbox("api")["messages"]
        assert note["subject"] == "Task T-1 assigned to you"
        assert note["task_id"] == "T-1"
        assert store.task_get("T-1")["log"][0]["note"] == "created"

    @pytest.mark.parametrize(
        ("kwargs", "error"),
        [
            ({"task_id": " ", "title": "x"}, "must not be empty"),
            ({"task_id": "T-1", "title": ""}, "must not be empty"),
            ({"task_id": "T-1", "title": "x", "depends_on": ["T-1"]}, "itself"),
            ({"task_id": "T-1", "title": "x", "owner": "ghost"}, "not registered"),
        ],
    )
    def test_create_validation(self, store: Store, kwargs: dict[str, Any], error: str) -> None:
        reg(store, "director")
        with pytest.raises(BusError, match=error):
            store.task_create("director", **kwargs)
        assert store.task_list()["tasks"] == []

    def test_duplicate(self, store: Store) -> None:
        reg(store, "director")
        store.task_create("director", "T-1", "x")
        with pytest.raises(BusError, match="already exists"):
            store.task_create("director", "T-1", "y")

    def test_taking_an_unowned_task_makes_you_the_owner(self, store: Store) -> None:
        reg(store, "director", "api")
        store.task_create("director", "T-1", "x")
        assert store.task_update("api", "T-1", "in_progress")["task"]["owner"] == "api"

    def test_only_owner_or_director_moves_an_owned_task(self, store: Store) -> None:
        reg(store, "director", "api", "web")
        store.task_create("director", "T-1", "x", owner="api")
        with pytest.raises(BusError, match="owned by api"):
            store.task_update("web", "T-1", "in_progress")
        assert store.task_update("director", "T-1", "blocked")["task"]["state"] == "blocked"
        assert store.task_update("web", "T-1", "todo", owner="web")["task"]["owner"] == "web"

    def test_dependencies_gate_in_progress(self, store: Store) -> None:
        reg(store, "director", "api")
        store.task_create("director", "T-0", "base")
        store.task_create("director", "T-1", "x", depends_on=["T-0", "T-9"])
        with pytest.raises(BusError, match="unfinished tasks: T-0, T-9"):
            store.task_update("api", "T-1", "in_progress")
        store.task_update("director", "T-0", "done")
        store.task_create("director", "T-9", "late")
        store.task_update("director", "T-9", "done")
        assert store.task_update("api", "T-1", "in_progress")["task"]["state"] == "in_progress"
        # Other states are not gated.
        store.task_create("director", "T-2", "y", depends_on=["T-404"])
        assert store.task_update("api", "T-2", "blocked")["task"]["state"] == "blocked"

    @pytest.mark.parametrize(
        ("state", "kind", "priority"),
        [("review", "review_request", "normal"), ("blocked", "note", "urgent"), ("done", "note", "normal")],
    )
    def test_director_is_notified(self, store: Store, state: str, kind: str, priority: str) -> None:
        reg(store, "director", "api")
        store.task_create("director", "T-1", "x")
        store.task_update("api", "T-1", "in_progress")
        store.task_update("api", "T-1", state, note="details")
        [m] = store.inbox("director")["messages"]
        assert (m["kind"], m["priority"], m["subject"], m["body"], m["task_id"]) == (
            kind,
            priority,
            f"Task T-1 -> {state}",
            "details",
            "T-1",
        )

    def test_no_notification_without_director_or_from_director(self, store: Store) -> None:
        reg(store, "api")
        store.task_create("api", "T-1", "x")
        store.task_update("api", "T-1", "review")  # no director registered: nothing to notify, no error
        reg(store, "director")
        store.task_update("director", "T-1", "done")
        assert unread(store, "director") == 0

    def test_list_filters_and_log(self, store: Store) -> None:
        reg(store, "director", "api")
        store.task_create("director", "T-2", "b", owner="api")
        store.task_create("director", "T-1", "a")
        store.task_update("api", "T-2", "in_progress", note="go")
        assert [t["id"] for t in store.task_list()["tasks"]] == ["T-1", "T-2"]
        assert [t["id"] for t in store.task_list(state="in_progress")["tasks"]] == ["T-2"]
        assert [t["id"] for t in store.task_list(owner="api")["tasks"]] == ["T-2"]
        log = store.task_get("T-2")["log"]
        assert [(e["agent"], e["state"], e["note"]) for e in log] == [
            ("director", "todo", "created"),
            ("api", "in_progress", "go"),
        ]
        with pytest.raises(BusError, match="Invalid task state"):
            store.task_list(state="started")
        with pytest.raises(BusError, match="does not exist"):
            store.task_get("T-404")
        with pytest.raises(BusError, match="does not exist"):
            store.task_update("api", "T-404", "done")


# ---------------------------------------------------------------------- claims
@pytest.mark.parametrize(
    ("raw", "norm", "prefix"),
    [
        ("src/core/", "src/core", "src/core"),
        ("./src\\Core\\x.h", "src/core/x.h", "src/core/x.h"),
        ("/src/core/**", "src/core/**", "src/core"),
        ("src/core/mem*", "src/core/mem*", "src/core"),
        ("src/*.py", "src/*.py", "src"),
        ("*.md", "*.md", ""),
        ("**", "**", ""),
        ("./", "", ""),
    ],
)
def test_claim_path_normalisation(raw: str, norm: str, prefix: str) -> None:
    assert norm_claim_path(raw) == norm
    assert claim_prefix(raw) == prefix


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ("src/core", "src/core", True),
        ("src/core", "src/core/x.h", True),
        ("src/core/**", "src/core/sub/y.h", True),
        ("src/core/mem*", "src/core/other.h", True),  # conservative: a glob covers its directory
        ("src/core", "src/corelib", False),
        ("src/a", "src/b", False),
        ("docs/a.md", "src", False),
        ("*.md", "src/x", True),
    ],
)
def test_claims_overlap(a: str, b: str, overlap: bool) -> None:
    assert claims_overlap(a, b) is overlap
    assert claims_overlap(b, a) is overlap


def test_norm_dir() -> None:
    assert norm_dir("C:\\Work\\Repo\\") == "c:/work/repo"


class TestClaims:
    def test_conflict_creates_nothing(self, store: Store) -> None:
        reg(store, "a", "b")
        assert store.claim("a", "src/core/**", "refactor")["ok"]
        r = store.claim("b", "src/core/x.h", "fix")
        assert r["ok"] is False
        assert [(c["agent"], c["path"], c["reason"]) for c in r["conflicts"]] == [("a", "src/core/**", "refactor")]
        assert [c["agent"] for c in store.claims()["claims"]] == ["a"]

    def test_own_claims_do_not_conflict(self, store: Store) -> None:
        reg(store, "a")
        assert store.claim("a", "src", "x")["ok"]
        assert store.claim("a", "src/x", "y")["ok"]

    def test_shared_claims(self, store: Store) -> None:
        reg(store, "a", "b", "c")
        assert store.claim("a", "docs", "x", shared=True)["ok"]
        assert store.claim("b", "docs/x.md", "y", shared=True)["ok"]
        assert store.claim("c", "docs/y.md", "z")["ok"] is False

    def test_reclaim_extends(self, store: Store, clock: FakeClock) -> None:
        reg(store, "a")
        first = store.claim("a", "src", "x", ttl_hours=1)
        clock.advance(1800)
        second = store.claim("a", "./src/", "y", ttl_hours=1)
        assert second["claim_id"] == first["claim_id"]
        assert second["expires"] > first["expires"]
        assert store.claims()["claims"][0]["reason"] == "y"

    def test_expiry(self, store: Store, clock: FakeClock) -> None:
        reg(store, "a", "b")
        store.claim("a", "src", "x", ttl_hours=1)
        clock.advance(3601)
        assert store.claims()["claims"] == []
        assert store.claim("b", "src", "y")["ok"]

    def test_ttl_is_clamped(self, store: Store, clock: FakeClock) -> None:
        reg(store, "a", "b")
        store.claim("a", "x", "r", ttl_hours=0)
        clock.advance(0.25 * 3600 - 1)
        assert store.claims()["claims"]
        store.claim("b", "y", "r", ttl_hours=10_000)
        clock.advance(72 * 3600)
        assert store.claims()["claims"] == []

    @pytest.mark.parametrize("path", ["", "./", "/", "**", "*.py", "?"])
    def test_whole_repository_refused(self, store: Store, path: str) -> None:
        reg(store, "a")
        with pytest.raises(BusError, match="whole repository"):
            store.claim("a", path, "x")

    def test_release(self, store: Store) -> None:
        reg(store, "a", "b")
        c1 = store.claim("a", "one", "x")["claim_id"]
        store.claim("a", "two", "x")
        store.claim("a", "three", "x")
        store.claim("b", "four", "x")
        assert store.release("b", claim_id=c1)["released"] == 0  # not b's claim
        assert store.release("a", claim_id=c1)["released"] == 1
        assert store.release("a", path="./TWO")["released"] == 1
        assert store.release("a")["released"] == 1
        assert [c["path"] for c in store.claims()["claims"]] == ["four"]
        assert store.claims(agent="a")["claims"] == []


# ---------------------------------------------------------------------- journal, purge, board
def test_events_journal(store: Store) -> None:
    reg(store, "a", "b")
    store.send("a", "b", "hello", subject="s", priority="urgent")
    store.claim("a", "src", "why")
    store.claim("b", "src/x", "why not")
    types = [e["type"] for e in store.events()["events"]]
    assert types == ["register", "register", "message", "claim", "conflict"]
    msg = store.events()["events"][2]
    assert (msg["agent"], msg["target"]) == ("a", "b")
    assert "URGENT" in msg["text"]
    assert "s: hello" in msg["text"]
    all_ids = [e["id"] for e in store.events()["events"]]
    assert [e["id"] for e in store.events(after=all_ids[2])["events"]] == all_ids[3:]
    assert [e["id"] for e in store.events(after=-1, limit=2)["events"]] == all_ids[-2:]
    assert [e["id"] for e in store.events(limit=2)["events"]] == all_ids[:2]


def test_status_event_only_on_change(store: Store) -> None:
    reg(store, "a")
    store.set_status("a", "working", "x")
    store.set_status("a", "working", "x")
    assert [e["type"] for e in store.events()["events"]].count("status") == 1


def test_purge(store: Store) -> None:
    reg(store, "director", "temp", "keep")
    store.send("temp", "keep", "from temp")
    store.send("keep", "temp", "to temp")
    store.send("director", "all", "broadcast stays")
    store.inbox("keep")
    store.claim("temp", "src", "x")
    store.task_create("director", "T-9", "test task")
    store.send("director", "keep", "about T-9", task_id="T-9")
    r = store.purge(agent="temp", task_id="T-9")
    assert r["deleted"]["agents"] == 1
    assert r["deleted"]["messages"] == 2
    assert r["deleted"]["tasks"] == 1
    assert [a["name"] for a in store.agents()] == ["director", "keep"]
    assert [m["body"] for m in store.history()["messages"]] == ["broadcast stays"]
    assert store.claims()["claims"] == []
    assert store.task_list()["tasks"] == []
    assert not any("temp" in (e["agent"], e["target"]) or e["target"] == "T-9" for e in store.events()["events"])
    with pytest.raises(BusError, match="needs agent"):
        store.purge()


def test_board(store: Store) -> None:
    reg(store, "director", "api")
    store.task_create("director", "T-1", "x")
    store.claim("api", "src", "x")
    store.send("director", "all", "use tabs", kind="decision")
    store.send("director", "api", "just a note")
    b = store.board()
    assert [a["name"] for a in b["agents"]] == ["api", "director"]
    assert list(b["tasks"]) == ["todo"]
    assert [c["path"] for c in b["claims"]] == ["src"]
    assert [m["body"] for m in b["recent_decisions_and_contract_changes"]] == ["use tabs"]


# ---------------------------------------------------------------------- hook
class TestHook:
    def test_unregistered_session_start_explains_how_to_register(self, store: Store) -> None:
        text = store.hook("session_start", "sess-1", "/repo")
        assert "NOT registered" in text
        assert PROTOCOL in text
        assert 'session_key="sess-1"' in text

    def test_unregistered_session_is_nudged_once(self, store: Store) -> None:
        first = store.hook("post_tool", "sess-1", "/repo")
        assert "not registered" in first
        assert 'session_key="sess-1"' in first
        assert store.hook("prompt", "sess-1", "/repo") == ""
        assert store.hook("post_tool", "", "/repo") == ""

    def test_delivery_marks_read(self, store: Store) -> None:
        store.register("api", "x", session_key="s1")
        reg(store, "director")
        store.send("director", "api", "please review", subject="review", task_id="T-1", priority="urgent")
        text = store.hook("post_tool", "s1", "")
        assert "1 new message(s) for 'api'" in text
        assert "URGENT note from director to you | review | task T-1" in text
        assert "please review" in text
        assert unread(store, "api") == 0
        assert store.hook("post_tool", "s1", "") == ""

    def test_session_start_of_registered_agent(self, store: Store) -> None:
        store.register("api", "x", session_key="s1")
        assert store.hook("session_start", "s1", "") == "[agent-bus] You are registered as 'api' (session_key=s1)."

    def test_long_bodies_are_truncated_and_many_messages_capped(self, store: Store) -> None:
        store.register("api", "x", session_key="s1")
        reg(store, "director")
        store.send("director", "api", "x" * (HOOK_BODY_LIMIT + 10))
        for i in range(HOOK_MAX_MESSAGES + 2):
            store.send("director", "api", f"m{i}")
        text = store.hook("post_tool", "s1", "")
        assert "…(truncated" in text
        assert "x" * (HOOK_BODY_LIMIT + 1) not in text
        assert f"... and {3} more: call inbox()." in text
        assert unread(store, "api") == 3

    def test_unknown_event(self, store: Store) -> None:
        with pytest.raises(BusError, match="Invalid hook event"):
            store.hook("stop", "s1", "")

    def test_directory_fallback_binds_an_unbound_agent(self, store: Store) -> None:
        store.register("api", "x", cwd="C:\\Work\\Repo")
        store.register("director", "x", session_key="d")
        store.send("director", "api", "hi")
        assert "hi" in store.hook("post_tool", "new-session", "c:/work/repo/")
        store.send("director", "api", "again")
        assert "again" in store.hook("post_tool", "new-session", "")  # now bound by session key

    def test_new_session_in_the_same_folder_does_not_take_over_a_bound_agent(self, store: Store) -> None:
        # Regression: a second Claude session started in an agent's folder used to receive that agent's mail.
        store.register("api", "x", session_key="s1", cwd="/repo")
        store.register("director", "x", session_key="d")
        store.send("director", "api", "private")
        text = store.hook("session_start", "s2", "/repo")
        assert "NOT registered" in text
        assert "private" not in text
        assert unread(store, "api") == 1

    def test_ambiguous_folder_binds_nobody(self, store: Store) -> None:
        store.register("one", "x", cwd="/repo")
        store.register("two", "x", cwd="/repo")
        assert "NOT registered" in store.hook("session_start", "s1", "/repo")


# ---------------------------------------------------------------------- transactions
def test_concurrent_overlapping_claims_admit_exactly_one(tmp_path: Path) -> None:
    # Regression: claim() used to check for conflicts and insert in separate lock scopes, so simultaneous claims of
    # the same path could all succeed (about one round in three on the old code). 20 rounds make a miss unlikely.
    store = Store(tmp_path / "bus.db")
    names = [f"agent{i}" for i in range(12)]
    for n in names:
        store.register(n, "x")
    for round_ in range(20):
        barrier = threading.Barrier(len(names))
        results: list[bool] = []

        def worker(name: str, path: str, barrier: threading.Barrier = barrier, results: list[bool] = results) -> None:
            barrier.wait()
            results.append(store.claim(name, path, "race")["ok"])

        threads = [threading.Thread(target=worker, args=(n, f"src/round{round_}")) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == 1, f"round {round_}: {sum(results)} winners"
    assert len(store.claims()["claims"]) == 20
    store.close()


def test_failed_operation_leaves_no_trace(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    reg(store, "director", "api")

    def broken_post(*args: Any) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "_post", broken_post)
    with pytest.raises(RuntimeError):
        store.task_create("director", "T-1", "x", owner="api")  # fails after inserting the task and its log
    monkeypatch.undo()
    assert store.task_list()["tasks"] == []
    assert store.task_create("director", "T-1", "x", owner="api")["ok"]
    assert len(store.task_get("T-1")["log"]) == 1


def test_data_survives_reopen(tmp_path: Path) -> None:
    s1 = Store(tmp_path / "bus.db")
    s1.register("a", "x")
    s1.register("b", "x")
    s1.send("a", "b", "persisted")
    s1.close()
    s2 = Store(tmp_path / "bus.db")
    assert s2.inbox("b")["messages"][0]["body"] == "persisted"
    assert s2.migration_notes == []
    s2.close()
    with sqlite3.connect(tmp_path / "bus.db") as db:
        assert db.execute("SELECT via FROM reads").fetchone()[0] == "inbox"

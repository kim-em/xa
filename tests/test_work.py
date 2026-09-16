from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

from xa import cli
from xa.config import Config
from xa.finish_work import finish
from xa.model import Item, Observation
from xa.store import Store
from xa.work import attach, reconcile


NOW = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)


def test_work_is_keyed_to_the_exact_problem_state(tmp_path):
    store = Store(tmp_path / "xa.db")
    store.start_work("m/k", "s1", "m", "fix", "claude", started_at=NOW)
    old = Item(
        monitor="m", obs=Observation(key="k", title="t", state_key="s1", actions=["fix"])
    )
    changed = Item(
        monitor="m", obs=Observation(key="k", title="t", state_key="s2", actions=["fix"])
    )

    attach([old, changed], store)
    assert old.work["fix"]["status"] == "starting"
    assert changed.work["fix"]["status"] == "starting"
    assert changed.work["fix"]["state_changed"] is True


def test_lifecycle_marker_moves_active_then_finished(tmp_path):
    store = Store(tmp_path / "xa.db")
    marker = tmp_path / "work.state"
    store.start_work("m/k", "s1", "m", "fix", "claude", str(marker), started_at=NOW)

    marker.write_text("active\n")
    assert reconcile(store) == []
    assert store.work_for("m/k", "s1", "fix").status == "active"

    marker.write_text("finished\n")
    finished = reconcile(store)
    assert [work.monitor for work in finished] == ["m"]
    assert store.work_for("m/k", "s1", "fix").status == "finished"


def test_an_adopted_tmux_session_finishes_when_it_disappears(tmp_path, monkeypatch):
    store = Store(tmp_path / "xa.db")
    store.start_work("m/k", "s1", "m", "fix", "claude",
                     session_name="session-1", started_at=NOW)
    alive = True
    monkeypatch.setattr("xa.work.adopted_session_alive", lambda name: alive)

    reconcile(store)
    assert store.work_for("m/k", "s1", "fix").status == "active"
    alive = False
    assert [work.uid for work in reconcile(store)] == ["m/k"]


def test_two_actions_on_one_item_have_independent_sessions(tmp_path):
    store = Store(tmp_path / "xa.db")
    store.start_work("m/k", "s1", "m", "conflicts", "claude", started_at=NOW)
    store.start_work("m/k", "s1", "m", "red", "claude", started_at=NOW)
    item = Item(
        monitor="m",
        obs=Observation(
            key="k", title="t", state_key="s1", actions=["conflicts", "red"]
        ),
    )

    attach([item], store)

    assert set(item.work) == {"conflicts", "red"}


def test_attach_works_with_store_from_before_bulk_state_lookup(tmp_path):
    """A long-lived `xa open` may finish after the engine has been upgraded."""
    store = Store(tmp_path / "xa.db")
    store.start_work("m/k", "s1", "m", "fix", "claude", started_at=NOW)

    class OlderStore:
        def execute(self, sql, params=()):
            return store.execute(sql, params)

    item = Item(
        monitor="m",
        obs=Observation(key="k", title="t", state_key="s1", actions=["fix"]),
    )

    attach([item], OlderStore())  # type: ignore[arg-type]

    assert item.work["fix"]["status"] == "starting"


def test_post_session_finalization_uses_a_fresh_interpreter(monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="monitor: refreshed\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.sys, "executable", "/current/python")

    cli._finalize_work("m/k", "state", "fix", "monitor", "pid:123", "finished")

    assert seen == [
        ([
            "/current/python", "-m", "xa.finish_work",
            "m/k", "state", "fix", "monitor", "pid:123", "finished",
        ], {
            "capture_output": True,
            "text": True,
        })
    ]


def test_finalizer_only_finishes_the_process_it_launched(tmp_path, monkeypatch):
    monkeypatch.setenv("XA_CACHE", str(tmp_path / "cache"))
    store = Store(tmp_path / "xa.db")
    store.start_work(
        "prs/mine", "state", "prs", "conflicts", "claude",
        session_name="pid:100", started_at=NOW,
    )
    store.start_work(
        "prs/mine", "state", "prs", "changes", "claude",
        session_name="pid:200", started_at=NOW,
    )
    store.set_work_status("prs/mine", "state", "conflicts", "active")
    store.set_work_status("prs/mine", "state", "changes", "active")
    cfg = Config(root=tmp_path)

    assert not finish(
        "prs/mine", "state", "conflicts", "prs", "pid:old", "failed",
        cfg=cfg, store=store,
    )
    assert finish(
        "prs/mine", "state", "conflicts", "prs", "pid:100", "failed",
        cfg=cfg, store=store,
    )

    assert store.work_for("prs/mine", "state", "conflicts").status == "failed"
    assert store.work_for("prs/mine", "state", "changes").status == "active"


def test_an_adopted_direct_process_is_recognised(monkeypatch):
    seen = []
    monkeypatch.setattr("xa.work.os.kill", lambda pid, signal: seen.append((pid, signal)))

    from xa.work import adopted_session_alive

    assert adopted_session_alive("pid:6200")
    assert seen == [(6200, 0)]


def test_ai_tmux_registry_entry_survives_a_missing_tmux_server(tmp_path, monkeypatch):
    store = Store(tmp_path / "xa.db")
    store.start_work(
        "m/k", "state", "m", "fix", "claude",
        session_name="ai-claude-work-1", session_backend="ai-tmux",
        session_cwd=str(tmp_path), started_at=NOW,
    )
    store.set_work_status("m/k", "state", "fix", "active")
    monkeypatch.setattr("xa.work.session_names", lambda cwd: {"ai-claude-work-1"})

    assert reconcile(store) == []
    assert store.work_for("m/k", "state", "fix").status == "active"


def test_ai_tmux_registry_removal_finishes_work(tmp_path, monkeypatch):
    store = Store(tmp_path / "xa.db")
    store.start_work(
        "m/k", "state", "m", "fix", "claude",
        session_name="ai-claude-work-1", session_backend="ai-tmux",
        session_cwd=str(tmp_path), started_at=NOW,
    )
    store.set_work_status("m/k", "state", "fix", "active")
    monkeypatch.setattr("xa.work.session_names", lambda cwd: set())

    assert [work.uid for work in reconcile(store)] == ["m/k"]
    assert store.work_for("m/k", "state", "fix").status == "finished"


def test_ai_tmux_registry_error_never_retires_work(tmp_path, monkeypatch):
    from xa.sessions import SessionError

    store = Store(tmp_path / "xa.db")
    store.start_work(
        "m/k", "state", "m", "fix", "claude",
        session_name="ai-claude-work-1", session_backend="ai-tmux",
        session_cwd=str(tmp_path), started_at=NOW,
    )
    store.set_work_status("m/k", "state", "fix", "active")

    def unavailable(cwd):
        raise SessionError("registry unavailable")

    monkeypatch.setattr("xa.work.session_names", unavailable)

    assert reconcile(store) == []
    assert store.work_for("m/k", "state", "fix").status == "active"


def test_claim_work_does_not_replace_an_active_session(tmp_path):
    store = Store(tmp_path / "xa.db")
    first = store.claim_work(
        "m/k", "state", "m", "fix", "claude",
        session_backend="ai-tmux", session_cwd=str(tmp_path),
    )

    assert first is not None
    assert store.claim_work("m/k", "state", "m", "fix", "claude") is None


def test_existing_work_rows_gain_session_backend_columns(tmp_path):
    path = tmp_path / "xa.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE work_sessions ("
        "uid TEXT, state_key TEXT, monitor TEXT, action TEXT, agent TEXT,"
        "status TEXT, marker TEXT, session_name TEXT, started_at TEXT, updated_at TEXT,"
        "PRIMARY KEY(uid,state_key,action))"
    )
    db.execute(
        "INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("m/k", "state", "m", "fix", "claude", "active", "", "pid:10",
         NOW.isoformat(), NOW.isoformat()),
    )
    db.commit()
    db.close()

    work = Store(path).work_for("m/k", "state", "fix")

    assert work.session_name == "pid:10"
    assert work.session_backend == ""
    assert work.session_cwd == ""

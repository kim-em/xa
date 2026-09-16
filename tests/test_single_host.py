"""One machine collects, and it is the machine you read on.

xa used to collect on one host and ship a snapshot to the others, so `xa ack`
wrote to a local database *and* posted the same mutation to the collector over
HTTP, queueing it in an outbox when that failed. There is one database now.
These pin the path that replaced it, including the part the old design could
not honestly promise: acknowledging something opens no socket at all.
"""

import json
import socket
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from xa import cli
from xa.collect import collect, write_snapshot
from xa.config import Config, MonitorSpec
from xa.model import MonitorReport, Observation, utcnow
from xa.store import Store

CONFIG = """
[monitor.m]
exec = "monitors/m"
interval = "1h"
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An xa installation in a temporary directory, with one item to act on."""
    for name in ("state", "cache", "policy"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("XA_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("XA_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("XA_POLICY", str(tmp_path / "policy"))
    (tmp_path / "policy" / "config.toml").write_text(CONFIG)

    cfg = Config(root=tmp_path / "policy")
    cfg.monitors["m"] = MonitorSpec(name="m", exec="monitors/m", interval=timedelta(hours=1))
    store = Store(cli.db_path())
    report = MonitorReport(
        monitor="m", collected_at=utcnow(),
        observations=[Observation(key="k", title="something is wrong", since=utcnow())],
    )
    write_snapshot(collect(cfg, store, reports=[report]), cli.snapshot_path())
    return tmp_path


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the mutation path opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def dispositions(uid: str = "m/k") -> list[str]:
    return [s.disposition for s in Store(cli.db_path()).suppressions() if s.uid == uid]


def configure_session_action(home: Path) -> None:
    (home / "policy" / "config.toml").write_text(
        CONFIG
        + f'''\n[[monitor.m.actions]]
id = "fix"
label = "Fix it"
kind = "session"
agent = "claude"
cwd = {json.dumps(str(home))}
prompt = "prompts/fix.md"
'''
    )
    (home / "policy" / "prompts").mkdir(exist_ok=True)
    (home / "policy" / "prompts" / "fix.md").write_text("Fix the thing")
    snapshot = json.loads(cli.snapshot_path().read_text())
    snapshot["items"][0]["actions"] = ["fix"]
    snapshot["items"][0]["action_labels"] = {"fix": "Fix it"}
    cli.snapshot_path().write_text(json.dumps(snapshot))


def test_acking_writes_to_the_database_and_touches_no_network(home, no_network):
    assert cli.main(["ack", "m/k"]) == 0
    assert dispositions() == ["acked"]


def test_snoozing_and_unmuting_round_trip(home, no_network):
    assert cli.main(["snooze", "m/k", "3h"]) == 0
    assert dispositions() == ["snoozed"]
    assert cli.main(["unmute", "m/k"]) == 0
    assert dispositions() == []


def test_muting_binds_to_every_state(home, no_network):
    assert cli.main(["mute", "m/k"]) == 0
    assert [s.state_key for s in Store(cli.db_path()).suppressions()] == ["*"]


def test_mode_and_threshold_are_stored_locally(home, no_network):
    assert cli.main(["mode", "m", "investigate"]) == 0
    assert Store(cli.db_path()).modes()["m"] == "investigate"
    assert cli.main(["threshold", "m.alert_after", "6h"]) == 0
    assert Store(cli.db_path()).overrides()["m"]["alert_after"] == "6h"


def test_suppressions_reports_the_database_not_the_snapshot(home, no_network, capsys):
    """The snapshot is a published picture; the database is what is true.

    `xa suppressions` used to prefer the snapshot, because on a machine that was
    not the collector the local database was only a write-ahead log of writes on
    their way elsewhere. Here it would report a stale published copy over an ack
    made a second ago.
    """
    snapshot = json.loads(cli.snapshot_path().read_text())
    snapshot["suppressions"] = [
        {"uid": "m/stale", "state_key": "x", "disposition": "acked", "until": None, "note": ""}
    ]
    cli.snapshot_path().write_text(json.dumps(snapshot))

    assert cli.main(["suppressions"]) == 0
    assert "no suppressions" in capsys.readouterr().out

    assert cli.main(["ack", "m/k"]) == 0
    capsys.readouterr()
    assert cli.main(["suppressions"]) == 0
    out = capsys.readouterr().out
    assert "m/k" in out and "m/stale" not in out


def test_the_ack_is_visible_in_the_next_read_without_waiting_for_a_tick(home, no_network, capsys):
    """`_mark` republishes the snapshot itself rather than waiting for a tick."""
    cli.main([])
    assert "something is wrong" in capsys.readouterr().out

    cli.main(["ack", "m/k"])
    capsys.readouterr()
    cli.main([])
    assert "something is wrong" not in capsys.readouterr().out


def test_refresh_accepts_an_item_id_and_reports_that_it_cleared(
    home, monkeypatch, capsys
):
    from xa.collect import Snapshot, read_snapshot

    def refreshed(cfg, store, only=None, force=False):
        assert only == ["m"]
        assert force is True
        return Snapshot(generated_at=utcnow(), monitors=[{"name": "m", "ok": True}])

    monkeypatch.setattr("xa.collect.collect", refreshed)

    assert cli.main(["refresh", "m/k"]) == 0

    out = capsys.readouterr().out
    assert "m: refreshed" in out
    assert "cleared  m/k" in out
    assert read_snapshot(cli.snapshot_path())["items"] == []


def test_a_cluster_member_can_be_muted_without_muting_the_summary(home, capsys):
    snapshot = json.loads(cli.snapshot_path().read_text())
    member = dict(
        snapshot["items"][0], uid="m/stale/org/a", key="stale/org/a",
        state_key="repo-state", title="org/a is stale",
        evidence={"repo": "org/a"}, cluster_key="stale",
        cluster_title="{count} repositories are stale", cluster_metric="stale",
    )
    snapshot["items"][0].update(
        uid="m/stale", key="stale", title="1 repositories are stale",
        cluster_size=1, cluster_key="stale",
        cluster_title="{count} repositories are stale", cluster_metric="stale",
        metrics={"stale": 1}, evidence={"cluster_members": [member]},
    )
    cli.snapshot_path().write_text(json.dumps(snapshot))

    assert cli.main(["mute", "m/stale/org/a"]) == 0

    assert [s.uid for s in Store(cli.db_path()).suppressions()] == ["m/stale/org/a"]
    assert json.loads(cli.snapshot_path().read_text())["items"] == []
    assert "muted: m/stale/org/a" in capsys.readouterr().out


def test_mute_refuses_an_addressable_cluster_summary(home):
    snapshot = json.loads(cli.snapshot_path().read_text())
    snapshot["items"][0].update(
        uid="m/stale", key="stale", cluster_key="stale",
        evidence={"cluster_members": [{"uid": "m/stale/org/a"}]},
    )
    cli.snapshot_path().write_text(json.dumps(snapshot))

    with pytest.raises(SystemExit, match="mute one of the members"):
        cli.main(["mute", "m/stale"])


def test_prompt_explains_when_given_an_item_id(home):
    (home / "policy" / "config.toml").write_text(
        CONFIG
        + """
offer = ["fix"]

[[monitor.m.actions]]
id = "fix"
label = "Fix it"
prompt = "prompts/fix.md"
"""
    )
    (home / "policy" / "prompts").mkdir()
    (home / "policy" / "prompts" / "fix.md").write_text("Fix it")
    snapshot = json.loads(cli.snapshot_path().read_text())
    snapshot["items"][0]["actions"] = ["fix"]
    cli.snapshot_path().write_text(json.dumps(snapshot))

    with pytest.raises(SystemExit) as raised:
        cli.main(["prompt", "m/k"])

    message = str(raised.value)
    assert "is an item ID" in message
    assert "xa open m/k --show-prompt" in message
    assert "xa prompt m.fix" in message


def test_prompt_unknown_action_points_at_the_action_list(home):
    with pytest.raises(SystemExit, match="xa actions"):
        cli.main(["prompt", "not-an-action"])


def test_nothing_in_the_engine_imports_aiohttp():
    """The HTTP server was the only reason for a third-party dependency."""
    import xa

    offenders = [
        p.name for p in Path(xa.__file__).parent.glob("*.py") if "aiohttp" in p.read_text()
    ]
    assert offenders == []


def test_doctor_checks_the_system_daemon_and_snapshot_freshness(
    home, monkeypatch, capsys
):
    def run(command, **kwargs):
        if command[:2] == ["launchctl", "print"] and command[2].startswith("system/"):
            return SimpleNamespace(returncode=0, stdout="state = running\npid = 123\n")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "running (system)" in out
    assert "fresh (" in out


def test_doctor_fails_for_a_dead_daemon_and_stale_snapshot(
    home, monkeypatch, capsys
):
    snapshot = json.loads(cli.snapshot_path().read_text())
    snapshot["generated_at"] = "2020-01-01T00:00:00+00:00"
    cli.snapshot_path().write_text(json.dumps(snapshot))
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="")
    )

    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "daemon       not running" in out
    assert "stale (" in out


def test_open_creates_and_records_a_durable_session(home, monkeypatch, capsys):
    configure_session_action(home)
    window = home / "the-window-i-typed-in"
    window.mkdir()
    monkeypatch.setenv("XA_SESSION_OWNER", str(window))
    attached = []
    created = []
    monkeypatch.setattr("xa.sessions.new_name", lambda: "ai-claude-test-1")
    monkeypatch.setattr(
        "xa.sessions.create",
        lambda *args, **kwargs: created.append((args, kwargs)) or "ai-claude-test-1",
    )
    monkeypatch.setattr(
        "xa.sessions.attach", lambda name, cwd: attached.append((name, cwd)) or 0
    )
    monkeypatch.setattr("xa.sessions.is_registered", lambda name, cwd: True)

    assert cli.main(["open", "m/k"]) == 0

    # The fixture observation has a generated state key; find the row directly.
    row = Store(cli.db_path()).execute(
        "SELECT * FROM work_sessions WHERE uid='m/k' AND action='fix'"
    ).fetchone()
    assert row["status"] == "active"
    assert row["session_backend"] == "ai-tmux"
    assert row["session_name"] == "ai-claude-test-1"
    # The agent works where the item says; the window that reopens its tab is
    # the one the command was typed in, and it is the registry that is keyed by
    # the second. Conflating them files the session where no window looks.
    assert row["session_cwd"] == str(home)
    assert row["session_owner"] == str(window)
    assert created[0][0][2] == home
    assert created[0][1]["owner"] == window
    assert attached == [("ai-claude-test-1", window)]
    assert "Fix it → claude" in capsys.readouterr().out


def test_open_retires_a_dead_legacy_pid_and_starts_fresh(home, monkeypatch):
    configure_session_action(home)
    item = json.loads(cli.snapshot_path().read_text())["items"][0]
    store = Store(cli.db_path())
    store.start_work(
        "m/k", item["state_key"], "m", "fix", "claude", session_name="pid:999"
    )
    store.set_work_status("m/k", item["state_key"], "fix", "active")
    monkeypatch.setattr("xa.work.adopted_session_alive", lambda name: False)
    monkeypatch.setattr("xa.sessions.new_name", lambda: "ai-claude-test-2")
    monkeypatch.setattr("xa.sessions.create", lambda *args, **kwargs: "ai-claude-test-2")
    monkeypatch.setattr("xa.sessions.attach", lambda name, cwd: 0)
    monkeypatch.setattr("xa.sessions.is_registered", lambda name, cwd: True)

    assert cli.main(["open", "m/k"]) == 0

    work = Store(cli.db_path()).work_for("m/k", item["state_key"], "fix")
    assert work.status == "active"
    assert work.session_backend == "ai-tmux"
    assert work.session_name == "ai-claude-test-2"


def test_open_attaches_an_existing_durable_session_without_creating_one(
    home, monkeypatch, capsys
):
    configure_session_action(home)
    original_cwd = home / "original-workspace"
    item = json.loads(cli.snapshot_path().read_text())["items"][0]
    store = Store(cli.db_path())
    store.start_work(
        "m/k", item["state_key"], "m", "fix", "claude",
        session_name="ai-claude-test-3", session_backend="ai-tmux",
        session_cwd=str(original_cwd),
    )
    store.set_work_status("m/k", item["state_key"], "fix", "active")
    monkeypatch.setattr("xa.sessions.is_registered", lambda name, cwd: True)
    monkeypatch.setattr(
        "xa.sessions.create", lambda *args, **kwargs: pytest.fail("created a rival session")
    )
    attached = []
    monkeypatch.setattr(
        "xa.sessions.attach", lambda name, cwd: attached.append((name, cwd)) or 0
    )

    assert cli.main(["open", "m/k"]) == 0
    assert attached == [("ai-claude-test-3", original_cwd)]
    assert "Reopening fix it" in capsys.readouterr().out

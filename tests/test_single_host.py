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


def test_nothing_in_the_engine_imports_aiohttp():
    """The HTTP server was the only reason for a third-party dependency."""
    import xa

    offenders = [
        p.name for p in Path(xa.__file__).parent.glob("*.py") if "aiohttp" in p.read_text()
    ]
    assert offenders == []

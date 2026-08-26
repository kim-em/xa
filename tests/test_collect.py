"""Collection behaviour, especially what the snapshot is."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from xa.collect import collect, due, write_snapshot, read_snapshot
from xa.config import Config, MonitorSpec
from xa.model import MonitorReport, Observation, utcnow
from xa.policy import Thresholds
from xa.store import Store


def store(tmp_path) -> Store:
    return Store(tmp_path / "xa.db")


def spec(name="m", **kw) -> MonitorSpec:
    return MonitorSpec(name=name, exec=f"monitors/{name}",
                       interval=kw.pop("interval", timedelta(hours=1)),
                       thresholds=kw.pop("thresholds", Thresholds(ttl=timedelta(hours=6))), **kw)


def cfg(tmp_path, *specs) -> Config:
    c = Config(root=tmp_path)
    for s in specs:
        c.monitors[s.name] = s
    return c


def report(monitor="m", key="k", **kw) -> MonitorReport:
    return MonitorReport(monitor=monitor, collected_at=kw.pop("at", utcnow()),
                         observations=[Observation(key=key, title="t", since=utcnow())])


def test_snapshot_survives_a_tick_where_nothing_is_due(tmp_path):
    """The snapshot is the current picture, not a log of the last tick.

    Rebuilding it from only what just ran erased every monitor that happened not
    to be scheduled, which reads to the user as "all clear".
    """
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    # Store the fingerprint too, or the monitor counts as edited and is due.
    st.save_report(report(), c.monitors["m"].fingerprint(tmp_path))
    st.record_run("m", utcnow(), True, None, 5)

    snapshot = collect(c, st)          # nothing is due; the report should stand
    assert [i.obs.key for i in snapshot.items] == ["k"]


def test_a_deleted_monitor_disappears(tmp_path):
    """Removing a monitor from config should not leave its items frozen forever."""
    st = store(tmp_path)
    st.save_report(report(monitor="gone"))
    snapshot = collect(cfg(tmp_path), st)
    assert snapshot.items == []


def test_a_stale_report_goes_unknown_rather_than_stays_ok(tmp_path):
    st = store(tmp_path)
    old = report(at=utcnow() - timedelta(hours=12))
    st.save_report(old)
    st.record_run("m", old.collected_at, True, None, 5)
    snapshot = collect(cfg(tmp_path, spec("m")), st)
    assert [i.severity for i in snapshot.items] == ["unknown"]


def test_first_seen_supplies_a_missing_start_time(tmp_path):
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    r = MonitorReport(monitor="m", observations=[Observation(key="k", title="t")])  # no `since`
    snapshot = collect(c, st, reports=[r])
    assert snapshot.items[0].obs.since is not None


def test_first_seen_is_stable_across_collections(tmp_path):
    """An unchanged problem must keep ageing, not reset its clock every run."""
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    make = lambda: MonitorReport(monitor="m", observations=[Observation(key="k", title="t")])
    first = collect(c, st, reports=[make()]).items[0].obs.since
    second = collect(c, st, reports=[make()]).items[0].obs.since
    assert first == second


def test_snapshot_round_trips_atomically(tmp_path):
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    snapshot = collect(c, st, reports=[report()])
    path = tmp_path / "snap.json"
    write_snapshot(snapshot, path)
    assert read_snapshot(path)["items"][0]["key"] == "k"
    assert not path.with_suffix(".json.tmp").exists()


def test_a_missing_executable_reports_unknown_not_health(tmp_path):
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    snapshot = collect(c, st, force=True)
    assert len(snapshot.items) == 1
    assert snapshot.items[0].severity == "unknown"
    assert snapshot.count == 0        # our bug, not the world's


def test_a_changed_monitor_is_due_regardless_of_interval(tmp_path):
    """Editing a monitor should take effect now, not after its interval.

    Waiting an hour to see whether a fix worked is what stops you fixing a
    noisy check at all.
    """
    st = store(tmp_path)
    exe = tmp_path / "monitors" / "m"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho '{}'\n")
    c = cfg(tmp_path, spec("m"))
    s = c.monitors["m"]

    st.record_run("m", utcnow(), True, None, 1)
    st.save_report(report(), s.fingerprint(tmp_path))
    assert not due(s, st, utcnow(), tmp_path)      # unchanged: wait for the interval

    exe.write_text("#!/bin/sh\necho '{\"observations\": []}'\n")
    assert due(s, st, utcnow(), tmp_path)          # edited: run it now


def test_a_monitor_that_failed_is_retried_long_before_its_interval(tmp_path):
    """The collector runs on a laptop, which wakes with no network for a moment.

    Scheduling on the last run whatever its verdict meant one badly-timed
    failure left a six-hourly check reading `unknown` for six hours.
    """
    st, s = store(tmp_path), spec("m", interval=timedelta(hours=6))
    now = utcnow()

    st.record_run("m", now - timedelta(minutes=10), False, "no route to host", 1)
    assert due(s, st, now)                     # failed ten minutes ago: try again

    st.record_run("m", now - timedelta(minutes=1), False, "no route to host", 1)
    assert not due(s, st, now)                 # but not on every tick

    st.record_run("m", now - timedelta(minutes=10), True, None, 1)
    assert not due(s, st, now)                 # succeeded: the interval stands


def test_the_retry_never_slows_a_monitor_down(tmp_path):
    """A monitor whose interval is shorter than the retry keeps its own pace."""
    st, s = store(tmp_path), spec("m", interval=timedelta(seconds=30))
    now = utcnow()
    st.record_run("m", now - timedelta(minutes=1), False, "boom", 1)
    assert due(s, st, now)


def test_changed_options_also_make_a_monitor_due(tmp_path):
    st = store(tmp_path)
    (tmp_path / "monitors").mkdir()
    (tmp_path / "monitors" / "m").write_text("#!/bin/sh\n")
    c = cfg(tmp_path, spec("m"))
    s = c.monitors["m"]
    st.record_run("m", utcnow(), True, None, 1)
    st.save_report(report(), s.fingerprint(tmp_path))

    s.options = {"threshold": 5}
    assert due(s, st, utcnow(), tmp_path)


def test_an_existing_database_survives_a_schema_change(tmp_path):
    """History is the baseline every trend depends on, so an upgrade must not
    require deleting it."""
    import sqlite3

    path = tmp_path / "xa.db"
    Store(path).close()
    # Simulate the pre-migration shape.
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE latest_reports DROP COLUMN fingerprint")
    db.commit()
    db.close()

    st = Store(path)                        # must migrate, not crash
    st.save_report(report(), "abc123")
    assert st.fingerprint("m") == "abc123"

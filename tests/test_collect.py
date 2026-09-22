"""Collection behaviour, especially what the snapshot is."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from xa.collect import (
    _env_for,
    _muted_observation_keys,
    collect,
    due,
    offered,
    read_snapshot,
    write_snapshot,
)
from xa.config import Action, Config, MonitorSpec
from xa.model import MonitorReport, Observation, utcnow
from xa.policy import Suppression, Thresholds
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


def test_first_seen_resets_when_a_problem_clears_and_recurs(tmp_path):
    """A stable state identifies the problem, not one endless occurrence of it."""
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    first_at = utcnow() - timedelta(minutes=3)
    returned_at = utcnow()

    def fault(at):
        return MonitorReport(
            monitor="m", collected_at=at,
            observations=[Observation(key="k", title="t")],
        )

    first = collect(c, st, reports=[fault(first_at)]).items[0].obs.since
    cleared = collect(c, st, reports=[MonitorReport(monitor="m")])
    returned = collect(c, st, reports=[fault(returned_at)]).items[0].obs.since

    assert first == first_at
    assert cleared.items == []
    assert returned == returned_at


def test_a_failed_report_does_not_claim_a_problem_cleared(tmp_path):
    """Losing sight of a monitor is not evidence that its fault ended."""
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    first_at = utcnow() - timedelta(minutes=3)
    fault = lambda at: MonitorReport(
        monitor="m", collected_at=at,
        observations=[Observation(key="k", title="t")],
    )

    collect(c, st, reports=[fault(first_at)])
    collect(c, st, reports=[MonitorReport.crashed("m", "timed out")])
    returned = collect(c, st, reports=[fault(utcnow())]).items[0].obs.since

    assert returned == first_at


def test_snapshot_round_trips_atomically(tmp_path):
    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    snapshot = collect(c, st, reports=[report()])
    path = tmp_path / "snap.json"
    write_snapshot(snapshot, path)
    assert read_snapshot(path)["items"][0]["key"] == "k"
    assert not path.with_suffix(".json.tmp").exists()


def test_snapshot_carries_human_action_labels(tmp_path):
    """Status reads only the snapshot, so every word it renders must be there."""
    st = store(tmp_path)
    action = Action(id="fix", label="Fix the failing build")
    c = cfg(tmp_path, spec("m", actions={"fix": action}))
    r = MonitorReport(
        monitor="m",
        observations=[Observation(key="k", title="t", actions=["fix"])],
    )

    payload = collect(c, st, reports=[r]).to_json()
    assert payload["items"][0]["action_labels"] == {"fix": "Fix the failing build"}


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


def test_only_live_wildcard_mutes_are_passed_to_their_monitor(tmp_path):
    now = utcnow()
    suppressions = [
        Suppression("m/stale/org/a", "*", "muted"),
        Suppression("m/stale/org/b", "state", "muted"),
        Suppression("other/stale/org/c", "*", "muted"),
        Suppression("m/stale/org/d", "*", "muted", until=now - timedelta(seconds=1)),
    ]

    keys = _muted_observation_keys(suppressions, "m", now)

    assert keys == ["stale/org/a"]
    assert json.loads(_env_for(spec("m"), cfg(tmp_path), keys)["XA_MUTED_KEYS"]) == keys


def test_invalidating_a_report_makes_a_long_interval_monitor_due(tmp_path):
    st, c = store(tmp_path), cfg(tmp_path, spec("m", interval=timedelta(weeks=2)))
    monitor = c.monitors["m"]
    st.save_report(report(), monitor.fingerprint(tmp_path))
    st.record_run("m", utcnow(), True, None, 1)
    assert not due(monitor, st, utcnow(), tmp_path)

    st.invalidate_report("m")

    assert due(monitor, st, utcnow(), tmp_path)


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


# -- a published snapshot is a real snapshot ---------------------------------
#
# The daemon publishes after every monitor, so a sweep produces many snapshots
# and the user reads whichever one happened to land. They must all mean the
# same thing. Two builders drifted apart once; these are the two ways it showed.

def test_a_snapshot_published_mid_sweep_still_carries_its_plans(tmp_path):
    """Investigations are paid for once. A sweep must not withdraw them."""
    from xa.collect import snapshot_from_store

    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    st.save_report(report(), c.monitors["m"].fingerprint(tmp_path))

    # The observation sets no state key, so it is derived; ask the snapshot what
    # it came out as rather than guessing at the digest.
    key = snapshot_from_store(c, st).items[0].obs.state_key
    st.save_plan("m/k", key, "FINDING: something", ok=True)

    assert snapshot_from_store(c, st).items[0].plan == "FINDING: something"


def test_a_snapshot_published_mid_sweep_ages_items_the_same_way(tmp_path):
    """An observation with no start time reports `warn`, because it cannot be aged.

    The backfill that gives it one used to run only in the final snapshot, so a
    sweep invented faults that the end of the sweep then retracted -- and the
    headline count is the one number that has to be true.
    """
    from xa.collect import snapshot_from_store

    # A threshold that is not "immediately", so age is allowed to matter.
    st = store(tmp_path)
    c = cfg(tmp_path, spec("m", thresholds=Thresholds(warn_after=timedelta(hours=1))))
    undated = MonitorReport(monitor="m", collected_at=utcnow(),
                            observations=[Observation(key="k", title="t")])
    st.save_report(undated, c.monitors["m"].fingerprint(tmp_path))

    item = snapshot_from_store(c, st).items[0]
    assert item.obs.since is not None, "the backfill has to run wherever a snapshot is built"
    assert item.severity == "info", "young, not a fault invented by a missing timestamp"
    assert not item.counts, "and so not in the headline number either"


def test_both_builders_agree(tmp_path):
    """The property the two tests above are really about."""
    from xa.collect import snapshot_from_store

    st, c = store(tmp_path), cfg(tmp_path, spec("m"))
    st.save_report(MonitorReport(monitor="m", collected_at=utcnow(),
                                 observations=[Observation(key="k", title="t")]),
                   c.monitors["m"].fingerprint(tmp_path))
    st.record_run("m", utcnow(), True, None, 5)

    mid_sweep = snapshot_from_store(c, st)
    final = collect(c, st)
    assert [(i.uid, i.severity, i.plan) for i in mid_sweep.items] == \
           [(i.uid, i.severity, i.plan) for i in final.items]


def test_an_action_is_offered_only_while_its_slice_has_something_in_it():
    # A backlog row's counts move. Offering to triage the failing CI when
    # nothing is failing teaches you to stop reading the suggestions.
    m = spec("prs", actions={
        "red": Action(id="red", label="Triage the failing CI", when="red"),
        "conflicts": Action(id="conflicts", label="Triage the conflicts", when="conflicts"),
        "triage": Action(id="triage", label="Rank the queue"),
    })
    obs = Observation(
        key="mine", title="t", kind="backlog",
        evidence={"conflicts": [{"number": 1}]},
        actions=["red", "conflicts", "triage"],
    )

    # `red` has no evidence; `triage` names no slice and is always available.
    assert offered(obs, m) == ["conflicts", "triage"]


def test_an_empty_slice_is_the_same_as_a_missing_one():
    m = spec("prs", actions={"red": Action(id="red", label="l", when="red")})
    obs = Observation(key="mine", title="t", evidence={"red": []}, actions=["red"])

    assert offered(obs, m) == []


def test_an_action_the_monitor_names_but_policy_does_not_configure_is_dropped():
    m = spec("prs", actions={})
    obs = Observation(key="mine", title="t", actions=["red"])

    assert offered(obs, m) == []

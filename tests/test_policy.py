"""Policy tests.

The behaviour worth guarding hardest is acknowledgement lapse: a snooze must
survive the clock ticking and lapse the moment the failure becomes a different
failure. Getting that backwards makes the tool either useless or dangerous.
"""

from datetime import datetime, timedelta, timezone

import pytest

from xa.model import Item, MonitorReport, Observation
from xa.policy import (
    MonitorPolicy,
    Suppression,
    Thresholds,
    apply_suppressions,
    build_items,
    cluster,
    humanise,
    parse_duration,
    parse_when,
    severity_for,
)

NOW = datetime(2026, 8, 25, 5, 53, tzinfo=timezone.utc)


def obs(**kw):
    kw.setdefault("key", "k")
    kw.setdefault("title", "t")
    return Observation(**kw)


# -- durations and when -----------------------------------------------------

@pytest.mark.parametrize("text,seconds", [("30s", 30), ("90m", 5400), ("6h", 21600), ("2d", 172800), ("1w", 604800)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == timedelta(seconds=seconds)


def test_parse_duration_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_parse_when_relative():
    assert parse_when("3h", NOW) == NOW + timedelta(hours=3)


def test_parse_when_tomorrow_is_the_morning():
    """"Tell me again tomorrow" means the morning, not one minute past midnight."""
    got = parse_when("tomorrow", NOW, hour=9).astimezone()
    assert (got.hour, got.minute) == (9, 0)
    assert got.date() > NOW.astimezone().date()


def test_parse_when_weekday_never_resolves_to_today():
    # NOW is a Tuesday; asking for "tue" means next week, not zero seconds.
    assert parse_when("tue", NOW) > NOW + timedelta(days=6)


def test_humanise():
    assert humanise(45) == "45s"
    assert humanise(3600) == "60m"
    assert humanise(9 * 3600) == "9h"
    assert humanise(4 * 86400) == "4d"
    assert humanise(None) == "-"


def test_labeled_links_round_trip_through_the_monitor_model():
    links = [{"label": "Discussion", "url": "https://example.test/topic/1"}]
    observation = Observation.from_json({"key": "k", "title": "t", "links": links})
    assert observation.to_json()["links"] == links


def test_why_label_round_trips_through_the_monitor_model():
    observation = Observation.from_json(
        {"key": "k", "title": "t", "why_label": "Choose what to stop watching"}
    )
    assert observation.to_json()["why_label"] == "Choose what to stop watching"


def test_direct_commands_round_trip_through_the_monitor_model():
    commands = [{"label": "Run it", "command": "xa run backup"}]
    observation = Observation.from_json({"key": "k", "title": "t", "commands": commands})
    assert observation.to_json()["commands"] == commands


# -- thresholds -------------------------------------------------------------

def test_severity_ladder():
    th = Thresholds(warn_after=timedelta(hours=2), alert_after=timedelta(hours=12))
    assert severity_for(obs(since=NOW - timedelta(minutes=30)), th, NOW) == "info"
    assert severity_for(obs(since=NOW - timedelta(hours=3)), th, NOW) == "warn"
    assert severity_for(obs(since=NOW - timedelta(hours=20)), th, NOW) == "alert"


def test_threshold_boundaries_are_inclusive():
    th = Thresholds(warn_after=timedelta(hours=2), alert_after=timedelta(hours=12))
    assert severity_for(obs(since=NOW - timedelta(hours=2)), th, NOW) == "warn"
    assert severity_for(obs(since=NOW - timedelta(hours=12)), th, NOW) == "alert"


def test_backlog_never_escalates():
    """A standing pile is a number with a trend, not a permanent emergency."""
    th = Thresholds(warn_after=timedelta(0), alert_after=timedelta(0))
    assert severity_for(obs(kind="backlog", since=NOW - timedelta(days=400)), th, NOW) == "info"


def test_status_never_escalates():
    """Healthy context is never something that needs the user."""
    th = Thresholds(warn_after=timedelta(0), alert_after=timedelta(0))
    assert severity_for(obs(kind="status", since=NOW - timedelta(days=400)), th, NOW) == "info"


def test_stale_data_is_unknown_not_ok():
    th = Thresholds(warn_after=timedelta(0))
    assert severity_for(obs(since=NOW), th, NOW, stale=True) == "unknown"


def test_ttl_defaults_to_three_intervals():
    th = Thresholds.from_config({}, interval=timedelta(minutes=30))
    assert th.ttl == timedelta(minutes=90)


# -- acknowledgement lapse --------------------------------------------------

def item(state_key="s1", uid_key="k", monitor="m", kind="fault"):
    return Item(monitor=monitor, obs=obs(key=uid_key, state_key=state_key, kind=kind))


def test_snooze_survives_the_clock_ticking():
    s = Suppression("m/k", "s1", "snoozed", until=NOW + timedelta(hours=3))
    assert apply_suppressions(item(), [s], NOW).disposition == "snoozed"
    # Same failure, one hour later: still snoozed.
    assert apply_suppressions(item(), [s], NOW + timedelta(hours=1)).disposition == "snoozed"


def test_snooze_lapses_when_the_failure_changes():
    """The whole point of binding to state_key rather than to the item."""
    s = Suppression("m/k", "s1", "snoozed", until=NOW + timedelta(days=30))
    assert apply_suppressions(item(state_key="s2"), [s], NOW).disposition == "active"


def test_snooze_expires():
    s = Suppression("m/k", "s1", "snoozed", until=NOW - timedelta(seconds=1))
    assert apply_suppressions(item(), [s], NOW).disposition == "active"


def test_mute_matches_any_state():
    """A false positive will never be right, whatever shape it takes next."""
    s = Suppression("m/k", "*", "muted", until=None)
    assert apply_suppressions(item(state_key="anything"), [s], NOW).disposition == "muted"


def test_specific_ack_beats_a_wildcard_mute():
    wildcard = Suppression("m/k", "*", "muted", until=None)
    exact = Suppression("m/k", "s1", "acked", until=NOW + timedelta(days=1))
    assert apply_suppressions(item(), [wildcard, exact], NOW).disposition == "acked"


def test_suppression_does_not_leak_across_items():
    s = Suppression("m/other", "s1", "muted", until=None)
    assert apply_suppressions(item(), [s], NOW).disposition == "active"


def test_suppressed_items_do_not_count():
    i = apply_suppressions(item(), [Suppression("m/k", "s1", "acked", None)], NOW)
    i.severity = "alert"
    assert not i.counts


# -- clustering -------------------------------------------------------------

def test_cluster_collapses_and_keeps_the_worst():
    """Fifteen conflicts from one batch are one problem, not fifteen."""
    a = Item(monitor="m", obs=obs(key="a", cluster="batch", since=NOW - timedelta(days=1)), severity="warn")
    b = Item(monitor="m", obs=obs(key="b", cluster="batch", since=NOW - timedelta(days=5)), severity="alert")
    c = Item(monitor="m", obs=obs(key="c"), severity="info")
    out = cluster([a, b, c])
    assert len(out) == 2
    head = out[0]
    assert head.cluster_size == 2
    assert head.severity == "alert"                      # worst member wins
    assert head.obs.since == NOW - timedelta(days=5)      # oldest start wins


def test_clusters_do_not_merge_across_monitors():
    a = Item(monitor="m1", obs=obs(key="a", cluster="batch"))
    b = Item(monitor="m2", obs=obs(key="b", cluster="batch"))
    assert len(cluster([a, b])) == 2


# -- the pipeline -----------------------------------------------------------

def test_crashed_monitor_surfaces_as_unknown():
    """Losing sight of something is not the same as seeing that it is fine."""
    items = build_items([MonitorReport.crashed("m", "boom")], {}, now=NOW)
    assert len(items) == 1
    assert items[0].severity == "unknown"
    assert not items[0].counts          # a tooling bug is not a world problem
    assert "boom" in items[0].obs.detail


def test_stale_report_marks_items_unknown():
    report = MonitorReport("m", collected_at=NOW - timedelta(hours=5), observations=[obs(since=NOW)])
    policies = {"m": MonitorPolicy("m", Thresholds(ttl=timedelta(hours=1)))}
    assert build_items([report], policies, now=NOW)[0].severity == "unknown"


def test_only_faults_past_a_threshold_count():
    th = Thresholds(warn_after=timedelta(hours=1))
    policies = {"m": MonitorPolicy("m", th)}
    report = MonitorReport(
        "m",
        collected_at=NOW,
        observations=[
            obs(key="old", since=NOW - timedelta(hours=5)),                    # counts
            obs(key="new", since=NOW),                                          # too young
            obs(key="pend", kind="pending", since=NOW - timedelta(days=9)),     # not a fault
            obs(key="back", kind="backlog", since=NOW - timedelta(days=9)),     # not a fault
            obs(key="status", kind="status", since=NOW - timedelta(days=9)),    # not a fault
        ],
    )
    items = build_items([report], policies, now=NOW)
    assert sum(1 for i in items if i.counts) == 1


def test_cluster_keeps_its_members():
    """Collapsing a group must not discard what was in it.

    A row reading "13 things, oldest 123 days" is unanswerable: you cannot tell
    whether any of them is yours, or current, or real. That is how a cluster
    becomes noise rather than a summary.
    """
    a = Item(monitor="m", obs=obs(key="a", title="first", cluster="batch", since=NOW))
    b = Item(monitor="m", obs=obs(key="b", title="second", cluster="batch", since=NOW))
    head = cluster([a, b])[0]
    members = head.obs.evidence["cluster_members"]
    assert [m["title"] for m in members] == ["second"]
    assert head.cluster_size == 2


def test_addressable_cluster_builds_a_stable_summary_with_full_members():
    import json

    common = {
        "cluster": "stale-members",
        "cluster_key": "stale",
        "cluster_title": "{count} repositories are stale",
        "cluster_metric": "stale",
    }
    a = Item(
        monitor="tools",
        obs=obs(key="stale/org/a", state_key="sa", evidence={"repo": "org/a"},
                metrics={"stale": 1}, **common),
    )
    b = Item(
        monitor="tools",
        obs=obs(key="stale/org/b", state_key="sb", evidence={"repo": "org/b"},
                metrics={"stale": 1}, **common),
    )

    summary = cluster([a, b])[0]

    assert summary.uid == "tools/stale"
    assert summary.obs.title == "2 repositories are stale"
    assert summary.obs.metrics["stale"] == 2
    assert [m["uid"] for m in summary.obs.evidence["cluster_members"]] == [
        "tools/stale/org/a", "tools/stale/org/b",
    ]
    json.dumps(summary.to_json())


def test_muted_member_is_not_folded_back_into_the_active_cluster():
    common = {
        "cluster": "stale-members",
        "cluster_key": "stale",
        "cluster_title": "{count} repositories are stale",
    }
    report = MonitorReport(
        "tools",
        collected_at=NOW,
        observations=[
            obs(key="stale/org/a", state_key="sa", **common),
            obs(key="stale/org/b", state_key="sb", **common),
        ],
    )

    items = build_items(
        [report], {}, [Suppression("tools/stale/org/b", "*", "muted")], now=NOW
    )

    active = next(item for item in items if item.disposition == "active")
    muted = next(item for item in items if item.disposition == "muted")
    assert active.uid == "tools/stale"
    assert active.obs.title == "1 repositories are stale"
    assert [m["uid"] for m in active.obs.evidence["cluster_members"]] == [
        "tools/stale/org/a"
    ]
    assert muted.uid == "tools/stale/org/b"

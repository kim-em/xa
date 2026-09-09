"""`xa help` explains the model; --help lists the flags.

These tests guard the properties that make it worth having: that every topic
renders, that the overview points only at topics that exist, and that an unknown
topic says what it should have been.
"""

import pytest

from xa.help import TOPICS, render


def test_overview_renders():
    out = render()
    assert "external amygdala" in out
    assert "Monitors emit facts" not in out          # that is the `model` topic


@pytest.mark.parametrize("topic", sorted(TOPICS))
def test_every_topic_renders(topic):
    out = render(topic)
    assert len(out) > 200
    # No unsubstituted placeholders. Checked by name rather than by brace,
    # because the monitor example legitimately contains a dict literal.
    assert "{h_" not in out
    assert "{title}" not in out


def test_overview_only_advertises_topics_that_exist():
    """A help page that points at a page that does not exist is worse than none."""
    import re

    advertised = set(re.findall(r"xa help (\w+)", render()))
    assert advertised
    assert advertised <= set(TOPICS)


def test_unknown_topic_lists_the_real_ones():
    out = render("nonsense")
    assert "no help topic" in out
    assert "model" in out


def test_topics_do_not_leak_format_syntax():
    """The monitor example contains a dict literal, which format() will eat."""
    assert '{"log": excerpt}' in render("monitors")


def test_fault_section_count_matches_the_headline():
    """A section reading FAULTS (4) above a headline reading 3 faults invites
    exactly one question, and the tool should not provoke it."""
    import re

    from xa.render import render

    snapshot = {
        "generated_at": "2026-08-25T05:53:00+00:00",
        "monitors": [],
        "items": [
            {"uid": "m/a", "monitor": "m", "key": "a", "kind": "fault", "severity": "warn",
             "disposition": "active", "counts": True, "state_key": "s", "title": "urgent",
             "detail": "", "since": "2026-08-20T00:00:00+00:00", "url": None, "cluster": None,
             "cluster_size": 1, "mode": "report", "plan": None, "metrics": {}, "evidence": {},
             "actions": ["x"]},
            {"uid": "m/b", "monitor": "m", "key": "b", "kind": "fault", "severity": "info",
             "disposition": "active", "counts": False, "state_key": "s", "title": "not yet",
             "detail": "", "since": "2026-08-25T05:00:00+00:00", "url": None, "cluster": None,
             "cluster_size": 1, "mode": "report", "plan": None, "metrics": {}, "evidence": {},
             "actions": ["x"]},
        ],
    }
    out = render(snapshot)
    assert "FAULTS (1)" in out
    assert "below threshold (1)" in out
    headline = int(re.search(r"(\d+) fault", out).group(1))
    section = int(re.search(r"FAULTS \((\d+)\)", out).group(1))
    assert headline == section


def test_single_action_is_a_human_copyable_next_step():
    from xa.render import render, render_detail

    snapshot = {
        "generated_at": "2026-08-25T05:53:00+00:00",
        "monitors": [],
        "items": [{
            "uid": "build/red", "monitor": "build", "key": "red", "kind": "fault",
            "severity": "warn", "disposition": "active", "counts": True,
            "state_key": "s", "title": "the build is red", "detail": "", "since": None,
            "cluster_size": 1, "plan": None, "metrics": {}, "evidence": {},
            "actions": ["fix"], "action_labels": {"fix": "Fix the failing build"},
        }],
    }

    out = render(snapshot)
    assert "Fix the failing build: xa open build/red" in out
    assert "[fix]" not in out
    assert "xa open build/red fix" not in out
    assert "Next: xa why <id> · xa investigate <id> · xa open <id>" in out
    assert "\033[35mxa open build/red\033[0m" in render_detail(
        snapshot["items"][0], colour=True
    )


def test_action_commands_are_never_hard_wrapped():
    from xa.render import render

    uid = "messages/a person/a conversation with a deliberately long subject"
    snapshot = {
        "generated_at": "2026-08-25T05:53:00+00:00",
        "monitors": [],
        "items": [{
            "uid": uid, "monitor": "messages", "key": "long", "kind": "pending",
            "severity": "warn", "disposition": "active", "counts": False,
            "state_key": "s", "title": "reply needed", "detail": "", "since": None,
            "cluster_size": 1, "plan": None, "metrics": {}, "evidence": {},
            "actions": ["reply"], "action_labels": {"reply": "Draft a thoughtful reply"},
        }],
    }

    assert f"xa open {uid!r}" in render(snapshot)


def test_action_ids_are_shown_when_the_user_must_choose_one():
    from xa.render import render

    snapshot = {
        "generated_at": "2026-08-25T05:53:00+00:00",
        "monitors": [],
        "items": [{
            "uid": "pr/12", "monitor": "pr", "key": "12", "kind": "pending",
            "severity": "warn", "disposition": "active", "counts": False,
            "state_key": "s", "title": "PR needs attention", "detail": "", "since": None,
            "cluster_size": 1, "plan": None, "metrics": {}, "evidence": {},
            "actions": ["review", "close"],
            "action_labels": {"review": "Review it", "close": "Close it"},
        }],
    }

    out = render(snapshot)
    assert "Review it: xa open pr/12 review" in out
    assert "Close it: xa open pr/12 close" in out


def test_backlog_actions_are_not_hidden_by_the_compact_renderer():
    from xa.render import render

    snapshot = {
        "generated_at": "2026-08-25T05:53:00+00:00",
        "monitors": [],
        "items": [{
            "uid": "reviews/owed", "monitor": "reviews", "key": "owed",
            "kind": "backlog", "severity": "info", "disposition": "active",
            "counts": False, "title": "reviews waiting", "metrics": {"open": 12},
            "actions": ["triage"], "action_labels": {"triage": "Triage the queue"},
        }],
    }

    assert "Triage the queue: xa open reviews/owed" in render(snapshot)

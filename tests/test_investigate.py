"""The investigate rung.

The properties worth guarding are about *not* doing work: not re-investigating
an unchanged problem, not investigating what has been silenced, and not acting.
"""

from datetime import timedelta

import pytest

from xa.investigate import _agent_command, verdict, wanted
from xa.model import Item, Observation, utcnow
from xa.store import Store


def store(tmp_path):
    return Store(tmp_path / "xa.db")


def item(mode="investigate", disposition="active", kind="fault", actions=("fix",), state="s1"):
    return Item(
        monitor="m",
        obs=Observation(key="k", title="t", kind=kind, state_key=state, actions=list(actions)),
        mode=mode,
        disposition=disposition,
    )


def test_only_investigates_on_that_rung(tmp_path):
    st = store(tmp_path)
    assert wanted(item(mode="investigate"), st)
    assert not wanted(item(mode="report"), st)
    assert not wanted(item(mode="auto"), st)


def test_does_not_investigate_what_you_have_silenced(tmp_path):
    st = store(tmp_path)
    assert not wanted(item(disposition="snoozed"), st)
    assert not wanted(item(disposition="muted"), st)


def test_does_not_investigate_a_backlog(tmp_path):
    """A standing pile is a number with a trend; there is no incident to look into."""
    assert not wanted(item(kind="backlog"), store(tmp_path))


def test_does_not_investigate_an_item_with_no_action(tmp_path):
    assert not wanted(item(actions=()), store(tmp_path))


def test_does_not_repeat_an_investigation_for_an_unchanged_problem(tmp_path):
    """Re-running every collection would burn money reproducing a stored answer."""
    st = store(tmp_path)
    i = item()
    assert wanted(i, st)
    st.save_plan(i.uid, i.obs.state_key, "FINDING: something")
    assert not wanted(i, st)


def test_a_changed_problem_is_investigated_again(tmp_path):
    """Plans follow the same rule as acknowledgements: they are about a state."""
    st = store(tmp_path)
    st.save_plan("m/k", "s1", "FINDING: the old problem")
    assert wanted(item(state="s2"), st)


def test_verdict_extraction():
    plan = """Some preamble.

FINDING: the check measures mtime, not content
CONFIDENCE: high
FIXABLE: needs-a-decision
PLAN:
- ask whether the machine is retired or broken"""
    v = verdict(plan)
    assert v["fixable"] == "needs-a-decision"
    assert v["confidence"] == "high"
    assert v["finding"].startswith("the check measures")


def test_verdict_of_an_unstructured_plan_is_empty():
    assert verdict("I could not work it out.") == {}


def test_agent_command_is_headless_for_both_agents():
    assert "-p" in _agent_command("claude", "x")
    assert "exec" in _agent_command("codex", "x")

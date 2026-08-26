"""The investigate rung.

The properties worth guarding are about *not* doing work: not re-investigating
an unchanged problem, not investigating what has been silenced, and not acting.
"""

from datetime import timedelta

import pytest

from xa.investigate import RETRY_FAILED_AFTER, _agent_command, attach, verdict, wanted
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


def test_does_not_investigate_status(tmp_path):
    """Status is context, not an incident to look into."""
    assert not wanted(item(kind="status"), store(tmp_path))


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


def test_a_failed_investigation_is_not_an_answer(tmp_path):
    """A timeout says nothing about the problem, so it must not stand as the finding.

    Storing failures as plans retired the item from the rung permanently: the
    text was non-empty, so `wanted` saw an answer and never looked again.
    """
    st = store(tmp_path)
    i = item()
    st.save_plan(i.uid, i.obs.state_key, "(investigation timed out after 0:10:00)", ok=False)
    assert not wanted(i, st), "a failure should not be retried on the very next tick"
    assert wanted(i, st, now=utcnow() + RETRY_FAILED_AFTER + timedelta(minutes=1))


def test_a_successful_investigation_is_never_retried(tmp_path):
    """The backoff is for failures only; a real answer does not go stale on a timer."""
    st = store(tmp_path)
    i = item()
    st.save_plan(i.uid, i.obs.state_key, "FINDING: something", ok=True)
    assert not wanted(i, st, now=utcnow() + timedelta(days=365))


def test_attach_carries_the_verdict_not_just_the_text(tmp_path):
    """Without `ok`, a half-finished report is indistinguishable from a finished one."""
    st = store(tmp_path)
    good, bad = item(state="s1"), item(state="s2")
    st.save_plan(good.uid, "s1", "FINDING: a", ok=True)
    st.save_plan(bad.uid, "s2", "(investigation timed out)", ok=False)

    attach([good, bad], st)
    assert (good.plan, good.plan_ok) == ("FINDING: a", True)
    assert bad.plan_ok is False


def test_an_item_with_no_stored_plan_keeps_its_defaults(tmp_path):
    i = item()
    attach([i], store(tmp_path))
    assert i.plan is None and i.plan_ok is True


# -- what counts as a finished investigation --------------------------------

def _run_against(tmp_path, monkeypatch, command):
    """Run one investigation with `command` standing in for the agent."""
    from xa.config import Action, Config, MonitorSpec
    from xa import investigate

    cfg = Config(root=tmp_path)
    cfg.monitors["m"] = MonitorSpec(
        name="m", actions={"fix": Action(id="fix", label="Fix", cwd=str(tmp_path))}
    )
    monkeypatch.setattr(investigate, "_agent_command", lambda agent, prompt: command)
    return investigate.run(
        {"uid": "m/k", "monitor": "m", "key": "k", "state_key": "s1",
         "title": "t", "actions": ["fix"]},
        cfg, timedelta(seconds=30),
    )


def test_silence_is_not_a_finding(tmp_path, monkeypatch):
    """An agent that exits cleanly having said nothing has not investigated anything.

    This was the worst of the failure modes: empty output stored fine, blocked
    every retry, and rendered nothing at all, so the item looked untouched.
    """
    result = _run_against(tmp_path, monkeypatch, ["true"])
    assert result.ok is False
    assert result.plan, "a failure still has to say something a person can read"


def test_output_from_an_agent_that_crashed_is_kept_but_not_trusted(tmp_path, monkeypatch):
    """Half an answer is worth reading and worth retrying. It is not worth relying on."""
    result = _run_against(tmp_path, monkeypatch, ["sh", "-c", "echo 'FINDING: partial'; exit 1"])
    assert result.ok is False
    assert "FINDING: partial" in result.plan


def test_a_clean_run_with_output_is_an_answer(tmp_path, monkeypatch):
    result = _run_against(tmp_path, monkeypatch, ["echo", "FINDING: the disk is full"])
    assert result.ok is True
    assert verdict(result.plan)["finding"] == "the disk is full"


def test_the_agent_that_ran_is_recorded(tmp_path, monkeypatch):
    """`xa investigate` logged "claude" whatever actually ran, which made the log a guess."""
    result = _run_against(tmp_path, monkeypatch, ["echo", "x"])
    assert result.agent == "claude"

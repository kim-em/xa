"""Template rendering and escalation planning."""

import json
from pathlib import Path

import pytest

from xa.actions import ActionError, build_prompt, context_for, plan, resolve
from xa.config import Action, Config, MonitorSpec
from xa.template import render

ITEM = {
    "uid": "nt/ci",
    "monitor": "nt",
    "title": "branch is red",
    "detail": "6 sites in 3 modules",
    "since": "2026-08-25T00:46:25+00:00",
    "url": "https://example.invalid/run/1",
    "actions": ["fix"],
    "evidence": {
        "branch": "nightly-testing",
        "repo": "org/repo",
        "modules": ["A.B", "C.D"],
        "diagnostics": [
            {"file": "X.lean", "line": 1, "message": "boom", "context": ["one", "two"]},
            {"file": "Y.lean", "line": 2, "message": "bang", "context": []},
        ],
    },
}


# -- template ---------------------------------------------------------------

def test_dotted_lookup():
    assert render("{{evidence.branch}}", ITEM) == "nightly-testing"


def test_missing_values_render_empty_not_an_error():
    """A prompt with a typo should still produce a usable prompt."""
    assert render("[{{nope.nothing}}]", ITEM) == "[]"


def test_list_section_over_scalars():
    assert render("{{#evidence.modules}}\n- {{.}}\n{{/evidence.modules}}", ITEM) == "- A.B\n- C.D\n"


def test_list_section_over_dicts_and_nesting():
    out = render(
        "{{#evidence.diagnostics}}\n{{file}}:{{line}}\n{{#context}}\n  {{.}}\n{{/context}}\n{{/evidence.diagnostics}}",
        ITEM,
    )
    assert out == "X.lean:1\n  one\n  two\nY.lean:2\n"


def test_inverted_section_fires_on_empty():
    assert render("{{^evidence.diagnostics}}none{{/evidence.diagnostics}}", ITEM) == ""
    assert render("{{^missing}}none{{/missing}}", {"missing": []}) == "none"


def test_standalone_tags_do_not_leave_blank_lines():
    out = render("a\n{{#evidence.modules}}\nx\n{{/evidence.modules}}\nb", ITEM)
    assert out == "a\nx\nx\nb"


# -- context ----------------------------------------------------------------

def test_context_adds_a_human_age():
    ctx = context_for(ITEM)
    assert ctx["age"].endswith(("m", "h", "d", "s"))
    assert ctx["title"] == "branch is red"


# -- planning ---------------------------------------------------------------

def _cfg(tmp_path: Path, **action_kw) -> Config:
    (tmp_path / "prompts").mkdir(exist_ok=True)
    (tmp_path / "prompts" / "p.md").write_text("fix {{evidence.branch}} ({{age}})")
    action = Action(id="fix", label="Fix it", prompt="prompts/p.md",
                    cwd=str(tmp_path), target="{{evidence.branch}}", **action_kw)
    cfg = Config(root=tmp_path)
    cfg.monitors["nt"] = MonitorSpec(name="nt", actions={"fix": action})
    return cfg


def test_prompt_is_rendered_from_the_template(tmp_path):
    cfg = _cfg(tmp_path)
    prompt = build_prompt(ITEM, cfg.monitors["nt"].actions["fix"], cfg)
    assert prompt.startswith("fix nightly-testing (")


def test_prompt_falls_back_when_no_template_is_configured(tmp_path):
    cfg = _cfg(tmp_path)
    action = cfg.monitors["nt"].actions["fix"]
    action.prompt = None
    assert "branch is red" in build_prompt(ITEM, action, cfg)


def test_target_may_itself_be_a_template(tmp_path):
    cfg = _cfg(tmp_path)
    launch = plan(ITEM, cfg.monitors["nt"].actions["fix"], cfg, ensure=False)
    assert launch.command[-1] == "nightly-testing"


def test_agent_override_reaches_the_command(tmp_path):
    cfg = _cfg(tmp_path)
    launch = plan(ITEM, cfg.monitors["nt"].actions["fix"], cfg, agent="codex", ensure=False)
    assert "--codex" in launch.command
    assert launch.env["WT_AGENT"] == "codex"


def test_unknown_agent_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ActionError):
        plan(ITEM, cfg.monitors["nt"].actions["fix"], cfg, agent="hal9000", ensure=False)


def test_missing_checkout_without_a_repo_explains_itself(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.monitors["nt"].actions["fix"].cwd = str(tmp_path / "absent")
    with pytest.raises(ActionError, match="declares no `repo`"):
        plan(ITEM, cfg.monitors["nt"].actions["fix"], cfg)


def test_resolve_picks_the_only_offered_action(tmp_path):
    cfg = _cfg(tmp_path)
    assert resolve(ITEM, cfg, None).id == "fix"


def test_resolve_rejects_an_unknown_action(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ActionError, match="no action"):
        resolve(ITEM, cfg, "nope")


def test_resolve_requires_a_choice_when_several_are_offered(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.monitors["nt"].actions["other"] = Action(id="other", label="Other")
    item = dict(ITEM, actions=["fix", "other"])
    with pytest.raises(ActionError, match="several actions"):
        resolve(item, cfg, None)

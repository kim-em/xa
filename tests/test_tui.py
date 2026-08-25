"""TUI smoke tests, run headlessly.

The interesting failures here are not layout but content: evidence routinely
contains shell snippets and Lean terms full of brackets, which a markup-parsing
widget will choke on. That crash lands on exactly the items worth reading.
"""

import json

import pytest

pytest.importorskip("textual")

from xa.tui import XA  # noqa: E402


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    payload = {
        "version": 1,
        "generated_at": "2026-08-25T05:53:00+00:00",
        "count": 1,
        "monitors": [{"name": "m", "ok": True, "collected_at": "2026-08-25T05:53:00+00:00"}],
        "items": [
            {
                "uid": "m/hostile", "monitor": "m", "key": "hostile", "kind": "fault",
                "severity": "alert", "disposition": "active", "counts": True,
                "state_key": "s1", "title": "brackets everywhere", "detail": "",
                "since": "2026-08-25T00:00:00+00:00", "url": None, "cluster": None,
                "cluster_size": 1, "mode": "report", "plan": None, "metrics": {},
                "actions": [],
                # The shapes that broke the pane in practice.
                "evidence": {
                    "shell": 'tmp=$(mktemp); wget -qO "$tmp" http://x/y.sh; [ -f "$tmp" ]',
                    "lean": "∀ {β : Type u} [inst : LinearOrder β], f [a, b] ≤ x",
                    "markupish": "[bold]not markup[/bold] [nope]",
                },
            },
            {
                "uid": "m/quiet", "monitor": "m", "key": "quiet", "kind": "backlog",
                "severity": "info", "disposition": "snoozed", "counts": False,
                "state_key": "s2", "title": "a pile", "detail": "",
                "since": None, "url": None, "cluster": None, "cluster_size": 1,
                "mode": "report", "plan": None, "metrics": {"total": 218, "capped": True},
                "evidence": {}, "actions": [],
            },
        ],
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("XA_CACHE", str(tmp_path))
    return path


async def test_hostile_evidence_does_not_crash_the_detail_pane(snapshot):
    app = XA()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        table = app.query_one("#table")
        assert table.row_count == 1        # the snoozed row is hidden by default
        for _ in range(table.row_count):
            await pilot.press("down")
            await pilot.pause()
        assert app.current["uid"] == "m/hostile"


async def test_show_hidden_reveals_suppressed_items(snapshot):
    app = XA()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("A")
        await pilot.pause()
        assert app.query_one("#table").row_count == 2


async def test_snooze_opens_a_prompt_and_escape_cancels(snapshot):
    app = XA()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        await pilot.pause()
        assert type(app.screen).__name__ == "Ask"
        await pilot.press("escape")
        await pilot.pause()
        assert type(app.screen).__name__ != "Ask"


async def test_agent_toggle_cycles_and_returns_to_the_configured_default(snapshot):
    app = XA()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        seen = []
        for _ in range(3):
            await pilot.press("c")
            seen.append(app.agent)
        assert seen == ["claude", "codex", None]

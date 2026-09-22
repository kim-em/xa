from datetime import datetime, timezone

from xa import render


def test_row_wraps_with_a_hanging_indent(monkeypatch):
    monkeypatch.setattr(render, "width", lambda: 80)
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "severity": "info",
        "since": "2026-08-25T09:00:00+00:00",
        "uid": "bump-branches/overdue",
        "title": "nobody has created bump/nightly-2026-08-25",
        "detail": (
            "asked for on Tue 25 Aug; needs the branch and a PR into v4.35.0. "
            "Latest adaptation PR is for nightly-2026-08-19"
        ),
        "actions": ["create"],
    }
    now = datetime(2026, 8, 26, 19, tzinfo=timezone.utc)

    lines = render._row(item, now, len(item["uid"]))

    content_col = len(item["uid"]) + 13
    assert len(lines) > 3
    assert all(len(line) <= 80 for line in lines)
    assert all(line[:content_col].isspace() for line in lines[1:])
    assert lines[-1].rstrip().endswith("xa open bump-branches/overdue")


def test_row_surfaces_each_labeled_link(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "severity": "warn",
        "since": "2026-08-20T09:00:00+00:00",
        "uid": "bump-branches/stuck/290",
        "title": "PR needs review",
        "detail": "",
        "links": [
            {"label": "PR #290", "url": "https://github.example/pull/290"},
            {"label": "Zulip thread", "url": "https://zulip.example/near/123"},
        ],
        "actions": ["review"],
    }

    lines = render._row(item, datetime(2026, 8, 26, 19, tzinfo=timezone.utc), len(item["uid"]))
    output = "\n".join(lines)
    assert "↗ PR #290: https://github.example/pull/290" in output
    assert "↗ Zulip thread: https://zulip.example/near/123" in output


def test_in_progress_work_replaces_the_start_action_with_reopen(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "severity": "warn", "since": "2026-08-26T18:00:00+00:00",
        "uid": "build/red", "title": "the build is red", "detail": "",
        "actions": ["fix"], "action_labels": {"fix": "Fix it"},
        "work": {"fix": {
            "action": "fix", "agent": "claude", "status": "active",
            "started_at": "2026-08-26T18:30:00+00:00",
            "updated_at": "2026-08-26T18:30:00+00:00",
        }},
    }

    lines = render._row(item, datetime(2026, 8, 26, 19, tzinfo=timezone.utc), 12)
    output = "\n".join(lines)
    assert "[in progress: claude, 30m]" in output
    assert "Reopen the in-progress session: xa open build/red" in output
    assert "Fix it:" not in output


def test_each_action_can_have_an_in_progress_session(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "uid": "prs/mine",
        "actions": ["conflicts", "red"],
        "action_labels": {
            "conflicts": "Triage conflicts",
            "red": "Triage failing CI",
        },
        "work": {
            "conflicts": {"status": "active", "agent": "claude"},
            "red": {"status": "active", "agent": "claude"},
        },
    }

    assert render._action_lines(item) == [
        "Reopen the in-progress session: xa open prs/mine conflicts",
        "Reopen the in-progress session: xa open prs/mine red",
    ]


def test_finished_sessions_restore_the_ordinary_action(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "uid": "prs/mine",
        "actions": ["conflicts", "red"],
        "action_labels": {
            "conflicts": "Triage the merge conflicts",
            "red": "Triage the failing CI",
        },
        "work": {
            "conflicts": {"status": "finished", "agent": "claude"},
            "red": {"status": "active", "agent": "claude"},
        },
    }

    assert render._action_lines(item) == [
        "Triage the merge conflicts: xa open prs/mine conflicts",
        "Reopen the in-progress session: xa open prs/mine red",
    ]


def test_monitor_can_opt_in_to_a_labeled_why_action(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "uid": "toolchains/stale",
        "why_label": "Choose repositories to stop watching",
        "actions": ["bump"],
        "action_labels": {"bump": "Bump the stale toolchains"},
    }

    assert render._action_lines(item) == [
        "Choose repositories to stop watching: xa why toolchains/stale",
        "Bump the stale toolchains: xa open toolchains/stale",
    ]


def test_why_command_is_purple(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", True)
    item = {
        "uid": "toolchains/stale",
        "why_label": "Choose repositories to stop watching",
    }

    assert render._action_lines(item) == [
        "Choose repositories to stop watching: "
        "\033[35mxa why toolchains/stale\033[0m"
    ]


def test_direct_job_command_is_purple(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", True)
    item = {
        "uid": "backups/job",
        "commands": [{"label": "Retry safely", "command": "xa run backups"}],
    }

    assert render._action_lines(item) == [
        "Retry safely: \033[35mxa run backups\033[0m"
    ]


def test_addressable_cluster_lists_copyable_mute_commands(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "uid": "tools/stale", "title": "2 repositories are stale",
        "kind": "pending", "severity": "warn", "disposition": "active",
        "state_key": "summary", "cluster_key": "stale", "metrics": {},
        "evidence": {"cluster_members": [
            {"uid": "tools/stale/org/a", "title": "org/a — v1, 40d old"},
            {"uid": "tools/stale/org/b", "title": "org/b — v1, 50d old"},
        ]},
    }

    output = render.render_detail(item)

    assert "members (2)" in output
    assert "xa mute tools/stale/org/a" in output
    assert "xa mute tools/stale/org/b" in output
    assert "  evidence" not in output


def test_addressable_cluster_member_still_shows_its_evidence(monkeypatch):
    monkeypatch.setattr(render, "_COLOUR", False)
    item = {
        "uid": "tools/stale/org/a", "title": "org/a — v1, 40d old",
        "kind": "pending", "severity": "warn", "disposition": "active",
        "state_key": "member", "cluster_key": "stale", "metrics": {},
        "evidence": {"repo": "org/a", "toolchain": "v1"},
    }

    output = render.render_detail(item)

    assert "  evidence" in output
    assert '"repo": "org/a"' in output


def test_a_backlog_row_shows_its_links(monkeypatch):
    """A slice small enough to read is better shown than sent to a session.

    Backlog rows used to render their metrics and actions and drop `links` on
    the floor, so a monitor with three pull requests to name had nowhere to
    put them.
    """
    monkeypatch.setattr(render, "width", lambda: 100)
    monkeypatch.setattr(render, "_COLOUR", False)
    snapshot = {
        "generated_at": datetime(2026, 9, 22, tzinfo=timezone.utc).isoformat(),
        "count": 0,
        "monitors": [],
        "items": [{
            "uid": "prs/mine", "monitor": "prs", "key": "mine", "kind": "backlog",
            "title": "open pull requests I opened", "severity": "info",
            "disposition": "active", "counts": False, "since": None,
            "metrics": {"total": 2, "mine_to_merge": 2},
            "links": [
                {"label": "org/a#1", "url": "https://github.com/org/a/pull/1"},
                {"label": "org/b#2", "url": "https://github.com/org/b/pull/2"},
            ],
            "actions": [], "action_labels": {},
        }],
    }

    out = render.render(snapshot)

    assert "org/a#1: https://github.com/org/a/pull/1" in out
    assert "org/b#2: https://github.com/org/b/pull/2" in out
    assert "2 mine to merge" in out

"""Status items are context, distinct from work waiting in a backlog."""


def test_status_has_its_own_section_and_human_readable_rates():
    from xa.render import render

    snapshot = {
        "generated_at": "2026-08-25T05:53:00+00:00",
        "monitors": [],
        "items": [{
            "uid": "project/queue", "monitor": "project", "kind": "status",
            "severity": "info", "disposition": "active", "counts": False,
            "title": "Pull request queue", "metrics": {
                "open": 114,
                "merges/hr_over_6hrs_(4.54_over_7_days)": 7.33,
            },
        }],
    }

    out = render(snapshot)
    assert "STATUS" in out
    assert "BACKLOG" not in out
    assert "7.33 merges/hr over 6hrs (4.54 over 7 days)" in out

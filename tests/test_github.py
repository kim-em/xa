from datetime import datetime, timezone

from xa.monitors import github


def pr_with_activity(*, commit="2026-08-26T10:00:00Z", events=(), labels=()):
    """A pull request node in the shape `TIMELINE` returns."""
    return {
        "id": "PR_1",
        "url": "https://example.test/pull/1",
        "state": "OPEN",
        "labels": {"nodes": [{"name": name} for name in labels]},
        "commits": {"nodes": [{"commit": {
            "committedDate": commit, "pushedDate": None,
        }}]},
        "timelineItems": {"nodes": list(events)},
    }


def comment(author, at, body="Please adjust this", *, kind="User"):
    return {
        "__typename": "IssueComment",
        "author": {"login": author, "__typename": kind},
        "createdAt": at,
        "body": body,
        "url": f"https://example.test/comments/{author}/{at}",
    }


def review(author, at, body="", *, state="COMMENTED", kind="User"):
    return {
        "__typename": "PullRequestReview",
        "author": {"login": author, "__typename": kind},
        "createdAt": at,
        "body": body,
        "state": state,
        "url": f"https://example.test/reviews/{author}/{at}",
    }


def push(at):
    return {
        "__typename": "PullRequestCommit",
        "commit": {"committedDate": at, "pushedDate": at},
    }


def test_external_comment_after_latest_commit_needs_response():
    pr = pr_with_activity(events=[comment("reviewer", "2026-08-26T11:00:00Z")])

    status = github._response_status(pr, "kim-em")

    assert status is not None
    assert status["author"] == "reviewer"
    assert status["latest_own"] == "2026-08-26T10:00:00Z"


def test_own_later_comment_clears_the_response():
    pr = pr_with_activity(events=[
        comment("reviewer", "2026-08-26T11:00:00Z"),
        comment("kim-em", "2026-08-26T12:00:00Z", "Done"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_a_later_push_clears_the_response():
    pr = pr_with_activity(events=[
        comment("reviewer", "2026-08-26T11:00:00Z"),
        push("2026-08-26T13:00:00Z"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_bot_comments_do_not_request_attention():
    pr = pr_with_activity(events=[
        comment("ci-helper", "2026-08-26T11:00:00Z", kind="Bot"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_policy_can_ignore_automation_using_user_accounts():
    pr = pr_with_activity(events=[
        comment("release-helper", "2026-08-26T11:00:00Z", "<!--status--> all green"),
    ])

    assert github._response_status(
        pr,
        "kim-em",
        ignore_authors=frozenset({"release-helper"}),
    ) is None
    assert github._response_status(
        pr,
        "kim-em",
        ignore_body_prefixes=("<!--status-->",),
    ) is None


def test_a_review_with_no_body_still_counts_as_feedback():
    # Only inline notes were left, so the body is empty. The review carries the
    # timing, which is what the nested-connection shape used to be paid for.
    pr = pr_with_activity(events=[review("reviewer", "2026-08-26T11:00:00Z")])

    status = github._response_status(pr, "kim-em")

    assert status is not None
    assert status["kind"] == "review"
    assert status["excerpt"] == ""


def test_a_bare_approval_is_not_feedback_awaiting_a_reply():
    # An approval with nothing written on it says go ahead, and `approved` and
    # `ready` already report it. Counting it as feedback would leave every
    # approved pull request permanently owing an answer.
    pr = pr_with_activity(events=[
        review("reviewer", "2026-08-26T11:00:00Z", state="APPROVED"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_an_approval_someone_wrote_on_is_feedback():
    pr = pr_with_activity(events=[
        review("reviewer", "2026-08-26T11:00:00Z", "one nit before you land it",
               state="APPROVED"),
    ])

    assert github._response_status(pr, "kim-em") is not None


def test_a_bodiless_changes_requested_review_is_feedback():
    # The empty body means the notes were left inline.
    pr = pr_with_activity(events=[
        review("reviewer", "2026-08-26T11:00:00Z", state="CHANGES_REQUESTED"),
    ])

    assert github._response_status(pr, "kim-em") is not None


def test_a_dismissed_review_has_been_retracted():
    pr = pr_with_activity(events=[
        review("reviewer", "2026-08-26T11:00:00Z", "never mind", state="DISMISSED"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_a_bare_approval_still_counts_as_engagement(monkeypatch):
    # Nothing is owed, but somebody looked, and that is what the stall clock asks.
    pr = pr_with_activity(events=[
        review("reviewer", "2026-08-26T11:00:00Z", state="APPROVED"),
    ])
    _stub_nodes(monkeypatch, [dict(pr)])

    github.resolve_timeline([pr], "kim-em")

    assert pr["response_needed"] is False
    assert pr["last_external_at"] == "2026-08-26T11:00:00Z"


def test_a_pending_review_is_not_yet_addressed_to_anyone():
    pr = pr_with_activity(events=[
        review("reviewer", "2026-08-26T11:00:00Z", "draft thoughts", state="PENDING"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_labels_read_from_either_query_shape():
    assert github.label_names(pr_with_activity(labels=["awaiting-author"])) == {
        "awaiting-author",
    }
    assert github.label_names({}) == frozenset()


def test_changes_requested_metric_uses_response_activity():
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    common = {
        "isDraft": False,
        "updatedAt": "2026-08-26T00:00:00Z",
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "commits": {"nodes": []},
    }
    prs = [
        {**common, "response_needed": True},
        {**common, "response_needed": False, "reviewDecision": "CHANGES_REQUESTED"},
    ]

    assert github.metrics_for(prs, now)["changes_requested"] == 1


def test_response_candidates_omit_only_configured_repositories():
    prs = [
        {"repository": {"nameWithOwner": "TauCetiProject/TauCeti"}},
        {"repository": {"nameWithOwner": "TauCetiProject/other"}},
        {"repository": {"nameWithOwner": "kim-em/hex-dev"}},
        {"repository": {"nameWithOwner": "leanprover-community/mathlib4"}},
    ]

    candidates = github._response_candidates(
        prs,
        frozenset({"taucetiproject/tauceti/", "KIM-EM/HEX-DEV"}),
    )

    assert candidates == [prs[1], prs[3]]


def test_repository_distribution_counts_only_the_given_slice():
    prs = [
        {"repository": {"nameWithOwner": "org/conflicted"}},
        {"repository": {"nameWithOwner": "org/other"}},
        {"repository": {"nameWithOwner": "org/conflicted"}},
    ]

    assert github._repo_distribution([prs[0], prs[2]]) == [
        {"repo": "org/conflicted", "count": 2},
    ]


def _stub_nodes(monkeypatch, nodes):
    import json as _json

    monkeypatch.setattr(
        github, "_gh",
        lambda args, retries=4: _json.dumps({"data": {"nodes": nodes}}),
    )


def test_closed_pull_request_from_a_stale_search_index_never_needs_a_response(monkeypatch):
    # `is:open` is applied by GitHub's search index, which lags state changes, so a
    # pull request closed minutes earlier still comes back as a hit. The per-node
    # read is current, and is where that gets noticed.
    pr = pr_with_activity(events=[comment("reviewer", "2026-08-26T11:00:00Z")])
    _stub_nodes(monkeypatch, [{**pr, "state": "CLOSED"}])

    github.resolve_timeline([pr], "kim-em")

    assert pr["state"] == "CLOSED"
    assert pr["response_needed"] is False
    assert pr["last_external_at"] is None


def test_open_pull_request_still_reports_its_response_activity(monkeypatch):
    pr = pr_with_activity(events=[comment("reviewer", "2026-08-26T11:00:00Z")])
    _stub_nodes(monkeypatch, [dict(pr)])

    github.resolve_timeline([pr], "kim-em")

    assert pr["response_needed"] is True
    assert pr["response_activity"]["author"] == "reviewer"


def test_engagement_is_recorded_even_when_nothing_awaits_a_response(monkeypatch):
    # The actor answered, so no response is owed. When a human last engaged is
    # a separate fact, and the only one that says whether this is stuck.
    pr = pr_with_activity(events=[
        comment("reviewer", "2026-08-26T11:00:00Z"),
        comment("kim-em", "2026-08-26T12:00:00Z", "Done"),
    ])
    _stub_nodes(monkeypatch, [dict(pr)])

    github.resolve_timeline([pr], "kim-em")

    assert pr["response_needed"] is False
    assert pr["last_external_at"] == "2026-08-26T11:00:00Z"
    assert pr["last_external_author"] == "reviewer"


def test_a_pull_request_nobody_has_touched_has_no_engagement(monkeypatch):
    pr = pr_with_activity(events=[comment("kim-em", "2026-08-26T12:00:00Z", "ping")])
    _stub_nodes(monkeypatch, [dict(pr)])

    github.resolve_timeline([pr], "kim-em")

    assert pr["last_external_at"] is None


def test_labels_survive_the_per_node_read(monkeypatch):
    pr = pr_with_activity()
    _stub_nodes(monkeypatch, [{**pr, "labels": {"nodes": [{"name": "delegated"}]}}])

    github.resolve_timeline([pr], "kim-em")

    assert github.label_names(pr) == {"delegated"}


def test_a_fresh_read_does_not_undo_a_resolved_mergeable(monkeypatch):
    # `resolve_mergeable` waits for GitHub to compute mergeability; a later read
    # answers UNKNOWN again, and must not overwrite the answer that was waited for.
    pr = {**pr_with_activity(), "mergeable": "CONFLICTING"}
    _stub_nodes(monkeypatch, [{**pr, "mergeable": "UNKNOWN"}])

    github.resolve_timeline([pr], "kim-em")

    assert pr["mergeable"] == "CONFLICTING"


def test_inline_comment_text_is_bought_back_for_a_bodiless_review(monkeypatch):
    pr = pr_with_activity(events=[review("reviewer", "2026-08-26T11:00:00Z")])
    pr["response_activity"] = github._response_status(pr, "kim-em")
    pr["response_needed"] = True
    assert pr["response_activity"]["excerpt"] == ""

    _stub_nodes(monkeypatch, [{
        "id": "PR_1",
        "reviewThreads": {"nodes": [{"comments": {"nodes": [{
            "author": {"login": "reviewer", "__typename": "User"},
            "createdAt": "2026-08-26T11:00:00Z",
            "body": "Rename this argument",
            "url": "https://example.test/thread/1",
        }]}}]},
    }])

    github.resolve_review_comment_bodies([pr], "kim-em")

    assert pr["response_activity"]["kind"] == "review comment"
    assert pr["response_activity"]["excerpt"] == "Rename this argument"
    assert pr["response_activity"]["url"] == "https://example.test/thread/1"

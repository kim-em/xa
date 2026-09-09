from datetime import datetime, timezone

from xa.monitors import github


def pr_with_activity(*, commit="2026-08-26T10:00:00Z", comments=(),
                     reviews=(), threads=()):
    return {
        "id": "PR_1",
        "url": "https://example.test/pull/1",
        "commits": {"nodes": [{"commit": {
            "committedDate": commit, "pushedDate": None,
        }}]},
        "comments": {"nodes": list(comments)},
        "reviews": {"nodes": list(reviews)},
        "reviewThreads": {"nodes": [
            {"comments": {"nodes": list(nodes)}} for nodes in threads
        ]},
    }


def comment(author, at, body="Please adjust this", *, kind="User"):
    return {
        "author": {"login": author, "__typename": kind},
        "createdAt": at,
        "body": body,
        "url": f"https://example.test/comments/{author}/{at}",
    }


def test_external_comment_after_latest_commit_needs_response():
    pr = pr_with_activity(comments=[comment("reviewer", "2026-08-26T11:00:00Z")])

    status = github._response_status(pr, "kim-em")

    assert status is not None
    assert status["author"] == "reviewer"
    assert status["latest_own"] == "2026-08-26T10:00:00Z"


def test_own_later_comment_clears_the_response():
    pr = pr_with_activity(comments=[
        comment("reviewer", "2026-08-26T11:00:00Z"),
        comment("kim-em", "2026-08-26T12:00:00Z", "Done"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_bot_comments_do_not_request_attention():
    pr = pr_with_activity(comments=[
        comment("ci-helper", "2026-08-26T11:00:00Z", kind="Bot"),
    ])

    assert github._response_status(pr, "kim-em") is None


def test_policy_can_ignore_automation_using_user_accounts():
    pr = pr_with_activity(comments=[
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


def test_inline_review_comment_counts_as_feedback():
    pr = pr_with_activity(threads=[[
        comment("reviewer", "2026-08-26T11:00:00Z", "Rename this argument"),
    ]])

    status = github._response_status(pr, "kim-em")

    assert status is not None
    assert status["kind"] == "review comment"
    assert status["excerpt"] == "Rename this argument"


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

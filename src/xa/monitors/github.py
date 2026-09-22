"""The `github-search` monitor kind.

A large fraction of "what needs me" is a GitHub search plus arithmetic, so that
case is config-only. Anything needing real parsing writes an executable
instead; this deliberately has no expression language, because a config file
full of embedded predicates is worse to debug than thirty lines of Python.

The metrics are a fixed vocabulary. Config chooses which to surface and which
slices deserve their own row.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from typing import Any

from ..config import Config, MonitorSpec
from ..model import MonitorReport, Observation, parse_ts, utcnow
from .lib import TRANSIENT, digest

QUERY = """
query($q:String!, $after:String) {
  search(query:$q, type:ISSUE, first:50, after:$after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      id number title url isDraft createdAt updatedAt mergeable reviewDecision
      repository { nameWithOwner viewerPermission }
      author { login }
      labels(first:30) { nodes { name } }
      commits(last:1) { nodes { commit { statusCheckRollup { state } } } }
    } }
  }
}
"""

MERGEABLE_RECHECK = """
query($ids:[ID!]!) { nodes(ids:$ids) { ... on PullRequest { id mergeable } } }
"""

# One request per batch for everything that needs a per-node read: current
# state, labels, and the timing of every human event.
#
# The shape matters more than the field list. GitHub's GraphQL budget charges
# for a connection nested inside a connection, and almost nothing else: the
# `reviewThreads { comments }` this replaced cost 0.55 points per pull request,
# where this costs 0.025, measured against the 5000/hour limit. The `last:`
# argument makes no difference to the price at all, so there is no reason to
# economise on the window.
#
# The price is paid in inline review-comment text, which lives only under that
# nested connection. A review with inline comments and no body still arrives as
# a PULL_REQUEST_REVIEW event, so who engaged and when both survive; only the
# quotable excerpt is missing, and `resolve_review_comment_bodies` fetches that
# for the handful of pull requests where it is actually going to be read.
TIMELINE = """
query($ids:[ID!]!) {
  nodes(ids:$ids) { ... on PullRequest {
    id state isDraft mergeable reviewDecision updatedAt
    labels(first:30) { nodes { name } }
    commits(last:1) { nodes { commit {
      committedDate pushedDate statusCheckRollup { state }
    } } }
    timelineItems(
      last:100,
      itemTypes:[ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT, LABELED_EVENT]
    ) { nodes {
      __typename
      ... on IssueComment { createdAt url body author { login __typename } }
      ... on PullRequestReview { createdAt url body state author { login __typename } }
      ... on PullRequestCommit { commit { committedDate pushedDate } }
      ... on LabeledEvent { createdAt label { name } }
    } }
  } }
}
"""

# Inline review-comment bodies, for the few pull requests whose latest human
# event turns out to have been an inline comment with no review body.
REVIEW_COMMENT_BODIES = """
query($ids:[ID!]!) {
  nodes(ids:$ids) { ... on PullRequest {
    id
    reviewThreads(last:20) { nodes {
      comments(first:5) { nodes { author { login __typename } createdAt body url } }
    } }
  } }
}
"""


def _gh(args: list[str], retries: int = 4) -> str:
    import time

    last = ""
    for attempt in range(retries):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
        if proc.returncode == 0:
            return proc.stdout
        last = (proc.stderr or proc.stdout or "").strip()
        if not any(t in last for t in TRANSIENT):
            break
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gh {' '.join(args[:2])}: {last[:300]}")


def search(query: str, pages: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(pages):
        args = ["api", "graphql", "-f", f"query={QUERY}", "-F", f"q={query}"]
        if after:
            args += ["-F", f"after={after}"]
        data = json.loads(_gh(args))["data"]["search"]
        out += data["nodes"]
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return out


def resolve_mergeable(prs: list[dict[str, Any]], batch: int = 80, waits=(3, 8)) -> None:
    """Fill in `mergeable` for pull requests GitHub has not computed yet.

    GitHub computes mergeability lazily: asking is what schedules the work, and
    the first answer is often UNKNOWN. A practice run saw 125 of 222 come back
    that way. Asking again after a pause resolves most of them, which is fine
    during collection and impossible on a read path.
    """
    import time

    for wait in waits:
        pending = [p for p in prs if p.get("mergeable") == "UNKNOWN"]
        if not pending:
            return
        time.sleep(wait)
        by_id = {p["id"]: p for p in pending}
        ids = list(by_id)
        for i in range(0, len(ids), batch):
            chunk = ids[i : i + batch]
            args = ["api", "graphql", "-f", f"query={MERGEABLE_RECHECK}"]
            for node_id in chunk:
                args += ["-f", f"ids[]={node_id}"]
            try:
                nodes = json.loads(_gh(args))["data"]["nodes"]
            except (RuntimeError, KeyError, TypeError):
                return  # Leave them UNKNOWN rather than failing the whole monitor.
            for node in nodes or []:
                if node and node.get("id") in by_id:
                    by_id[node["id"]]["mergeable"] = node.get("mergeable", "UNKNOWN")


def ci_state(pr: dict[str, Any]) -> str | None:
    nodes = (pr.get("commits") or {}).get("nodes") or []
    if not nodes:
        return None
    rollup = nodes[0]["commit"].get("statusCheckRollup")
    return rollup.get("state") if rollup else None


def _is_bot(author: dict[str, Any] | None) -> bool:
    if not author:
        return True
    login = str(author.get("login") or "")
    return author.get("__typename") == "Bot" or login.endswith("[bot]")


def viewer_permission(pr: dict[str, Any]) -> str:
    """What the authenticated user may do in the pull request's repository.

    One of ADMIN, MAINTAIN, WRITE, TRIAGE, READ, or an empty string when the
    field was not asked for. Approving a pull request and being able to merge
    it are different things, and only this says which.
    """
    return str((pr.get("repository") or {}).get("viewerPermission") or "")


def label_names(pr: dict[str, Any]) -> frozenset[str]:
    """The labels currently on a pull request, from either query shape."""
    return frozenset(
        str(node["name"])
        for node in (pr.get("labels") or {}).get("nodes") or []
        if node and node.get("name")
    )


def _timeline_events(node: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Human events on a pull request, and the actor-neutral latest push time.

    A review is an event whether or not it has a body. The old shape read
    review bodies and inline comments separately and skipped bodiless reviews,
    which meant a reviewer who left only inline notes registered through their
    comments; here the review itself carries the timing, so an inline-only
    review still counts and no nested connection has to be paid for.

    GitHub does not reliably expose when a commit was pushed to a branch.
    `pushedDate` is used where present, with `committedDate` as the closest
    stable proxy.
    """
    events: list[dict[str, Any]] = []
    pushes: list[str] = []
    for item in (node.get("timelineItems") or {}).get("nodes") or []:
        kind = item.get("__typename")
        if kind == "IssueComment":
            events.append({
                "at": item.get("createdAt"), "author": item.get("author"),
                "body": item.get("body") or "", "url": item.get("url"),
                "kind": "comment",
            })
        elif kind == "PullRequestReview":
            # A review still being drafted is not yet addressed to anyone, and
            # one that was dismissed has been retracted.
            if item.get("state") in ("PENDING", "DISMISSED"):
                continue
            events.append({
                "at": item.get("createdAt"), "author": item.get("author"),
                "body": item.get("body") or "", "url": item.get("url"),
                "kind": "review", "state": item.get("state"),
            })
        elif kind == "PullRequestCommit":
            commit = item.get("commit") or {}
            at = commit.get("pushedDate") or commit.get("committedDate")
            if at:
                pushes.append(at)

    head = ((node.get("commits") or {}).get("nodes") or [{}])[-1].get("commit") or {}
    at = head.get("pushedDate") or head.get("committedDate")
    if at:
        pushes.append(at)
    return [event for event in events if event.get("at")], max(pushes, default="")


def _external(events: list[dict[str, Any]], actor: str, *,
              ignore_authors: frozenset[str],
              ignore_body_prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
    """Events from a human who is neither the actor nor ignored by policy."""
    return [
        event
        for event in events
        if not _is_bot(event.get("author"))
        and (event.get("author") or {}).get("login") != actor
        and (event.get("author") or {}).get("login") not in ignore_authors
        and not str(event.get("body") or "").lstrip().startswith(ignore_body_prefixes)
    ]


def _is_feedback(event: dict[str, Any]) -> bool:
    """Whether an event asks the author for anything.

    A bare approval with no body is a green light, not a question: it is
    reported by the `approved` and `ready` metrics, and treating it as feedback
    would leave every approved pull request permanently owing a reply. An
    approval whose author took the trouble to write something is feedback, and
    so is a bodiless review at any other state, because the empty body means
    the notes were left inline.
    """
    return not (
        event.get("kind") == "review"
        and event.get("state") == "APPROVED"
        and not str(event.get("body") or "").strip()
    )


def _response_status(pr: dict[str, Any], actor: str, *,
                     ignore_authors: frozenset[str] = frozenset(),
                     ignore_body_prefixes: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Latest human event after the actor's own latest comment or push."""
    events, latest_push = _timeline_events(pr)
    own = [event["at"] for event in events
           if (event.get("author") or {}).get("login") == actor]
    latest_own = max([latest_push, *own], default="")
    external = [
        event
        for event in _external(events, actor, ignore_authors=ignore_authors,
                               ignore_body_prefixes=ignore_body_prefixes)
        if _is_feedback(event)
    ]
    if not external:
        return None
    latest = max(external, key=lambda event: event["at"])
    if latest["at"] <= latest_own:
        return None
    return {
        "author": (latest.get("author") or {}).get("login") or "unknown",
        "at": latest["at"],
        "url": latest.get("url") or pr.get("url"),
        "kind": latest["kind"],
        "excerpt": " ".join(str(latest.get("body") or "").split())[:300],
        "latest_own": latest_own,
    }


def resolve_timeline(prs: list[dict[str, Any]], actor: str, *,
                     ignore_authors: frozenset[str] = frozenset(),
                     ignore_body_prefixes: tuple[str, ...] = (),
                     batch: int = 40) -> None:
    """Attach current state, labels, and human-engagement timing to each PR.

    The `is:open` in a search query is applied by GitHub's search index, which
    lags state changes by minutes to tens of minutes; a pull request closed just
    before a collection still comes back as a hit. These per-node reads are not
    served from that index, so they are the cheap place to notice. A closed pull
    request is recorded as such and never needs a response.

    Two things are attached that a search cannot answer. `response_activity` is
    feedback awaiting the actor, and `last_external_at` is when any human other
    than the actor last engaged at all, which is a different question: a pull
    request nobody has touched for a month has nothing awaiting a response and
    is still stuck. `updatedAt` cannot stand in for the second, because the
    actor's own pushes reset it.
    """
    by_id = {pr["id"]: pr for pr in prs}
    ids = list(by_id)
    for i in range(0, len(ids), batch):
        args = ["api", "graphql", "-f", f"query={TIMELINE}"]
        for node_id in ids[i : i + batch]:
            args += ["-f", f"ids[]={node_id}"]
        nodes = json.loads(_gh(args))["data"]["nodes"]
        for node in nodes or []:
            if not node or node.get("id") not in by_id:
                continue
            pr = by_id[node["id"]]
            pr["state"] = node.get("state")
            if node.get("labels"):
                pr["labels"] = node["labels"]
            for field_name in ("isDraft", "reviewDecision", "updatedAt", "commits"):
                if node.get(field_name) is not None:
                    pr[field_name] = node[field_name]
            # `resolve_mergeable` waits for GitHub to compute this; do not undo
            # that work with the UNKNOWN a fresh read comes back with.
            if node.get("mergeable") and node["mergeable"] != "UNKNOWN":
                pr["mergeable"] = node["mergeable"]

            if node.get("state") != "OPEN":
                pr["response_needed"] = False
                pr["response_activity"] = None
                pr["last_external_at"] = None
                continue

            events, latest_push = _timeline_events(node)
            external = _external(events, actor, ignore_authors=ignore_authors,
                                 ignore_body_prefixes=ignore_body_prefixes)
            latest = max(external, key=lambda event: event["at"], default=None)
            status = _response_status(
                node, actor,
                ignore_authors=ignore_authors,
                ignore_body_prefixes=ignore_body_prefixes,
            )
            pr["response_activity"] = status
            pr["response_needed"] = status is not None
            pr["last_external_at"] = latest["at"] if latest else None
            pr["last_external_author"] = (
                (latest.get("author") or {}).get("login") if latest else None
            )
            pr["latest_own_at"] = latest_push or None


def resolve_response_activity(prs: list[dict[str, Any]], actor: str, *,
                              ignore_authors: frozenset[str] = frozenset(),
                              ignore_body_prefixes: tuple[str, ...] = (),
                              batch: int = 40) -> None:
    """Attach whether each PR has newer human feedback awaiting the actor."""
    resolve_timeline(prs, actor, ignore_authors=ignore_authors,
                     ignore_body_prefixes=ignore_body_prefixes, batch=batch)


def resolve_review_comment_bodies(prs: list[dict[str, Any]], actor: str, *,
                                  ignore_authors: frozenset[str] = frozenset(),
                                  ignore_body_prefixes: tuple[str, ...] = (),
                                  batch: int = 20) -> None:
    """Recover inline review-comment text where the excerpt came back empty.

    `TIMELINE` deliberately omits the one nested connection GitHub charges for,
    which is also the only place inline review-comment bodies live. A reviewer
    who left inline notes and no summary therefore registers with the right
    author and timestamp but nothing quotable. This buys the text back for
    exactly those pull requests, which is a handful rather than all of them.
    """
    wanted = {
        pr["id"]: pr
        for pr in prs
        if (pr.get("response_activity") or {}).get("kind") == "review"
        and not (pr.get("response_activity") or {}).get("excerpt")
    }
    ids = list(wanted)
    for i in range(0, len(ids), batch):
        args = ["api", "graphql", "-f", f"query={REVIEW_COMMENT_BODIES}"]
        for node_id in ids[i : i + batch]:
            args += ["-f", f"ids[]={node_id}"]
        try:
            nodes = json.loads(_gh(args))["data"]["nodes"]
        except (RuntimeError, KeyError, TypeError):
            return  # An excerpt is a nicety; never fail a collection for one.
        for node in nodes or []:
            if not node or node.get("id") not in wanted:
                continue
            pr = wanted[node["id"]]
            status = pr["response_activity"]
            comments = [
                {**comment, "at": comment.get("createdAt")}
                for thread in (node.get("reviewThreads") or {}).get("nodes") or []
                for comment in (thread.get("comments") or {}).get("nodes") or []
                if comment.get("createdAt") and comment.get("body")
            ]
            external = _external(comments, actor, ignore_authors=ignore_authors,
                                 ignore_body_prefixes=ignore_body_prefixes)
            fresh = [c for c in external if c["at"] > str(status.get("latest_own") or "")]
            if not fresh:
                continue
            latest = max(fresh, key=lambda comment: comment["at"])
            status["kind"] = "review comment"
            status["excerpt"] = " ".join(str(latest.get("body") or "").split())[:300]
            status["url"] = latest.get("url") or status.get("url")


def _needs_response(pr: dict[str, Any]) -> bool:
    return bool(pr.get("response_needed"))


def repo_distribution(prs: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for pr in prs:
        repo = pr["repository"]["nameWithOwner"]
        counts[repo] = counts.get(repo, 0) + 1
    # A list, not a mapping: prompt templates iterate, and a bare dict renders
    # as nothing at all in a section.
    return [
        {"repo": repo, "count": count}
        for repo, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    ]


def response_candidates(
    prs: list[dict[str, Any]], ignored_repositories: frozenset[str]
) -> list[dict[str, Any]]:
    """PRs whose discussion activity should be checked for a response."""
    ignored = {name.rstrip("/").casefold() for name in ignored_repositories}
    return [
        pr
        for pr in prs
        if str((pr.get("repository") or {}).get("nameWithOwner") or "")
        .rstrip("/")
        .casefold()
        not in ignored
    ]


def metrics_for(prs: list[dict[str, Any]], now) -> dict[str, int]:
    """The fixed vocabulary of pull-request metrics."""
    stale_cutoff = now - timedelta(days=30)

    def age_ok(pr):
        updated = parse_ts(pr.get("updatedAt"))
        return updated is not None and updated < stale_cutoff

    green = [p for p in prs if ci_state(p) == "SUCCESS" and not p["isDraft"]]
    return {
        "total": len(prs),
        "draft": sum(1 for p in prs if p["isDraft"]),
        "green": len(green),
        "red": sum(1 for p in prs if ci_state(p) == "FAILURE"),
        "pending_ci": sum(1 for p in prs if ci_state(p) == "PENDING"),
        "conflicts": sum(1 for p in prs if p.get("mergeable") == "CONFLICTING"),
        "changes_requested": sum(1 for p in prs if _needs_response(p)),
        "approved": sum(1 for p in prs if p.get("reviewDecision") == "APPROVED"),
        "stale_30d": sum(1 for p in prs if age_ok(p) and not p["isDraft"]),
        "ready": sum(
            1
            for p in prs
            if p.get("reviewDecision") == "APPROVED"
            and p.get("mergeable") == "MERGEABLE"
            and ci_state(p) == "SUCCESS"
            and not p["isDraft"]
        ),
    }


# Slices worth naming, mapping a metric to the PRs behind it.
SLICES = {
    "conflicts": lambda p: p.get("mergeable") == "CONFLICTING",
    "red": lambda p: ci_state(p) == "FAILURE",
    "changes_requested": _needs_response,
    "ready": lambda p: (
        p.get("reviewDecision") == "APPROVED"
        and p.get("mergeable") == "MERGEABLE"
        and ci_state(p) == "SUCCESS"
        and not p["isDraft"]
    ),
}


def run_github_search(spec: MonitorSpec, cfg: Config) -> MonitorReport:
    now = utcnow()
    raw = spec.raw
    query = raw.get("query")
    if not query:
        raise ValueError(f"monitor {spec.name!r} is kind=github-search but has no `query`")

    prs = search(query, pages=int(raw.get("pages", 20)))
    resolve_mergeable(prs)
    if "changes_requested" in (raw.get("metrics") or []):
        actor = str(raw.get("response_actor") or "").strip()
        if not actor:
            raise ValueError(
                f"monitor {spec.name!r} surfaces changes_requested but has no response_actor"
            )
        response_prs = response_candidates(
            prs,
            frozenset(
                str(value)
                for value in raw.get("response_ignore_repositories") or []
            ),
        )
        ignore_authors = frozenset(
            str(value) for value in raw.get("response_ignore_authors") or []
        )
        ignore_prefixes = tuple(
            str(value) for value in raw.get("response_ignore_body_prefixes") or []
        )
        resolve_response_activity(
            response_prs,
            actor,
            ignore_authors=ignore_authors,
            ignore_body_prefixes=ignore_prefixes,
        )
        resolve_review_comment_bodies(
            [pr for pr in response_prs if pr.get("response_needed")],
            actor,
            ignore_authors=ignore_authors,
            ignore_body_prefixes=ignore_prefixes,
        )
    m = metrics_for(prs, now)

    title = raw.get("title") or f"{m['total']} open pull requests"
    shown = raw.get("metrics") or ["total", "conflicts", "red", "stale_30d", "ready"]
    surfaced = {k: m[k] for k in shown if k in m}

    report = MonitorReport(monitor=spec.name, collected_at=now)

    # The oldest few in each named slice, so the row can be acted on without a
    # second round trip.
    evidence: dict[str, Any] = {"query": query}
    for name, predicate in SLICES.items():
        limits = raw.get("evidence_limits") or {}
        limit = int(limits.get(name, 20))
        matching = [p for p in prs if predicate(p)]
        picked = sorted(
            matching,
            key=lambda p: p.get("updatedAt") or "",
        )[:limit]
        if picked:
            evidence[name] = [
                {
                    "repo": p["repository"]["nameWithOwner"],
                    "number": p["number"],
                    "title": p["title"][:90],
                    "url": p["url"],
                    "updated": (p.get("updatedAt") or "")[:10],
                    **(
                        {
                            "comment_author": p["response_activity"]["author"],
                            "comment_kind": p["response_activity"]["kind"],
                            "commented": p["response_activity"]["at"],
                            "comment_url": p["response_activity"]["url"],
                            "comment_excerpt": p["response_activity"]["excerpt"],
                            "latest_own_activity": p["response_activity"]["latest_own"],
                        }
                        if p.get("response_activity") else {}
                    ),
                }
                for p in picked
            ]
            evidence[f"{name}_by_repo"] = repo_distribution(matching)

    report.observations.append(
        Observation(
            key=raw.get("key", "queue"),
            title=title,
            kind=raw.get("emit", "backlog"),  # type: ignore[arg-type]
            # Backlogs move constantly. Keying on the exact count would make
            # every acknowledgement lapse within the hour, so key on the query.
            state_key=digest(query),
            detail=raw.get("detail", ""),
            metrics=surfaced,
            evidence=evidence,
            actions=list(raw.get("offer") or []),
        )
    )
    return report

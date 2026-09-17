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
      repository { nameWithOwner }
      author { login }
      commits(last:1) { nodes { commit { statusCheckRollup { state } } } }
    } }
  }
}
"""

MERGEABLE_RECHECK = """
query($ids:[ID!]!) { nodes(ids:$ids) { ... on PullRequest { id mergeable } } }
"""

RESPONSE_ACTIVITY = """
query($ids:[ID!]!) {
  nodes(ids:$ids) { ... on PullRequest {
    id state
    commits(last:1) { nodes { commit { committedDate pushedDate } } }
    comments(last:50) { nodes { author { login __typename } createdAt body url } }
    reviews(last:50) { nodes { author { login __typename } submittedAt body url } }
    reviewThreads(last:50) { nodes {
      comments(last:50) { nodes { author { login __typename } createdAt body url } }
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


def _ci(pr: dict[str, Any]) -> str | None:
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


def _response_status(pr: dict[str, Any], actor: str, *,
                     ignore_authors: frozenset[str] = frozenset(),
                     ignore_body_prefixes: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Latest human comment after the author's latest comment or head commit.

    GitHub does not reliably expose the time an ordinary commit was pushed to a
    PR branch. `pushedDate` is used when available, with `committedDate` as the
    closest stable proxy. Issue comments, review bodies, and inline review
    comments all count; empty reviews and bot noise do not.
    """
    events: list[dict[str, Any]] = []
    for node in (pr.get("comments") or {}).get("nodes") or []:
        if node.get("body"):
            events.append({**node, "at": node.get("createdAt"), "kind": "comment"})
    for node in (pr.get("reviews") or {}).get("nodes") or []:
        if str(node.get("body") or "").strip():
            events.append({**node, "at": node.get("submittedAt"), "kind": "review"})
    for thread in (pr.get("reviewThreads") or {}).get("nodes") or []:
        for node in (thread.get("comments") or {}).get("nodes") or []:
            if node.get("body"):
                events.append({**node, "at": node.get("createdAt"), "kind": "review comment"})

    commits = (pr.get("commits") or {}).get("nodes") or []
    commit = commits[-1].get("commit") if commits else None
    own_times = [
        at
        for at in [
            (commit or {}).get("pushedDate") or (commit or {}).get("committedDate"),
            *(event.get("at") for event in events
              if (event.get("author") or {}).get("login") == actor),
        ]
        if at
    ]
    latest_own = max(own_times, default="")
    external = [
        event
        for event in events
        if not _is_bot(event.get("author"))
        and (event.get("author") or {}).get("login") != actor
        and (event.get("author") or {}).get("login") not in ignore_authors
        and not str(event.get("body") or "").lstrip().startswith(ignore_body_prefixes)
        and event.get("at")
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


def resolve_response_activity(prs: list[dict[str, Any]], actor: str, *,
                              ignore_authors: frozenset[str] = frozenset(),
                              ignore_body_prefixes: tuple[str, ...] = (),
                              batch: int = 20) -> None:
    """Attach whether each PR has newer human feedback awaiting the actor.

    The `is:open` in the search query is applied by GitHub's search index, which
    lags state changes by minutes to tens of minutes; a pull request closed just
    before a collection still comes back as a hit. These per-node reads are not
    served from that index, so they are the cheap place to notice. A closed pull
    request is recorded as such and never needs a response.
    """
    by_id = {pr["id"]: pr for pr in prs}
    ids = list(by_id)
    for i in range(0, len(ids), batch):
        args = ["api", "graphql", "-f", f"query={RESPONSE_ACTIVITY}"]
        for node_id in ids[i : i + batch]:
            args += ["-f", f"ids[]={node_id}"]
        nodes = json.loads(_gh(args))["data"]["nodes"]
        for node in nodes or []:
            if node and node.get("id") in by_id:
                if node.get("state") != "OPEN":
                    by_id[node["id"]]["state"] = node.get("state")
                    by_id[node["id"]]["response_needed"] = False
                    continue
                status = _response_status(
                    node,
                    actor,
                    ignore_authors=ignore_authors,
                    ignore_body_prefixes=ignore_body_prefixes,
                )
                by_id[node["id"]]["response_activity"] = status
                by_id[node["id"]]["response_needed"] = status is not None


def _needs_response(pr: dict[str, Any]) -> bool:
    return bool(pr.get("response_needed"))


def _repo_distribution(prs: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
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


def _response_candidates(
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

    green = [p for p in prs if _ci(p) == "SUCCESS" and not p["isDraft"]]
    return {
        "total": len(prs),
        "draft": sum(1 for p in prs if p["isDraft"]),
        "green": len(green),
        "red": sum(1 for p in prs if _ci(p) == "FAILURE"),
        "pending_ci": sum(1 for p in prs if _ci(p) == "PENDING"),
        "conflicts": sum(1 for p in prs if p.get("mergeable") == "CONFLICTING"),
        "changes_requested": sum(1 for p in prs if _needs_response(p)),
        "approved": sum(1 for p in prs if p.get("reviewDecision") == "APPROVED"),
        "stale_30d": sum(1 for p in prs if age_ok(p) and not p["isDraft"]),
        "ready": sum(
            1
            for p in prs
            if p.get("reviewDecision") == "APPROVED"
            and p.get("mergeable") == "MERGEABLE"
            and _ci(p) == "SUCCESS"
            and not p["isDraft"]
        ),
    }


# Slices worth naming, mapping a metric to the PRs behind it.
SLICES = {
    "conflicts": lambda p: p.get("mergeable") == "CONFLICTING",
    "red": lambda p: _ci(p) == "FAILURE",
    "changes_requested": _needs_response,
    "ready": lambda p: (
        p.get("reviewDecision") == "APPROVED"
        and p.get("mergeable") == "MERGEABLE"
        and _ci(p) == "SUCCESS"
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
        response_prs = _response_candidates(
            prs,
            frozenset(
                str(value)
                for value in raw.get("response_ignore_repositories") or []
            ),
        )
        resolve_response_activity(
            response_prs,
            actor,
            ignore_authors=frozenset(
                str(value) for value in raw.get("response_ignore_authors") or []
            ),
            ignore_body_prefixes=tuple(
                str(value) for value in raw.get("response_ignore_body_prefixes") or []
            ),
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
            evidence[f"{name}_by_repo"] = _repo_distribution(matching)

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

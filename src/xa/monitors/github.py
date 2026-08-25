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
        "changes_requested": sum(1 for p in prs if p.get("reviewDecision") == "CHANGES_REQUESTED"),
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
    "changes_requested": lambda p: p.get("reviewDecision") == "CHANGES_REQUESTED",
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
    m = metrics_for(prs, now)

    title = raw.get("title") or f"{m['total']} open pull requests"
    shown = raw.get("metrics") or ["total", "conflicts", "red", "stale_30d", "ready"]
    surfaced = {k: m[k] for k in shown if k in m}

    report = MonitorReport(monitor=spec.name, collected_at=now)

    # The oldest few in each named slice, so the row can be acted on without a
    # second round trip.
    evidence: dict[str, Any] = {"query": query}
    for name, predicate in SLICES.items():
        picked = sorted(
            (p for p in prs if predicate(p)),
            key=lambda p: p.get("updatedAt") or "",
        )[:20]
        if picked:
            evidence[name] = [
                {
                    "repo": p["repository"]["nameWithOwner"],
                    "number": p["number"],
                    "title": p["title"][:90],
                    "url": p["url"],
                    "updated": (p.get("updatedAt") or "")[:10],
                }
                for p in picked
            ]

    by_repo: dict[str, int] = {}
    for p in prs:
        by_repo[p["repository"]["nameWithOwner"]] = by_repo.get(p["repository"]["nameWithOwner"], 0) + 1
    evidence["by_repo"] = dict(sorted(by_repo.items(), key=lambda kv: -kv[1])[:15])

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

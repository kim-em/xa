"""Core data model.

The central split: a *monitor* emits `Observation`s, which are facts. It never
decides severity, never reads a threshold, never knows about snoozing. The
engine turns observations into `Item`s by applying policy.

Keeping monitors policy-free is what lets thresholds live in config, and what
lets someone else's monitors run unchanged on this engine.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# `fault`: something is wrong and could in principle be fixed.
# `pending`: nothing is broken; a decision is waiting on the user.
# `backlog`: a standing pile, reported as metrics and a trend, never as a list.
# `status`: useful context about a healthy system; no response is expected.
#
# Only faults contribute to the count and are eligible for push. The count is
# the scarce resource: a badge reading 7 has to mean seven things are wrong.
Kind = Literal["fault", "pending", "backlog", "status"]

# `info`: emitted, but too young to have crossed a threshold. Shown, not counted.
# `warn` / `alert`: crossed `warn_after` / `alert_after`.
# `unknown`: the monitor crashed, or its data aged past its TTL. Never `ok`,
#   because a check that cannot see is not a check that sees nothing wrong.
Severity = Literal["info", "warn", "alert", "unknown"]

SEVERITY_ORDER: dict[Severity, int] = {"info": 0, "unknown": 1, "warn": 2, "alert": 3}

# States an item can be in relative to the user's acknowledgements.
Disposition = Literal["active", "acked", "snoozed", "muted"]


def utcnow() -> datetime:
    """The one place the current time is read.

    During the practice run a wrong assumption about "now" turned a 31-minute-old
    commit into an apparent 4.6-hour stall. Every age in the system is derived
    from a stored absolute timestamp against this function, never from a
    timestamp baked in at collection time.
    """
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z and None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class Observation:
    """A fact reported by a monitor.

    `key` is stable identity: the same problem recurring gets the same key.
    `state_key` is a digest of *what is currently wrong*. Acknowledgements bind
    to the pair, so "I know, stop telling me" lapses the moment the failure
    changes into a different failure.

    Choosing `state_key` is the subtlest part of writing a monitor. Too
    fine-grained and an acknowledgement never sticks, because something
    incidental churns and every collection looks like a new problem. Too coarse
    and a genuinely new failure is silenced by an old acknowledgement.
    """

    key: str
    title: str
    kind: Kind = "fault"
    state_key: str = ""
    detail: str = ""
    since: datetime | None = None
    url: str | None = None
    # Items sharing a cluster collapse into one row. Without this the count
    # lies: 26 merge conflicts were really about 12 problems, because 15 of
    # them were one 55-day-old cluster of agent-authored PRs.
    cluster: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    # Everything an escalated session would otherwise have to re-derive: log
    # excerpts, file lists, diffs. Gathering this is the expensive part, and
    # doing it during collection is what keeps reads instant.
    evidence: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.state_key:
            self.state_key = derive_state_key(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Observation":
        kind = raw.get("kind", "fault")
        if kind not in ("fault", "pending", "backlog", "status"):
            raise ValueError(f"unknown kind {kind!r} for observation {raw.get('key')!r}")
        return cls(
            key=str(raw["key"]),
            title=str(raw.get("title", raw["key"])),
            kind=kind,
            state_key=str(raw.get("state_key", "")),
            detail=str(raw.get("detail", "")),
            since=parse_ts(raw.get("since")),
            url=raw.get("url"),
            cluster=raw.get("cluster"),
            metrics=dict(raw.get("metrics") or {}),
            evidence=dict(raw.get("evidence") or {}),
            actions=list(raw.get("actions") or []),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "state_key": self.state_key,
            "detail": self.detail,
            "since": self.since.isoformat() if self.since else None,
            "url": self.url,
            "cluster": self.cluster,
            "metrics": self.metrics,
            "evidence": self.evidence,
            "actions": self.actions,
        }


def derive_state_key(obs: Observation) -> str:
    """Fallback `state_key` when a monitor does not supply one.

    Deliberately excludes `since`, so a snooze survives the clock ticking and
    only lapses when the substance of the problem changes.
    """
    material = "\x1f".join([obs.key, obs.kind, obs.title, obs.detail])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class MonitorReport:
    """One monitor's output for one collection run."""

    monitor: str
    ok: bool = True
    collected_at: datetime = field(default_factory=utcnow)
    observations: list[Observation] = field(default_factory=list)
    # Set when the monitor itself failed. A crashed monitor is a bug in our
    # tooling; a broken thing is a bug in the world. Conflating them means
    # tooling bugs get triaged as world problems, or real problems get waved
    # away as flaky checks.
    error: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any], monitor: str | None = None) -> "MonitorReport":
        name = str(raw.get("monitor") or monitor or "")
        if not name:
            raise ValueError("monitor report has no name")
        return cls(
            monitor=name,
            ok=bool(raw.get("ok", True)),
            collected_at=parse_ts(raw.get("collected_at")) or utcnow(),
            observations=[Observation.from_json(o) for o in raw.get("observations") or []],
            error=raw.get("error"),
        )

    @classmethod
    def crashed(cls, monitor: str, error: str) -> "MonitorReport":
        return cls(monitor=monitor, ok=False, error=error)

    def to_json(self) -> dict[str, Any]:
        return {
            "monitor": self.monitor,
            "ok": self.ok,
            "collected_at": self.collected_at.isoformat(),
            "error": self.error,
            "observations": [o.to_json() for o in self.observations],
        }


@dataclass(slots=True, frozen=True)
class StoredPlan:
    """An investigation's output as it was recorded.

    The verdict and the time matter as much as the text: without them a failed
    run is indistinguishable from a finished one, which is how a timeout used
    to retire an item from the rung for good.
    """

    plan: str
    ok: bool
    created_at: datetime


@dataclass(slots=True)
class Item:
    """An observation with policy applied. This is what the user sees."""

    monitor: str
    obs: Observation
    severity: Severity = "info"
    disposition: Disposition = "active"
    # When an ack or snooze lapses. None means it is active.
    suppressed_until: datetime | None = None
    # How many separate observations collapsed into this row.
    cluster_size: int = 1
    # The monitor's current autonomy rung: report | investigate | auto.
    mode: str = "report"
    # Attached by an investigate run, when that rung is switched on.
    plan: str | None = None
    # Whether that run finished cleanly. A timed-out or crashed investigation
    # still has text worth reading, but it is not an answer, and presenting it
    # as one is how a half-finished report gets acted on.
    plan_ok: bool = True

    @property
    def key(self) -> str:
        return self.obs.key

    @property
    def uid(self) -> str:
        """Globally unique handle, which is what the CLI and TUI address."""
        return f"{self.monitor}/{self.obs.key}"

    def age(self, now: datetime | None = None) -> float | None:
        """Seconds since the condition began, or None if the monitor said nothing."""
        if self.obs.since is None:
            return None
        return ((now or utcnow()) - self.obs.since).total_seconds()

    @property
    def counts(self) -> bool:
        """Whether this item contributes to the headline number."""
        return (
            self.obs.kind == "fault"
            and self.disposition == "active"
            and self.severity in ("warn", "alert")
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "monitor": self.monitor,
            "severity": self.severity,
            "disposition": self.disposition,
            "suppressed_until": self.suppressed_until.isoformat() if self.suppressed_until else None,
            "cluster_size": self.cluster_size,
            "mode": self.mode,
            "plan": self.plan,
            "plan_ok": self.plan_ok,
            "counts": self.counts,
            **self.obs.to_json(),
        }

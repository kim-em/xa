"""Policy: thresholds, ageing, severity, clustering, and suppression.

Everything a monitor is not allowed to decide lives here. This module is the
reason "how long overdue matters" is a config edit rather than a code change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time, timezone
from typing import Iterable, Sequence

from .model import (
    SEVERITY_ORDER,
    Disposition,
    Item,
    MonitorReport,
    Observation,
    Severity,
    utcnow,
)

# ---------------------------------------------------------------------------
# Durations and "when"
# ---------------------------------------------------------------------------

_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)

_WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

DEFAULT_SNOOZE_HOUR = 9


def parse_duration(text: str | int | float | None) -> timedelta | None:
    """`"3h"`, `"90m"`, `"2d"`, `"1w"`, or a bare number of seconds."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return timedelta(seconds=float(text))
    m = _DURATION_RE.match(str(text))
    if not m:
        raise ValueError(f"cannot parse duration {text!r} (try 30m, 6h, 2d, 1w)")
    return timedelta(seconds=float(m.group(1)) * _DURATION_UNITS[m.group(2).lower()])


def parse_when(text: str | None, now: datetime | None = None, hour: int = DEFAULT_SNOOZE_HOUR) -> datetime:
    """Resolve a user-supplied snooze target to an absolute UTC instant.

    Accepts a duration (`3h`, `2d`), a weekday (`mon`), `tomorrow`, `tonight`,
    `next week`, or an ISO date/datetime. Bare days resolve to `hour` local
    time, because "tell me again tomorrow" means the morning, not 00:00.
    """
    now = now or utcnow()
    local_now = now.astimezone()
    raw = (text or "tomorrow").strip().lower()

    def at_local(d: datetime) -> datetime:
        return datetime.combine(d.date(), time(hour=hour), tzinfo=local_now.tzinfo).astimezone(timezone.utc)

    if raw in ("tomorrow", "tmr"):
        return at_local(local_now + timedelta(days=1))
    if raw == "tonight":
        return datetime.combine(local_now.date(), time(hour=20), tzinfo=local_now.tzinfo).astimezone(timezone.utc)
    if raw in ("next week", "nextweek"):
        return at_local(local_now + timedelta(days=7))
    if raw in _WEEKDAYS:
        delta = (_WEEKDAYS[raw] - local_now.weekday()) % 7 or 7
        return at_local(local_now + timedelta(days=delta))
    try:
        return now + parse_duration(raw)  # type: ignore[operator]
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"cannot parse {text!r}; try 3h, 2d, tomorrow, mon, next week, or 2026-09-01"
        ) from exc
    if parsed.tzinfo is None:
        if (parsed.hour, parsed.minute, parsed.second) == (0, 0, 0):
            parsed = parsed.replace(hour=hour)
        parsed = parsed.replace(tzinfo=local_now.tzinfo)
    return parsed.astimezone(timezone.utc)


def humanise(seconds: float | None) -> str:
    """Compact age rendering, tuned to stay narrow enough for a menu-bar row."""
    if seconds is None:
        return "-"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.0f}h"
    return f"{hours / 24:.0f}d"


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Thresholds:
    """When an emitted observation starts to matter.

    An observation exists because something is true. It becomes `warn` or
    `alert` only once it has been true long enough to be worth interrupting
    for, which is what stops a monitor that fires on every transient blip from
    training the user to ignore the whole surface.
    """

    warn_after: timedelta = timedelta(0)
    alert_after: timedelta | None = None
    # Whether an `alert` here is worth a phone push. Deliberately orthogonal to
    # severity, so severity does not silently become a paging policy.
    push: bool = False
    # How long this monitor's data stays trustworthy. Past it, items render
    # `unknown` rather than continuing to assert their last known state.
    ttl: timedelta | None = None

    @classmethod
    def from_config(cls, raw: dict | None, interval: timedelta | None = None) -> "Thresholds":
        raw = raw or {}
        ttl = parse_duration(raw.get("ttl"))
        if ttl is None and interval is not None:
            # Tolerate two missed collections before declaring blindness.
            ttl = interval * 3
        return cls(
            warn_after=parse_duration(raw.get("warn_after")) or timedelta(0),
            alert_after=parse_duration(raw.get("alert_after")),
            push=bool(raw.get("push", False)),
            ttl=ttl,
        )


def severity_for(
    obs: Observation,
    thresholds: Thresholds,
    now: datetime,
    stale: bool = False,
) -> Severity:
    """Map an observation plus its age onto a severity."""
    if stale:
        return "unknown"
    if obs.kind == "backlog":
        # Backlogs are a number with a trend. They never escalate on age, or
        # every standing pile would permanently read as an emergency.
        return "info"
    if obs.since is None:
        # No start time means we cannot age it. Report it, do not escalate it.
        return "warn" if obs.kind == "fault" else "info"
    age = now - obs.since
    if thresholds.alert_after is not None and age >= thresholds.alert_after:
        return "alert"
    if age >= thresholds.warn_after:
        return "warn"
    return "info"


# ---------------------------------------------------------------------------
# Suppression: acks, snoozes, mutes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Suppression:
    """A user's decision to stop hearing about something.

    Bound to `(uid, state_key)`. When the failure changes into a different
    failure the state_key moves and the suppression no longer matches, so the
    item resurfaces immediately. A `state_key` of `"*"` matches any state,
    which is what `mute` uses for a false positive that will never be right.
    """

    uid: str
    state_key: str
    disposition: Disposition
    until: datetime | None = None
    note: str = ""

    def matches(self, item: Item) -> bool:
        if self.uid != item.uid:
            return False
        return self.state_key in ("*", item.obs.state_key)

    def active_at(self, now: datetime) -> bool:
        return self.until is None or now < self.until


def apply_suppressions(item: Item, suppressions: Iterable[Suppression], now: datetime) -> Item:
    """Attach the most specific live suppression, if any."""
    best: Suppression | None = None
    for s in suppressions:
        if not s.matches(item) or not s.active_at(now):
            continue
        # An exact state_key match beats a wildcard mute, so a targeted ack is
        # not shadowed by a broad one.
        if best is None or (best.state_key == "*" and s.state_key != "*"):
            best = s
    if best is not None:
        item.disposition = best.disposition
        item.suppressed_until = best.until
    return item


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster(items: Sequence[Item]) -> list[Item]:
    """Collapse items sharing a monitor and cluster label into one row.

    Without this the count lies. Fifteen merge conflicts from one 55-day-old
    batch of agent-authored PRs are one problem, and six messages from one
    person on one day are one conversation.
    """
    out: list[Item] = []
    seen: dict[tuple[str, str], Item] = {}
    for item in items:
        label = item.obs.cluster
        if not label:
            out.append(item)
            continue
        ck = (item.monitor, label)
        head = seen.get(ck)
        if head is None:
            seen[ck] = item
            out.append(item)
            continue
        head.cluster_size += 1
        # The cluster inherits the worst severity and the oldest start, so
        # collapsing never hides the most urgent member.
        if SEVERITY_ORDER[item.severity] > SEVERITY_ORDER[head.severity]:
            head.severity = item.severity
        if item.obs.since and (head.obs.since is None or item.obs.since < head.obs.since):
            head.obs.since = item.obs.since
    return out


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MonitorPolicy:
    """Per-monitor policy, assembled from config."""

    name: str
    thresholds: Thresholds = field(default_factory=Thresholds)
    mode: str = "report"
    # Overrides matched against the start of an observation's key. One monitor
    # often reports several kinds of thing that deserve different deadlines:
    # a branch nobody has created is not urgent on the same timescale as a
    # build that has just gone red.
    key_thresholds: dict[str, Thresholds] = field(default_factory=dict)

    def thresholds_for(self, key: str) -> Thresholds:
        """The most specific matching override, or the monitor default."""
        best, best_len = self.thresholds, -1
        for prefix, thresholds in self.key_thresholds.items():
            if key.startswith(prefix) and len(prefix) > best_len:
                best, best_len = thresholds, len(prefix)
        return best


def build_items(
    reports: Iterable[MonitorReport],
    policies: dict[str, MonitorPolicy],
    suppressions: Iterable[Suppression] = (),
    now: datetime | None = None,
) -> list[Item]:
    """Turn raw monitor output into the list the user sees."""
    now = now or utcnow()
    suppressions = list(suppressions)
    items: list[Item] = []

    for report in reports:
        policy = policies.get(report.monitor) or MonitorPolicy(report.monitor)
        ttl = policy.thresholds.ttl
        stale = ttl is not None and (now - report.collected_at) > ttl

        if not report.ok:
            # A crashed monitor surfaces as one `unknown` row, never as silence
            # and never as `ok`. Losing sight of something is not the same as
            # seeing that it is fine.
            items.append(
                Item(
                    monitor=report.monitor,
                    obs=Observation(
                        key="_monitor",
                        title=f"{report.monitor} could not run",
                        kind="fault",
                        state_key="crashed",
                        detail=(report.error or "").strip()[:500],
                        since=report.collected_at,
                    ),
                    severity="unknown",
                    mode=policy.mode,
                )
            )
            continue

        for obs in report.observations:
            items.append(
                Item(
                    monitor=report.monitor,
                    obs=obs,
                    severity=severity_for(obs, policy.thresholds_for(obs.key), now, stale=stale),
                    mode=policy.mode,
                )
            )

    items = [apply_suppressions(i, suppressions, now) for i in items]
    return cluster(items)


def sort_key(item: Item, now: datetime | None = None):
    """Most urgent first, then oldest first within a severity."""
    now = now or utcnow()
    kind_rank = {"fault": 0, "pending": 1, "backlog": 2}[item.obs.kind]
    return (kind_rank, -SEVERITY_ORDER[item.severity], -(item.age(now) or 0))

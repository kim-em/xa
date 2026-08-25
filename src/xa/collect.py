"""Running monitors and assembling the snapshot.

Collection is the only place that touches the network or the clock-consuming
work. Reads never do, which is the whole point: asking must never start work.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config, MonitorSpec
from .model import Item, MonitorReport, utcnow
from .policy import MonitorPolicy, Thresholds, build_items, parse_duration, sort_key
from .store import Store


@dataclass(slots=True)
class Snapshot:
    """Everything a reader needs, precomputed."""

    generated_at: datetime
    items: list[Item] = field(default_factory=list)
    monitors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def count(self) -> int:
        """The headline number. Only active faults past a threshold."""
        return sum(1 for i in self.items if i.counts)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "generated_at": self.generated_at.isoformat(),
            "count": self.count,
            "monitors": self.monitors,
            "items": [i.to_json() for i in self.items],
        }


def _env_for(spec: MonitorSpec, cfg: Config) -> dict[str, str]:
    """Options reach a monitor as `XA_OPT_*`, so it can be tuned without editing."""
    env = dict(os.environ)
    env["XA_POLICY"] = str(cfg.root)
    env["XA_MONITOR"] = spec.name
    for key, value in spec.options.items():
        env[f"XA_OPT_{key.upper()}"] = value if isinstance(value, str) else json.dumps(value)
    return env


def run_monitor(spec: MonitorSpec, cfg: Config) -> tuple[MonitorReport, int]:
    """Execute one monitor and parse its report.

    A monitor that crashes, times out, or emits unparseable output produces an
    `unknown` report rather than silence. Losing sight of something is not the
    same as seeing that it is fine.
    """
    started = time.monotonic()

    if spec.kind != "exec":
        from . import monitors as _monitors

        try:
            report = _monitors.run_declarative(spec, cfg)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as `unknown`
            report = MonitorReport.crashed(spec.name, f"{type(exc).__name__}: {exc}")
        return report, int((time.monotonic() - started) * 1000)

    path = cfg.resolve(spec.exec)
    if path is None or not path.exists():
        return (
            MonitorReport.crashed(spec.name, f"monitor executable not found: {spec.exec}"),
            int((time.monotonic() - started) * 1000),
        )

    try:
        proc = subprocess.run(
            [str(path), *spec.args],
            capture_output=True,
            text=True,
            timeout=spec.timeout.total_seconds(),
            env=_env_for(spec, cfg),
            cwd=str(cfg.root),
        )
    except subprocess.TimeoutExpired:
        return (
            MonitorReport.crashed(spec.name, f"timed out after {spec.timeout}"),
            int((time.monotonic() - started) * 1000),
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0 and not proc.stdout.strip():
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        return MonitorReport.crashed(spec.name, f"exit {proc.returncode}: {' / '.join(tail)}"), duration_ms

    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        head = proc.stdout.strip()[:200]
        return MonitorReport.crashed(spec.name, f"bad JSON ({exc}): {head}"), duration_ms

    try:
        return MonitorReport.from_json(raw, monitor=spec.name), duration_ms
    except Exception as exc:  # noqa: BLE001
        return MonitorReport.crashed(spec.name, f"{type(exc).__name__}: {exc}"), duration_ms


def effective_policies(cfg: Config, store: Store | None) -> dict[str, MonitorPolicy]:
    """Config defaults with live overrides from the database layered on top."""
    policies = cfg.policies()
    if store is None:
        return policies

    modes = store.modes()
    overrides = store.overrides()
    for name, policy in policies.items():
        if name in modes:
            policy.mode = modes[name]
        for field_name, value in (overrides.get(name) or {}).items():
            if field_name in ("warn_after", "alert_after", "ttl"):
                setattr(policy.thresholds, field_name, parse_duration(value))
            elif field_name == "push":
                policy.thresholds.push = bool(value)
    return policies


def due(spec: MonitorSpec, store: Store, now: datetime) -> bool:
    last = store.last_run(spec.name)
    return last is None or (now - last) >= spec.interval


def collect(
    cfg: Config,
    store: Store,
    only: Sequence[str] | None = None,
    force: bool = False,
    reports: Iterable[MonitorReport] | None = None,
) -> Snapshot:
    """Run every due monitor and build the snapshot.

    Monitors run one at a time on purpose. Fanning out concurrently tripped TLS
    handshake failures against the GitHub API immediately during a manual
    practice run, and a monitoring tool that falls over under its own load is
    worse than none.
    """
    now = utcnow()
    collected: list[MonitorReport] = list(reports or [])

    if reports is None:
        for name, spec in cfg.monitors.items():
            if only and name not in only:
                continue
            if not spec.enabled:
                continue
            if not force and not due(spec, store, now):
                continue
            report, duration_ms = run_monitor(spec, cfg)
            store.record_run(name, report.collected_at, report.ok, report.error, duration_ms)
            store.save_report(report)
            collected.append(report)

        # The snapshot is the current picture, not a log of this tick. Monitors
        # that were not due still hold: their last report stands until it ages
        # past its TTL, at which point the engine marks it unknown rather than
        # letting it quietly keep asserting health.
        fresh = {r.monitor for r in collected}
        for raw in store.latest_reports(known=set(cfg.monitors)):
            if raw["monitor"] not in fresh:
                collected.append(MonitorReport.from_json(raw))

    policies = effective_policies(cfg, store)
    store.prune_suppressions(now)

    # Fill in `since` for observations whose monitor could not supply one, using
    # when we first saw this exact state. Ageing, thresholds and snooze lapse
    # all depend on it, so an item without a start time is a second-class item.
    for report in collected:
        for obs in report.observations:
            first = store.note_first_seen(report.monitor, obs.key, obs.state_key, report.collected_at)
            if obs.since is None:
                obs.since = first

    items = build_items(collected, policies, store.suppressions(), now=now)
    items.sort(key=lambda i: sort_key(i, now))
    store.record_items(items, now)

    return Snapshot(
        generated_at=now,
        items=items,
        monitors=[
            {
                "name": r.monitor,
                "ok": r.ok,
                "collected_at": r.collected_at.isoformat(),
                "error": r.error,
                "mode": (policies.get(r.monitor) or MonitorPolicy(r.monitor)).mode,
            }
            for r in collected
        ],
    )


def write_snapshot(snapshot: Snapshot, path: Path) -> None:
    """Write atomically, so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot.to_json(), indent=1))
    tmp.replace(path)


def read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None

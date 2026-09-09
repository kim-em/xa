"""Running monitors and assembling the snapshot.

Collection is the only place that touches the network or the clock-consuming
work. Reads never do, which is the whole point: asking must never start work.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import Config, MonitorSpec
from .model import Item, MonitorReport, utcnow
from .policy import MonitorPolicy, Thresholds, build_items, parse_duration, sort_key
from .store import Store

log = logging.getLogger("xa.collect")


@dataclass(slots=True)
class Snapshot:
    """Everything a reader needs, precomputed."""

    generated_at: datetime
    items: list[Item] = field(default_factory=list)
    monitors: list[dict[str, Any]] = field(default_factory=list)
    suppressions: list[dict[str, Any]] = field(default_factory=list)

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
            "suppressions": self.suppressions,
            "items": [i.to_json() for i in self.items],
        }


def _suppressions_payload(store: Store) -> list[dict[str, Any]]:
    return [
        {"uid": s.uid, "state_key": s.state_key, "disposition": s.disposition,
         "until": s.until.isoformat() if s.until else None, "note": s.note}
        for s in store.suppressions()
    ]

def _env_for(spec: MonitorSpec, cfg: Config) -> dict[str, str]:
    """Options reach a monitor as `XA_OPT_*`, so it can be tuned without editing."""
    env = dict(os.environ)
    env["XA_POLICY"] = str(cfg.root)
    env["XA_MONITOR"] = spec.name
    # Let monitors `import xa` without knowing where the engine is installed.
    # Hardcoding a source path in each monitor makes the policy directory
    # non-portable, and the policy directory is meant to outlive any one
    # machine's idea of where things live.
    import xa as _xa

    engine = str(Path(_xa.__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{engine}{os.pathsep}{existing}" if existing else engine
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
            targets = [policy.thresholds, *policy.key_thresholds.values()]
            for target in targets:
                if field_name in ("warn_after", "alert_after", "ttl"):
                    setattr(target, field_name, parse_duration(value))
                elif field_name == "push":
                    target.push = bool(value)
    return policies


# How long to wait before retrying a monitor that failed. Short, because the
# usual cause is a network that was not up yet, and the cost of being wrong is
# one more timeout.
RETRY_AFTER = timedelta(minutes=5)


def due(spec: MonitorSpec, store: Store, now: datetime, root: Path | None = None) -> bool:
    """Whether to run this monitor now.

    A monitor whose definition has changed is always due: waiting out an
    interval to see the effect of an edit is friction that stops you fixing a
    noisy check.

    A monitor whose last run *failed* is due again after `RETRY_AFTER` rather
    than its full interval. The collector runs on a laptop, which wakes with no
    network for a few seconds every day; without this, one badly-timed failure
    leaves a six-hourly check reading `unknown` for six hours.
    """
    if root is not None and store.fingerprint(spec.name) != spec.fingerprint(root):
        return True
    last, ok = store.last_run(spec.name)
    if last is None:
        return True
    return (now - last) >= (spec.interval if ok else min(spec.interval, RETRY_AFTER))


def build_snapshot(reports: list[MonitorReport], cfg: Config, store: Store,
                   now: datetime) -> Snapshot:
    """Turn monitor reports into the picture the user reads.

    Every snapshot is built here, including the ones published part-way through
    a sweep. There used to be two of these and they drifted: the progress
    builder skipped both the `since` backfill and the plan attachment, so a
    sweep withdrew every investigation it had already paid for and, because an
    observation with no start time cannot be aged and so reports `warn`,
    invented faults that the final snapshot then retracted. A snapshot that is
    published is a snapshot that is read; there is no such thing as a draft.
    """
    from .investigate import attach
    from .work import attach as attach_work

    policies = effective_policies(cfg, store)

    # Fill in `since` for observations whose monitor could not supply one, using
    # when we first saw this exact state. Ageing, thresholds and snooze lapse
    # all depend on it, so an item without a start time is a second-class item.
    for report in reports:
        for obs in report.observations:
            first = store.note_first_seen(report.monitor, obs.key, obs.state_key, report.collected_at)
            if obs.since is None:
                obs.since = first

    items = build_items(reports, policies, store.suppressions(), now=now)
    items.sort(key=lambda i: sort_key(i, now))

    # Attach any plan already produced for each item's current state. Doing this
    # unconditionally means a plan produced before a monitor was demoted back to
    # `report` still shows, rather than silently disappearing.
    attach(items, store)

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
            for r in reports
        ],
        suppressions=_suppressions_payload(store),
    )


def snapshot_from_store(cfg: Config, store: Store, now: datetime | None = None) -> Snapshot:
    """Build the current picture from whatever each monitor last reported."""
    now = now or utcnow()
    reports = [MonitorReport.from_json(raw) for raw in store.latest_reports(known=set(cfg.monitors))]
    return build_snapshot(reports, cfg, store, now)


def collect(
    cfg: Config,
    store: Store,
    only: Sequence[str] | None = None,
    force: bool = False,
    reports: Iterable[MonitorReport] | None = None,
    on_progress: "Callable[[Snapshot], None] | None" = None,
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
            if not force and not due(spec, store, now, cfg.root):
                continue
            report, duration_ms = run_monitor(spec, cfg)
            store.record_run(name, report.collected_at, report.ok, report.error, duration_ms)
            store.save_report(report, spec.fingerprint(cfg.root))
            collected.append(report)
            if on_progress is not None:
                # Publish after every monitor, not at the end of the pass. A
                # full sweep takes minutes, and a cold start that shows nothing
                # until it finishes is indistinguishable from a broken tool.
                on_progress(snapshot_from_store(cfg, store))

        # The snapshot is the current picture, not a log of this tick. Monitors
        # that were not due still hold: their last report stands until it ages
        # past its TTL, at which point the engine marks it unknown rather than
        # letting it quietly keep asserting health.
        fresh = {r.monitor for r in collected}
        for raw in store.latest_reports(known=set(cfg.monitors)):
            if raw["monitor"] not in fresh:
                collected.append(MonitorReport.from_json(raw))

    # Everything that ages out. These are cheap deletes on a small database,
    # and the alternative is a table nobody ever prunes: `forget_plans` existed
    # for months with no caller, which meant a state that changed and later
    # came back silently reattached an arbitrarily old investigation.
    store.prune_suppressions(now)
    store.forget_plans()
    store.forget_first_seen()
    store.prune_history()

    snapshot = build_snapshot(collected, cfg, store, now)
    store.record_items(snapshot.items, now)
    return snapshot


def investigate_pending(cfg: Config, store: Store, snapshot: Snapshot,
                        timeout: timedelta = timedelta(minutes=10)) -> int:
    """Run investigations for items whose monitor is on that rung.

    Deliberately after the snapshot is written, and one at a time: an
    investigation takes minutes, and holding the whole picture back while one
    runs would make a rung meant to save time cost it instead.
    """
    from .investigate import run as run_one, wanted

    done = 0
    for item in snapshot.items:
        if not wanted(item, store):
            continue
        result = run_one(item.to_json(), cfg, timeout)
        if result is None:
            continue
        store.save_plan(result.uid, result.state_key, result.plan, result.ok,
                        from_agent=result.from_agent)
        item.plan, item.plan_ok = result.plan, result.ok
        # Only successes count. A caller that logs "attached 3 plan(s)" after
        # three timeouts is reporting work it did not do.
        if result.ok:
            done += 1
        else:
            log.warning("investigation of %s failed: %s", result.uid, result.plan[:200])
    return done


def write_snapshot_json(payload: dict[str, Any], path: Path) -> None:
    """Write atomically, so a reader never sees a half-written file.

    The temporary name carries the pid. The daemon and an `xa ack` both publish
    to this path, and a shared temporary file lets one of them replace the
    other's underneath it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)


def write_snapshot(snapshot: Snapshot, path: Path) -> None:
    write_snapshot_json(snapshot.to_json(), path)


def read_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None

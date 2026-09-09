"""Scheduled mutating jobs, kept deliberately separate from monitors.

`xa collect --force` may run every monitor and must remain observational. Jobs
run only when their own scheduler says they are due, or through explicit
`xa run`. Their latest result is a small atomic JSON document that a cheap
monitor can inspect without repeating the work.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config as config_mod
from .config import Config, JobSpec
from .model import parse_ts, utcnow


class JobBusy(RuntimeError):
    pass


def status_path(name: str) -> Path:
    return config_mod.state_dir() / "jobs" / f"{name}.json"


def read_status(name: str) -> dict[str, Any] | None:
    path = status_path(name)
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def write_status(name: str, payload: dict[str, Any]) -> None:
    path = status_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    tmp.replace(path)


@contextmanager
def job_lock(name: str) -> Iterator[None]:
    path = status_path(name).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JobBusy(f"job {name!r} is already running") from exc
        yield


def due(spec: JobSpec, now: datetime | None = None) -> bool:
    now = now or utcnow()
    status = read_status(spec.name)
    if status is None:
        return True
    finished = parse_ts(status.get("finished_at"))
    if finished is None:
        return True
    wait = spec.interval if status.get("ok") else spec.retry_after
    return now - finished >= wait


def _env_for(spec: JobSpec, cfg: Config) -> dict[str, str]:
    env = dict(os.environ)
    env["XA_POLICY"] = str(cfg.root)
    env["XA_JOB"] = spec.name
    env["XA_JOB_STATUS"] = str(status_path(spec.name))
    import xa as _xa

    engine = str(Path(_xa.__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{engine}{os.pathsep}{existing}" if existing else engine
    for key, value in spec.options.items():
        env[f"XA_OPT_{key.upper()}"] = value if isinstance(value, str) else json.dumps(value)
    return env


def run_job(spec: JobSpec, cfg: Config) -> dict[str, Any]:
    """Run one job under its lock and persist success or failure atomically."""
    previous = read_status(spec.name) or {}
    started = utcnow()
    started_clock = time.monotonic()
    path = cfg.resolve(spec.exec)
    result: dict[str, Any] = {}
    error: str | None = None
    returncode: int | None = None

    with job_lock(spec.name):
        if path is None or not path.exists():
            error = f"job executable not found: {spec.exec}"
        else:
            try:
                proc = subprocess.run(
                    [str(path), *spec.args],
                    capture_output=True,
                    text=True,
                    timeout=spec.timeout.total_seconds(),
                    env=_env_for(spec, cfg),
                    cwd=str(cfg.root),
                )
                returncode = proc.returncode
                if proc.stdout.strip():
                    try:
                        decoded = json.loads(proc.stdout)
                        if isinstance(decoded, dict):
                            result = decoded
                        else:
                            error = "job output was JSON but not an object"
                    except json.JSONDecodeError as exc:
                        error = f"job emitted invalid JSON: {exc}"
                if proc.returncode != 0:
                    tail = (proc.stderr or proc.stdout or "job failed").strip().splitlines()[-5:]
                    error = " / ".join(tail)[:1000]
                elif not result and error is None:
                    error = "job emitted no result"
            except subprocess.TimeoutExpired:
                error = f"timed out after {spec.timeout}"
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"

        ok = error is None and bool(result.get("ok", True))
        if not ok and error is None:
            error = str(result.get("error") or result.get("summary") or "job reported failure")
        finished = utcnow()
        status = {
            "name": spec.name,
            "ok": ok,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "last_success_at": (
                finished.isoformat() if ok else previous.get("last_success_at")
            ),
            "duration_ms": int((time.monotonic() - started_clock) * 1000),
            "returncode": returncode,
            "summary": str(result.get("summary") or ("completed" if ok else error)),
            "error": error,
            "details": result.get("details") or {},
        }
        write_status(spec.name, status)
        return status


def run_due_jobs(cfg: Config, now: datetime | None = None) -> list[dict[str, Any]]:
    """Run due jobs serially; callers decide which health monitors to refresh."""
    now = now or utcnow()
    results = []
    for spec in cfg.jobs.values():
        if not spec.enabled or not due(spec, now):
            continue
        try:
            results.append(run_job(spec, cfg))
        except JobBusy:
            continue
    return results

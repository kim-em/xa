"""Helpers for writing monitors.

A monitor is any executable that prints one JSON document. This module exists
only to make the common case short; nothing here is required, and a monitor
written in bash or Go is equally welcome.

The contract a monitor must honour:

- Emit facts, not judgements. Report `since`, never a severity.
- Set `state_key` to a digest of *what is currently wrong*, excluding anything
  incidental that churns between runs. Acknowledgements bind to it.
- Attach evidence. Whatever an escalated session would otherwise have to go and
  fetch again belongs in `evidence`, gathered now while it is cheap.
- Fail loudly. Exit non-zero with a message on stderr rather than reporting
  that everything is fine.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Sequence

# Transient network failures that deserve a retry rather than a crash. Fanning
# out concurrently against the GitHub API produced all of these within seconds
# during a manual practice run.
TRANSIENT = (
    "TLS handshake timeout",
    "unexpected EOF",
    "connection reset",
    "i/o timeout",
    "EOF",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "was submitted too quickly",
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def opt(name: str, default: Any = None) -> Any:
    """Read a config-supplied option, passed in as `XA_OPT_<NAME>`.

    This is how a monitor stays tunable without being edited.
    """
    raw = os.environ.get(f"XA_OPT_{name.upper()}")
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def job_status(name: str) -> dict[str, Any] | None:
    """Read the latest persisted result of a scheduled job."""
    from xa.jobs import read_status

    return read_status(name)


def digest(*parts: Any) -> str:
    """A short, stable digest for use as a `state_key`."""
    material = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def run(cmd: Sequence[str], retries: int = 4, timeout: int = 120) -> str:
    """Run a command, retrying only on transient network failures.

    Retries are serial with backoff on purpose. The failure mode being guarded
    against is caused by concurrency, so responding to it with more concurrency
    would be exactly wrong.
    """
    last = ""
    for attempt in range(retries):
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout
        last = (proc.stderr or proc.stdout or "").strip()
        if not any(t in last for t in TRANSIENT):
            break
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{' '.join(cmd[:3])}...: {last[:300]}")


def gh(*args: str, **kw: Any) -> str:
    return run(["gh", *args], **kw)


def gh_json(*args: str, **kw: Any) -> Any:
    return json.loads(gh(*args, **kw) or "null")


def gh_api(path: str, **kw: Any) -> Any:
    return gh_json("api", path, **kw)


def gh_graphql(query: str, **variables: Any) -> Any:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-F", f"{k}={v}"]
    return gh_json(*args)


class Report:
    """Accumulates observations, then prints the document the engine reads."""

    def __init__(self, monitor: str | None = None):
        self.monitor = monitor or os.environ.get("XA_MONITOR", "")
        self.collected_at = now()
        self.observations: list[dict[str, Any]] = []

    def add(
        self,
        key: str,
        title: str,
        *,
        kind: str = "fault",
        state_key: str = "",
        detail: str = "",
        since: datetime | None = None,
        url: str | None = None,
        cluster: str | None = None,
        metrics: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        actions: Sequence[str] = (),
    ) -> None:
        self.observations.append(
            {
                "key": key,
                "title": title,
                "kind": kind,
                "state_key": state_key,
                "detail": detail,
                "since": iso(since),
                "url": url,
                "cluster": cluster,
                "metrics": metrics or {},
                "evidence": evidence or {},
                "actions": list(actions),
            }
        )

    def emit(self) -> None:
        json.dump(
            {
                "monitor": self.monitor,
                "ok": True,
                "collected_at": iso(self.collected_at),
                "observations": self.observations,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")


def main(fn) -> None:
    """Wrap a monitor body so that any failure becomes a loud non-zero exit.

    A monitor must never swallow an error and report health. `unknown` is a
    legitimate answer; a false `ok` is not.
    """
    report = Report()
    try:
        fn(report)
    except Exception as exc:  # noqa: BLE001
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    report.emit()

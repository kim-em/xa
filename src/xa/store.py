"""Durable state: acknowledgements, snoozes, modes, threshold overrides, history.

Two tiers, deliberately:

- The policy directory holds *defaults*, in files a human edits and git tracks.
- This database holds *live overrides*, mutated by `xa` at runtime.

The database wins. That way `xa snooze` takes effect instantly without a commit
and a sync, while the checked-in files remain the readable statement of intent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .model import parse_ts, utcnow
from .policy import Suppression

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppressions (
    uid         TEXT NOT NULL,
    state_key   TEXT NOT NULL,
    disposition TEXT NOT NULL,
    until       TEXT,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (uid, state_key)
);

CREATE TABLE IF NOT EXISTS modes (
    monitor    TEXT PRIMARY KEY,
    mode       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overrides (
    monitor    TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (monitor, name)
);

-- One row per observed (monitor, key, state_key) transition. This is the
-- baseline that trend detection reads; without durable history a rebuilt host
-- silently reports "all good" because it has nothing to compare against.
CREATE TABLE IF NOT EXISTS history (
    ts        TEXT NOT NULL,
    monitor   TEXT NOT NULL,
    key       TEXT NOT NULL,
    state_key TEXT NOT NULL,
    severity  TEXT NOT NULL,
    metrics   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS history_lookup ON history (monitor, key, ts);

-- When a given (monitor, key, state_key) was first observed. Monitors often
-- cannot know when a condition began: a disk fills gradually, a host stops
-- answering at no particular moment. Rather than have each invent a timestamp
-- or report none, the engine remembers when it first saw the situation.
CREATE TABLE IF NOT EXISTS first_seen (
    monitor    TEXT NOT NULL,
    key        TEXT NOT NULL,
    state_key  TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (monitor, key, state_key)
);

CREATE TABLE IF NOT EXISTS runs (
    monitor      TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    ok           INTEGER NOT NULL,
    error        TEXT,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS runs_lookup ON runs (monitor, collected_at);

CREATE TABLE IF NOT EXISTS actions_log (
    ts       TEXT NOT NULL,
    uid      TEXT NOT NULL,
    action   TEXT NOT NULL,
    agent    TEXT NOT NULL,
    mode     TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- suppressions -----------------------------------------------------

    def suppress(
        self,
        uid: str,
        state_key: str,
        disposition: str,
        until: datetime | None,
        note: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO suppressions (uid, state_key, disposition, until, note, created_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(uid, state_key) DO UPDATE SET"
            " disposition=excluded.disposition, until=excluded.until,"
            " note=excluded.note, created_at=excluded.created_at",
            (uid, state_key, disposition, until.isoformat() if until else None, note, utcnow().isoformat()),
        )

    def unsuppress(self, uid: str) -> int:
        cur = self.db.execute("DELETE FROM suppressions WHERE uid = ?", (uid,))
        return cur.rowcount

    def suppressions(self) -> list[Suppression]:
        rows = self.db.execute("SELECT * FROM suppressions").fetchall()
        return [
            Suppression(
                uid=r["uid"],
                state_key=r["state_key"],
                disposition=r["disposition"],  # type: ignore[arg-type]
                until=parse_ts(r["until"]),
                note=r["note"],
            )
            for r in rows
        ]

    def prune_suppressions(self, now: datetime | None = None) -> int:
        """Drop suppressions whose expiry has passed.

        Nothing is silenced permanently by design, so an ack made in haste
        costs weeks rather than forever.
        """
        now = now or utcnow()
        cur = self.db.execute(
            "DELETE FROM suppressions WHERE until IS NOT NULL AND until < ?", (now.isoformat(),)
        )
        return cur.rowcount

    # -- modes and threshold overrides ------------------------------------

    def set_mode(self, monitor: str, mode: str) -> None:
        self.db.execute(
            "INSERT INTO modes (monitor, mode, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(monitor) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
            (monitor, mode, utcnow().isoformat()),
        )

    def modes(self) -> dict[str, str]:
        return {r["monitor"]: r["mode"] for r in self.db.execute("SELECT * FROM modes")}

    def set_override(self, monitor: str, name: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO overrides (monitor, name, value, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(monitor, name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (monitor, name, json.dumps(value), utcnow().isoformat()),
        )

    def overrides(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.db.execute("SELECT * FROM overrides"):
            out.setdefault(r["monitor"], {})[r["name"]] = json.loads(r["value"])
        return out

    # -- first seen -------------------------------------------------------

    def note_first_seen(self, monitor: str, key: str, state_key: str, now: datetime) -> datetime:
        """Record and return when this exact situation was first observed."""
        self.db.execute(
            "INSERT OR IGNORE INTO first_seen (monitor, key, state_key, first_seen) VALUES (?,?,?,?)",
            (monitor, key, state_key, now.isoformat()),
        )
        row = self.db.execute(
            "SELECT first_seen FROM first_seen WHERE monitor=? AND key=? AND state_key=?",
            (monitor, key, state_key),
        ).fetchone()
        return parse_ts(row["first_seen"]) or now

    def forget_first_seen(self, keep: timedelta = timedelta(days=180)) -> int:
        cutoff = (utcnow() - keep).isoformat()
        return self.db.execute("DELETE FROM first_seen WHERE first_seen < ?", (cutoff,)).rowcount

    # -- history ----------------------------------------------------------

    def record_run(self, monitor: str, collected_at: datetime, ok: bool, error: str | None, duration_ms: int) -> None:
        self.db.execute(
            "INSERT INTO runs (monitor, collected_at, ok, error, duration_ms) VALUES (?,?,?,?,?)",
            (monitor, collected_at.isoformat(), int(ok), error, duration_ms),
        )

    def record_items(self, items: Iterable[Any], now: datetime | None = None) -> None:
        now = (now or utcnow()).isoformat()
        self.db.executemany(
            "INSERT INTO history (ts, monitor, key, state_key, severity, metrics) VALUES (?,?,?,?,?,?)",
            [
                (now, i.monitor, i.obs.key, i.obs.state_key, i.severity, json.dumps(i.obs.metrics))
                for i in items
            ],
        )

    def last_run(self, monitor: str) -> datetime | None:
        row = self.db.execute(
            "SELECT collected_at FROM runs WHERE monitor = ? ORDER BY collected_at DESC LIMIT 1",
            (monitor,),
        ).fetchone()
        return parse_ts(row["collected_at"]) if row else None

    def metric_series(self, monitor: str, key: str, metric: str, since: datetime) -> list[tuple[datetime, float]]:
        """Past values of one metric, for trend detection."""
        rows = self.db.execute(
            "SELECT ts, metrics FROM history WHERE monitor=? AND key=? AND ts >= ? ORDER BY ts",
            (monitor, key, since.isoformat()),
        ).fetchall()
        out = []
        for r in rows:
            value = json.loads(r["metrics"]).get(metric)
            if isinstance(value, (int, float)):
                out.append((parse_ts(r["ts"]), float(value)))
        return out

    def prune_history(self, keep: timedelta = timedelta(days=400)) -> int:
        cutoff = (utcnow() - keep).isoformat()
        cur = self.db.execute("DELETE FROM history WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM runs WHERE collected_at < ?", (cutoff,))
        return cur.rowcount

    # -- action log -------------------------------------------------------

    def log_action(self, uid: str, action: str, agent: str, mode: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO actions_log (ts, uid, action, agent, mode, detail) VALUES (?,?,?,?,?,?)",
            (utcnow().isoformat(), uid, action, agent, mode, detail),
        )

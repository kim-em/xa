"""Durable state: acknowledgements, snoozes, modes, threshold overrides, history.

Two tiers, deliberately:

- The policy directory holds *defaults*, in files a human edits and git tracks.
- This database holds *live overrides*, mutated by `xa` at runtime.

The database wins. That way `xa snooze` takes effect instantly without a commit
and a sync, while the checked-in files remain the readable statement of intent.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .model import StoredPlan, WorkSession, parse_ts, utcnow
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

-- When the current continuous occurrence of a (monitor, key, state_key) was
-- first observed. Monitors often cannot know when a condition began: a disk
-- fills gradually, a host stops answering at no particular moment. Rather than
-- have each invent a timestamp or report none, the engine remembers when it
-- first saw the situation and removes the row after a successful clear.
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

-- The most recent report from each monitor, kept so the snapshot survives ticks
-- where nothing was due. Rebuilding it from only what just ran would erase every
-- monitor that happened not to be scheduled, which reads as "all clear".
CREATE TABLE IF NOT EXISTS latest_reports (
    monitor      TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    payload      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL DEFAULT ''
);

-- An investigation's output, keyed by the state it was about. When the problem
-- changes, its state_key changes and the stale plan stops being attached, which
-- is the same rule acknowledgements follow.
CREATE TABLE IF NOT EXISTS plans (
    uid        TEXT NOT NULL,
    state_key  TEXT NOT NULL,
    plan       TEXT NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 1,
    -- 0 when the text is an engine note rather than an agent's report.
    from_agent INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY (uid, state_key)
);

CREATE TABLE IF NOT EXISTS actions_log (
    ts       TEXT NOT NULL,
    uid      TEXT NOT NULL,
    action   TEXT NOT NULL,
    agent    TEXT NOT NULL,
    mode     TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);

-- The current working session for one action on an exact item state. This is deliberately
-- separate from actions_log: the log is an audit trail, while this row has a
-- lifecycle and is what prevents `xa open` from duplicating live work.
CREATE TABLE IF NOT EXISTS work_sessions (
    uid          TEXT NOT NULL,
    state_key    TEXT NOT NULL,
    monitor      TEXT NOT NULL,
    action       TEXT NOT NULL,
    agent        TEXT NOT NULL,
    status       TEXT NOT NULL,
    marker       TEXT NOT NULL DEFAULT '',
    session_name TEXT,
    session_backend TEXT NOT NULL DEFAULT '',
    session_cwd  TEXT NOT NULL DEFAULT '',
    session_owner TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (uid, state_key, action)
);
"""


# Columns added after the initial schema, applied to existing databases.
ADDED_COLUMNS = [
    ("latest_reports", "fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("plans", "from_agent", "INTEGER NOT NULL DEFAULT 1"),
    ("work_sessions", "session_backend", "TEXT NOT NULL DEFAULT ''"),
    ("work_sessions", "session_cwd", "TEXT NOT NULL DEFAULT ''"),
    ("work_sessions", "session_owner", "TEXT NOT NULL DEFAULT ''"),
]


def synchronised(fn):
    """Serialise access to the connection.

    Two processes write this database: the daemon on its tick, and `xa` when
    you acknowledge something. WAL is what makes that safe. The lock is the
    in-process half of the same guarantee, since sqlite3 forbids sharing one
    connection across threads and a caller is free to add one.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that CREATE TABLE IF NOT EXISTS cannot.

        The schema will keep changing, and an existing database must survive
        that: a monitoring tool that needs its history deleted to take an
        upgrade has thrown away the baseline every trend depends on.
        """
        for table, column, spec in ADDED_COLUMNS:
            existing = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def execute(self, sql: str, params: tuple = ()):
        """Escape hatch for callers that need raw SQL, still serialised."""
        with self._lock:
            return self.db.execute(sql, params)

    # -- suppressions -----------------------------------------------------

    @synchronised
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

    @synchronised
    def unsuppress(self, uid: str) -> int:
        cur = self.db.execute("DELETE FROM suppressions WHERE uid = ?", (uid,))
        return cur.rowcount

    @synchronised
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

    @synchronised
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

    @synchronised
    def set_mode(self, monitor: str, mode: str) -> None:
        self.db.execute(
            "INSERT INTO modes (monitor, mode, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(monitor) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
            (monitor, mode, utcnow().isoformat()),
        )

    @synchronised
    def modes(self) -> dict[str, str]:
        return {r["monitor"]: r["mode"] for r in self.db.execute("SELECT * FROM modes")}

    @synchronised
    def set_override(self, monitor: str, name: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO overrides (monitor, name, value, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(monitor, name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (monitor, name, json.dumps(value), utcnow().isoformat()),
        )

    @synchronised
    def overrides(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in self.db.execute("SELECT * FROM overrides"):
            out.setdefault(r["monitor"], {})[r["name"]] = json.loads(r["value"])
        return out

    # -- latest reports ---------------------------------------------------

    @synchronised
    def save_report(self, report, fingerprint: str = "") -> None:
        self.db.execute(
            "INSERT INTO latest_reports (monitor, collected_at, payload, fingerprint) VALUES (?,?,?,?)"
            " ON CONFLICT(monitor) DO UPDATE SET"
            " collected_at=excluded.collected_at, payload=excluded.payload,"
            " fingerprint=excluded.fingerprint",
            (report.monitor, report.collected_at.isoformat(), json.dumps(report.to_json()), fingerprint),
        )

    @synchronised
    def fingerprint(self, monitor: str) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM latest_reports WHERE monitor = ?", (monitor,)
        ).fetchone()
        return row["fingerprint"] if row else None

    @synchronised
    def invalidate_report(self, monitor: str) -> None:
        """Make a monitor due without discarding the last factual report."""
        self.db.execute(
            "UPDATE latest_reports SET fingerprint = '' WHERE monitor = ?", (monitor,)
        )

    @synchronised
    def latest_reports(self, known: set[str] | None = None) -> list[dict]:
        """Every monitor's most recent report, newest state per monitor.

        `known` drops reports from monitors that no longer exist in config, so
        deleting a monitor makes its items disappear rather than freeze.
        """
        rows = self.db.execute("SELECT monitor, payload FROM latest_reports").fetchall()
        out = []
        for r in rows:
            if known is not None and r["monitor"] not in known:
                continue
            out.append(json.loads(r["payload"]))
        return out

    @synchronised
    def forget_report(self, monitor: str) -> None:
        self.db.execute("DELETE FROM latest_reports WHERE monitor = ?", (monitor,))

    # -- first seen -------------------------------------------------------

    @synchronised
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

    @synchronised
    def forget_inactive_first_seen(
        self, monitor: str, active: Iterable[tuple[str, str]],
    ) -> int:
        """Forget states a successful report says are no longer present.

        A state that returns after disappearing is a new occurrence, even when
        its stable identity is unchanged. Keeping the old timestamp made a
        momentary recurrence look weeks old and could promote it straight to
        alert severity.
        """
        active = set(active)
        rows = self.db.execute(
            "SELECT key, state_key FROM first_seen WHERE monitor = ?", (monitor,)
        ).fetchall()
        stale = [
            (monitor, row["key"], row["state_key"])
            for row in rows
            if (row["key"], row["state_key"]) not in active
        ]
        self.db.executemany(
            "DELETE FROM first_seen WHERE monitor=? AND key=? AND state_key=?", stale
        )
        return len(stale)

    @synchronised
    def forget_first_seen(self, keep: timedelta = timedelta(days=180)) -> int:
        cutoff = (utcnow() - keep).isoformat()
        return self.db.execute("DELETE FROM first_seen WHERE first_seen < ?", (cutoff,)).rowcount

    # -- history ----------------------------------------------------------

    @synchronised
    def record_run(self, monitor: str, collected_at: datetime, ok: bool, error: str | None, duration_ms: int) -> None:
        self.db.execute(
            "INSERT INTO runs (monitor, collected_at, ok, error, duration_ms) VALUES (?,?,?,?,?)",
            (monitor, collected_at.isoformat(), int(ok), error, duration_ms),
        )

    @synchronised
    def record_items(self, items: Iterable[Any], now: datetime | None = None) -> None:
        now = (now or utcnow()).isoformat()
        self.db.executemany(
            "INSERT INTO history (ts, monitor, key, state_key, severity, metrics) VALUES (?,?,?,?,?,?)",
            [
                (now, i.monitor, i.obs.key, i.obs.state_key, i.severity, json.dumps(i.obs.metrics))
                for i in items
            ],
        )

    @synchronised
    def last_run(self, monitor: str) -> tuple[datetime | None, bool]:
        """When this monitor last ran, and whether that run succeeded.

        The verdict matters to scheduling: a monitor that failed should be tried
        again long before its interval is up. See `collect.due`.
        """
        row = self.db.execute(
            "SELECT collected_at, ok FROM runs WHERE monitor = ? ORDER BY collected_at DESC LIMIT 1",
            (monitor,),
        ).fetchone()
        return (parse_ts(row["collected_at"]), bool(row["ok"])) if row else (None, True)

    @synchronised
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

    @synchronised
    def prune_history(self, keep: timedelta = timedelta(days=400)) -> int:
        cutoff = (utcnow() - keep).isoformat()
        cur = self.db.execute("DELETE FROM history WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM runs WHERE collected_at < ?", (cutoff,))
        return cur.rowcount

    # -- investigation plans ----------------------------------------------

    @synchronised
    def save_plan(self, uid: str, state_key: str, plan: str, ok: bool = True,
                  from_agent: bool = True) -> None:
        self.db.execute(
            "INSERT INTO plans (uid, state_key, plan, ok, from_agent, created_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(uid, state_key) DO UPDATE SET"
            " plan=excluded.plan, ok=excluded.ok, from_agent=excluded.from_agent,"
            " created_at=excluded.created_at",
            (uid, state_key, plan, int(ok), int(from_agent), utcnow().isoformat()),
        )

    @synchronised
    def stored_plan(self, uid: str, state_key: str) -> StoredPlan | None:
        row = self.db.execute(
            "SELECT plan, ok, from_agent, created_at FROM plans"
            " WHERE uid = ? AND state_key = ?",
            (uid, state_key),
        ).fetchone()
        if row is None:
            return None
        return StoredPlan(row["plan"], bool(row["ok"]),
                          parse_ts(row["created_at"]) or utcnow(),
                          from_agent=bool(row["from_agent"]))

    @synchronised
    def forget_plans(self, keep: timedelta = timedelta(days=30)) -> int:
        cutoff = (utcnow() - keep).isoformat()
        return self.db.execute("DELETE FROM plans WHERE created_at < ?", (cutoff,)).rowcount

    # -- action log -------------------------------------------------------

    @synchronised
    def log_action(self, uid: str, action: str, agent: str, mode: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO actions_log (ts, uid, action, agent, mode, detail) VALUES (?,?,?,?,?,?)",
            (utcnow().isoformat(), uid, action, agent, mode, detail),
        )

    @synchronised
    def recent_actions(self, uid: str, limit: int = 5) -> list[sqlite3.Row]:
        """What has already been done about this item, most recent first.

        Worth showing before starting anything: an item that was escalated an
        hour ago probably has a session open on it somewhere.
        """
        return self.db.execute(
            "SELECT ts, action, agent, mode FROM actions_log WHERE uid = ?"
            " ORDER BY ts DESC LIMIT ?",
            (uid, limit),
        ).fetchall()

    # -- working sessions ------------------------------------------------

    @staticmethod
    def _work(row: sqlite3.Row | None) -> WorkSession | None:
        if row is None:
            return None
        return WorkSession(
            uid=row["uid"], state_key=row["state_key"], monitor=row["monitor"],
            action=row["action"], agent=row["agent"], status=row["status"],
            marker=row["marker"], session_name=row["session_name"],
            session_backend=row["session_backend"], session_cwd=row["session_cwd"],
            session_owner=row["session_owner"],
            started_at=parse_ts(row["started_at"]) or utcnow(),
            updated_at=parse_ts(row["updated_at"]) or utcnow(),
        )

    @synchronised
    def start_work(self, uid: str, state_key: str, monitor: str, action: str,
                   agent: str, marker: str = "", session_name: str | None = None,
                   session_backend: str = "", session_cwd: str = "",
                   session_owner: str = "",
                   started_at: datetime | None = None) -> WorkSession:
        now = started_at or utcnow()
        self.db.execute(
            "INSERT INTO work_sessions"
            " (uid,state_key,monitor,action,agent,status,marker,session_name,"
            " session_backend,session_cwd,session_owner,started_at,updated_at)"
            " VALUES (?,?,?,?,?,'starting',?,?,?,?,?,?,?)"
            " ON CONFLICT(uid,state_key,action) DO UPDATE SET"
            " monitor=excluded.monitor, action=excluded.action, agent=excluded.agent,"
            " status='starting', marker=excluded.marker, session_name=excluded.session_name,"
            " session_backend=excluded.session_backend, session_cwd=excluded.session_cwd,"
            " session_owner=excluded.session_owner,"
            " started_at=excluded.started_at, updated_at=excluded.updated_at",
            (uid, state_key, monitor, action, agent, marker, session_name,
             session_backend, session_cwd, session_owner,
             now.isoformat(), now.isoformat()),
        )
        return self.work_for(uid, state_key, action)  # type: ignore[return-value]

    @synchronised
    def claim_work(self, uid: str, state_key: str, monitor: str, action: str,
                   agent: str, session_backend: str = "",
                   session_cwd: str = "", session_name: str | None = None,
                   session_owner: str = "",
                   ) -> WorkSession | None:
        """Claim a fresh session without replacing work another process owns."""
        now = utcnow().isoformat()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            live = self.db.execute(
                "SELECT 1 FROM work_sessions WHERE uid=? AND action=?"
                " AND status IN ('starting','active') LIMIT 1",
                (uid, action),
            ).fetchone()
            if live is not None:
                self.db.execute("COMMIT")
                return None
            self.db.execute(
                "INSERT INTO work_sessions"
                " (uid,state_key,monitor,action,agent,status,marker,session_name,"
                " session_backend,session_cwd,session_owner,started_at,updated_at)"
                " VALUES (?,?,?,?,?,'starting','',?,?,?,?,?,?)"
                " ON CONFLICT(uid,state_key,action) DO UPDATE SET"
                " monitor=excluded.monitor, agent=excluded.agent, status='starting',"
                " marker='', session_name=NULL, session_backend=excluded.session_backend,"
                " session_cwd=excluded.session_cwd, session_owner=excluded.session_owner,"
                " started_at=excluded.started_at,"
                " updated_at=excluded.updated_at",
                (uid, state_key, monitor, action, agent, session_name,
                 session_backend, session_cwd, session_owner, now, now),
            )
            row = self.db.execute(
                "SELECT * FROM work_sessions WHERE uid=? AND state_key=? AND action=?",
                (uid, state_key, action),
            ).fetchone()
            self.db.execute("COMMIT")
            return self._work(row)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    @synchronised
    def retire_work_if_current(self, work: WorkSession,
                               status: str = "finished") -> WorkSession | None:
        """Retire a stale row only if no newer launcher has replaced it."""
        current_name = work.session_name
        condition = "session_name IS NULL" if current_name is None else "session_name=?"
        params: tuple[Any, ...] = (status, utcnow().isoformat(), work.uid,
                                   work.state_key, work.action)
        if current_name is not None:
            params += (current_name,)
        cur = self.db.execute(
            "UPDATE work_sessions SET status=?, updated_at=?"
            " WHERE uid=? AND state_key=? AND action=?"
            " AND status IN ('starting','active') AND " + condition,
            params,
        )
        if not cur.rowcount:
            return None
        return self.work_for(work.uid, work.state_key, work.action)

    @synchronised
    def set_work_status(self, uid: str, state_key: str, action: str,
                        status: str) -> WorkSession | None:
        self.db.execute(
            "UPDATE work_sessions SET status=?, updated_at=?"
            " WHERE uid=? AND state_key=? AND action=?",
            (status, utcnow().isoformat(), uid, state_key, action),
        )
        row = self.db.execute(
            "SELECT * FROM work_sessions WHERE uid=? AND state_key=? AND action=?",
            (uid, state_key, action),
        ).fetchone()
        return self._work(row)

    @synchronised
    def set_work_session_name(self, uid: str, state_key: str, action: str,
                              session_name: str, session_backend: str = "",
                              session_cwd: str = "",
                              session_owner: str = "") -> WorkSession | None:
        self.db.execute(
            "UPDATE work_sessions SET session_name=?, session_backend=?, session_cwd=?,"
            " session_owner=?, status='active', updated_at=?"
            " WHERE uid=? AND state_key=? AND action=?",
            (session_name, session_backend, session_cwd, session_owner,
             utcnow().isoformat(), uid, state_key, action),
        )
        return self.work_for(uid, state_key, action)

    @synchronised
    def work_for(self, uid: str, state_key: str, action: str) -> WorkSession | None:
        row = self.db.execute(
            "SELECT * FROM work_sessions WHERE uid=? AND state_key=? AND action=?",
            (uid, state_key, action),
        ).fetchone()
        return self._work(row)

    @synchronised
    def unfinished_work_for(self, uid: str, action: str | None = None) -> WorkSession | None:
        sql = (
            "SELECT * FROM work_sessions WHERE uid=?"
            " AND status IN ('starting','active')"
        )
        params: tuple[str, ...] = (uid,)
        if action is not None:
            sql += " AND action=?"
            params += (action,)
        row = self.db.execute(sql + " ORDER BY updated_at DESC LIMIT 1", params).fetchone()
        return self._work(row)

    @synchronised
    def unfinished_work(self) -> list[WorkSession]:
        rows = self.db.execute(
            "SELECT * FROM work_sessions WHERE status IN ('starting','active')"
        ).fetchall()
        return [work for row in rows if (work := self._work(row)) is not None]

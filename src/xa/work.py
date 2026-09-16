"""Lifecycle of agent sessions launched from actionable items."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .model import Item, WorkSession, parse_ts, utcnow
from .sessions import SessionError, session_names
from .store import Store


ACTIVE = ("starting", "active")


def _stored_work(row) -> WorkSession | None:
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


def _work_for(store: Store, uid: str, state_key: str, action: str) -> WorkSession | None:
    """Read work through Store's stable SQL boundary.

    `xa open` can remain alive while the engine is edited. Avoid calling helper
    methods whose Python signatures may have changed underneath that old
    process; the work_sessions schema is the durable interface here.
    """
    row = store.execute(
        "SELECT * FROM work_sessions WHERE uid=? AND state_key=? AND action=?",
        (uid, state_key, action),
    ).fetchone()
    return _stored_work(row)


def _unfinished_work_for(store: Store, uid: str, action: str) -> WorkSession | None:
    row = store.execute(
        "SELECT * FROM work_sessions WHERE uid=? AND action=?"
        " AND status IN ('starting','active') ORDER BY updated_at DESC LIMIT 1",
        (uid, action),
    ).fetchone()
    return _stored_work(row)


def marker_state(work: WorkSession) -> str | None:
    if not work.marker:
        return None
    try:
        state = Path(work.marker).read_text().strip()
    except OSError:
        return None
    return state if state in ("starting", "active", "finished") else None


def tmux_alive(session_name: str) -> bool:
    socket = Path(os.environ.get(
        "AI_TMUX_SOCKET", "~/.local/state/ai-tmux/tmux.sock"
    )).expanduser()
    if not socket.exists():
        return False
    try:
        return subprocess.run(
            ["tmux", "-S", str(socket), "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except FileNotFoundError:
        return False


def adopted_session_alive(session_name: str) -> bool:
    """Check a session adopted before xa supplied a lifecycle marker."""
    if session_name.startswith("pid:"):
        try:
            os.kill(int(session_name.removeprefix("pid:")), 0)
        except (OSError, ValueError):
            return False
        return True
    return tmux_alive(session_name)


def reconcile(store: Store) -> list[WorkSession]:
    """Refresh live state and return sessions that have just finished."""
    finished: list[WorkSession] = []
    registries: dict[str, set[str] | None] = {}
    for work in store.unfinished_work():
        if (
            work.status == "starting"
            and not work.session_name
            and not work.marker
            and (utcnow() - work.started_at).total_seconds() >= 60
        ):
            store.retire_work_if_current(work, "failed")
            continue
        state = marker_state(work)
        if state == "active" and work.status != "active":
            store.set_work_status(work.uid, work.state_key, work.action, "active")
            continue
        if state == "finished":
            done = store.set_work_status(
                work.uid, work.state_key, work.action, "finished"
            )
            if done is not None:
                finished.append(done)
            continue

        if work.session_backend == "ai-tmux" and work.session_name and work.session_cwd:
            registry = work.session_registry
            if registry not in registries:
                try:
                    registries[registry] = session_names(Path(registry))
                except SessionError:
                    # Unknown is not finished. Losing a resumable session is
                    # worse than retaining a row until the helper recovers.
                    registries[registry] = None
            names = registries[registry]
            if names is None:
                continue
            if work.session_name in names:
                if work.status != "active":
                    store.set_work_status(work.uid, work.state_key, work.action, "active")
            elif work.status == "active":
                done = store.set_work_status(
                    work.uid, work.state_key, work.action, "finished"
                )
                if done is not None:
                    finished.append(done)
            continue

        # A tmux name or direct-process PID is used for sessions adopted before
        # lifecycle markers existed. Once observed as active, its disappearance
        # is the same clean signal as the future marker's `finished` state.
        if work.session_name:
            if adopted_session_alive(work.session_name):
                if work.status != "active":
                    store.set_work_status(
                        work.uid, work.state_key, work.action, "active"
                    )
            elif work.status == "active":
                done = store.set_work_status(
                    work.uid, work.state_key, work.action, "finished"
                )
                if done is not None:
                    finished.append(done)
    return finished


def attach(items: list[Item], store: Store) -> None:
    for item in items:
        by_action: dict[str, WorkSession] = {}
        for action in item.obs.actions:
            exact = _work_for(store, item.uid, item.obs.state_key, action)
            if exact is None or exact.status == "failed":
                live = _unfinished_work_for(store, item.uid, action)
                if live is not None:
                    exact = live
            if exact is not None:
                by_action[action] = exact
        item.work = {}
        for action, work in by_action.items():
            item.work[action] = work.to_json()
            item.work[action]["state_changed"] = work.state_key != item.obs.state_key

"""Fresh-process finalization for long-running action sessions."""

from __future__ import annotations

import argparse

from . import config as config_mod
from .collect import collect, snapshot_from_store, write_snapshot
from .model import utcnow
from .store import Store


def finish(
    uid: str,
    state_key: str,
    action: str,
    monitor: str,
    session_name: str,
    status: str,
    *,
    cfg=None,
    store: Store | None = None,
) -> bool:
    """Finish only the exact process record that originally launched."""
    cfg = cfg or config_mod.load()
    store = store or Store(config_mod.state_dir() / "xa.db")
    updated = store.execute(
        "UPDATE work_sessions SET status=?, updated_at=?"
        " WHERE uid=? AND state_key=? AND action=? AND session_name=?",
        (status, utcnow().isoformat(), uid, state_key, action, session_name),
    ).rowcount
    if not updated:
        # A newer launch has replaced this record. Its lifecycle wins; an old
        # process exiting must not finish the new session.
        return False

    if status == "finished" and monitor in cfg.monitors:
        snapshot = collect(cfg, store, only=[monitor], force=True)
    else:
        snapshot = snapshot_from_store(cfg, store)
    write_snapshot(snapshot, config_mod.cache_dir() / "snapshot.json")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("uid")
    parser.add_argument("state_key")
    parser.add_argument("action")
    parser.add_argument("monitor")
    parser.add_argument("session_name")
    parser.add_argument("status", choices=["finished", "failed"])
    args = parser.parse_args(argv)
    finish(
        args.uid, args.state_key, args.action, args.monitor,
        args.session_name, args.status,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

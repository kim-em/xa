"""The collector.

Runs monitors on their own schedules and rewrites the snapshot. Everything
expensive happens here so that nothing expensive happens when the user asks.

Monitors run one at a time. Serial execution is not an oversight: fanning out
concurrently against the same APIs produced TLS handshake failures and invented
outages during development, and a monitoring tool that falls over under its own
load is worse than none.

One machine collects and that machine is the one you read on, so this is a
plain loop over a local sqlite database. There is no server here and nothing to
ship anywhere.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta
from pathlib import Path

from . import config as config_mod
from .collect import collect, investigate_pending, write_snapshot
from .jobs import run_due_jobs
from .store import Store

log = logging.getLogger("xa.daemon")

# How often to look for work. Individual monitors have their own intervals; this
# is only the granularity at which they are noticed.
TICK = timedelta(seconds=30)


def collector(cfg, store: Store, snapshot_path: Path, once: bool = False) -> None:
    while True:
        try:
            job_results = run_due_jobs(cfg)
            job_recheck = {
                spec.monitor
                for result in job_results
                if (spec := cfg.jobs.get(result["name"])) is not None and spec.monitor
            }
            recheck = sorted(job_recheck)
            snapshot = collect(
                cfg, store,
                only=recheck or None,
                force=bool(recheck),
                on_progress=lambda s: write_snapshot(s, snapshot_path),
            )
            write_snapshot(snapshot, snapshot_path)
            for result in job_results:
                log.info("job %s: %s", result["name"],
                         "ok" if result["ok"] else result["summary"])
            log.info("snapshot: %d item(s), %d fault(s)", len(snapshot.items), snapshot.count)

            # After publishing, so an investigation never delays the picture.
            investigated = investigate_pending(cfg, store, snapshot)
            if investigated:
                write_snapshot(snapshot, snapshot_path)
                log.info("attached %d investigation plan(s)", investigated)
        except Exception:
            # The collector must outlive any single failure. A monitor that
            # crashes is already reported as `unknown`; the loop dying would
            # take everything else with it and look like silence.
            log.exception("collection failed")
        if once:
            return
        time.sleep(TICK.total_seconds())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xa-daemon", description="collect for xa")
    p.add_argument("--once", action="store_true", help="collect once and exit")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from .cli import db_path, snapshot_path

    cfg = config_mod.load()
    store = Store(db_path())
    if not cfg.monitors:
        log.error("no monitors configured in %s/config.toml", cfg.root)
        return 1

    log.info("collecting for policy %s", cfg.root)
    try:
        collector(cfg, store, snapshot_path(), once=args.once)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

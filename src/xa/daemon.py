"""The collector.

Runs monitors on their own schedules and rewrites the snapshot. Everything
expensive happens here so that nothing expensive happens when the user asks.

Monitors run one at a time, in a worker thread so the HTTP server stays
responsive. Serial execution is not an oversight: fanning out concurrently
against the same APIs produced TLS handshake failures and invented outages
during development, and a monitoring tool that falls over under its own load is
worse than none.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from . import config as config_mod
from .collect import collect, due, investigate_pending, run_monitor, write_snapshot
from .model import utcnow
from .store import Store

log = logging.getLogger("xa.daemon")

# How often to look for work. Individual monitors have their own intervals; this
# is only the granularity at which they are noticed.
TICK = timedelta(seconds=30)


async def collector(cfg: Config, store: Store, snapshot_path: Path, once: bool = False) -> None:  # type: ignore[name-defined]
    loop = asyncio.get_running_loop()
    while True:
        try:
            snapshot = await loop.run_in_executor(
                None,
                lambda: collect(cfg, store, on_progress=lambda s: write_snapshot(s, snapshot_path)),
            )
            write_snapshot(snapshot, snapshot_path)
            log.info("snapshot: %d item(s), %d fault(s)", len(snapshot.items), snapshot.count)

            # After publishing, so an investigation never delays the picture.
            investigated = await loop.run_in_executor(
                None, lambda: investigate_pending(cfg, store, snapshot)
            )
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
        await asyncio.sleep(TICK.total_seconds())


async def run(cfg, store, snapshot_path: Path, host: str, port: int) -> None:
    from .server import build_app

    app = build_app(cfg, store, snapshot_path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("serving on http://%s:%d, policy %s", host, port, cfg.root)
    await collector(cfg, store, snapshot_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xa-daemon", description="collect for xa")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address. There is no authentication on /act, so bind to a "
                        "tailscale address rather than 0.0.0.0 and let the tailnet be the "
                        "access control")
    p.add_argument("--port", type=int, default=8787)
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

    if args.once:
        asyncio.run(collector(cfg, store, snapshot_path(), once=True))
        return 0

    try:
        asyncio.run(run(cfg, store, snapshot_path(), args.host, args.port))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

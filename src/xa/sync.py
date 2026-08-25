"""Client-side cache refresh and outbox drain.

This is what makes reads instant on a machine that is not the collector: a tiny
agent pulls the snapshot on a timer, and `xa` reads the resulting file without
touching the network. It also drains writes that were made while the daemon was
unreachable, so an acknowledgement typed on a plane is not lost.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config as config_mod

log = logging.getLogger("xa.sync")


def outbox_path() -> Path:
    return config_mod.cache_dir() / "outbox.jsonl"


def queue_write(body: dict) -> None:
    """Record a mutation that could not be delivered."""
    path = outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(body) + "\n")


def post(url: str, payload, timeout: float = 5.0):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def drain(daemon_url: str) -> int:
    """Deliver everything queued, and only then discard it."""
    path = outbox_path()
    if not path.exists() or path.stat().st_size == 0:
        return 0
    pending = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not pending:
        return 0
    try:
        post(f"{daemon_url}/act", pending)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.debug("outbox still undeliverable: %s", exc)
        return 0
    path.write_text("")
    log.info("delivered %d queued write(s)", len(pending))
    return len(pending)


def pull(daemon_url: str, destination: Path, timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(f"{daemon_url}/snapshot", timeout=timeout) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.debug("snapshot unavailable: %s", exc)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_bytes(payload)
    tmp.replace(destination)
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xa-sync", description="keep the local xa snapshot fresh")
    p.add_argument("--url", default=None, help="daemon URL (default: from config)")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from .cli import snapshot_path

    cfg = config_mod.load()
    url = (args.url or cfg.daemon_url).rstrip("/")
    destination = snapshot_path()

    while True:
        # Drain first: a snapshot pulled before local writes land would show the
        # user their own acknowledgement being undone.
        drain(url)
        pull(url, destination)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

"""HTTP interface to a running daemon.

Reads are served from the snapshot the daemon already wrote, so this endpoint
is cheap; clients are expected to cache it locally anyway and never to consult
it on the read path. Writes are the reason it exists: an acknowledgement made
on a laptop has to reach the host doing the collecting.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import Config
from .model import utcnow
from .policy import parse_duration, parse_when
from .store import Store


def apply_write(store: Store, cfg: Config, body: dict[str, Any]) -> dict[str, Any]:
    """Apply one mutation. Shared by the HTTP route and the offline outbox, so
    a write made while disconnected takes exactly the same path when it lands."""
    op = body.get("op")

    if op in ("ack", "snooze", "mute"):
        uid = body["uid"]
        state_key = body.get("state_key", "*")
        if op == "mute":
            state_key, until = "*", None
        elif op == "ack":
            until = utcnow() + cfg.ack_expiry
        else:
            until = parse_when(body.get("when"), hour=cfg.snooze_hour)
        disposition = {"ack": "acked", "snooze": "snoozed", "mute": "muted"}[op]
        store.suppress(uid, state_key, disposition, until, body.get("note", ""))
        return {"ok": True, "uid": uid, "disposition": disposition,
                "until": until.isoformat() if until else None}

    if op == "unmute":
        return {"ok": True, "removed": store.unsuppress(body["uid"])}

    if op == "mode":
        store.set_mode(body["monitor"], body["mode"])
        return {"ok": True, "monitor": body["monitor"], "mode": body["mode"]}

    if op == "threshold":
        if body.get("value") is None:
            store.execute("DELETE FROM overrides WHERE monitor=? AND name=?",
                          (body["monitor"], body["name"]))
            return {"ok": True, "cleared": True}
        store.set_override(body["monitor"], body["name"], body["value"])
        return {"ok": True}

    raise ValueError(f"unknown op {op!r}")


def build_app(cfg: Config, store: Store, snapshot_path: Path) -> web.Application:
    app = web.Application()

    async def snapshot(request: web.Request) -> web.Response:
        if not snapshot_path.exists():
            return web.json_response({"error": "no snapshot yet"}, status=503)
        return web.Response(body=snapshot_path.read_bytes(), content_type="application/json")

    async def act(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "expected JSON"}, status=400)
        try:
            # A batch, so an outbox that accumulated while offline drains in one
            # round trip rather than one request per queued write.
            ops = body if isinstance(body, list) else [body]
            return web.json_response({"results": [apply_write(store, cfg, o) for o in ops]})
        except (KeyError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "monitors": len(cfg.monitors),
            "snapshot_age_s": (
                (utcnow().timestamp() - snapshot_path.stat().st_mtime)
                if snapshot_path.exists() else None
            ),
        })

    app.router.add_get("/snapshot", snapshot)
    app.router.add_post("/act", act)
    app.router.add_get("/health", health)
    return app

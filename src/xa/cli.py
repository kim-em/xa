"""The `xa` command.

Reads come from a precomputed snapshot on local disk and touch nothing else:
no socket, no subprocess, no clock-consuming work. Asking must never start
work, or the whole design collapses into "wait while I go and look".

Writes are a different matter, and are allowed to be slow.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import config as config_mod
from .model import utcnow
from .policy import parse_duration, parse_when


def snapshot_path() -> Path:
    return config_mod.cache_dir() / "snapshot.json"


def db_path() -> Path:
    return config_mod.state_dir() / "xa.db"


def _load_snapshot() -> dict[str, Any]:
    from .collect import read_snapshot

    snap = read_snapshot(snapshot_path())
    if snap is None:
        return {"version": 1, "generated_at": None, "count": 0, "items": [], "monitors": []}
    return snap


def _find(snapshot: dict[str, Any], needle: str) -> dict[str, Any]:
    """Resolve a uid, a bare key, or an unambiguous prefix."""
    items = snapshot.get("items", [])
    exact = [i for i in items if i["uid"] == needle]
    if exact:
        return exact[0]
    partial = [i for i in items if needle in i["uid"]]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SystemExit(f"xa: no item matching {needle!r} (try `xa` to list them)")
    names = "\n  ".join(i["uid"] for i in partial)
    raise SystemExit(f"xa: {needle!r} is ambiguous:\n  {names}")


def _store():
    from .store import Store

    return Store(db_path())


def _mark(args, disposition: str, until_text: str | None, note: str = "") -> int:
    """Shared implementation of ack, snooze and mute."""
    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    cfg = config_mod.load()

    if disposition == "muted":
        # A false positive will never be right, so it binds to every state.
        state_key, until = "*", None
    elif disposition == "acked":
        # "I'll deal with this": silence until the failure becomes a different
        # failure, with a long but finite expiry so nothing is lost forever.
        state_key, until = item["state_key"], utcnow() + cfg.ack_expiry
    else:
        state_key = item["state_key"]
        until = parse_when(until_text, hour=cfg.snooze_hour)

    store = _store()
    store.suppress(item["uid"], state_key, disposition, until, note)

    # Update the snapshot optimistically rather than waiting for the collector
    # to publish again, so the next read reflects this within the second.
    for i in snapshot.get("items", []):
        if i["uid"] == item["uid"]:
            i["disposition"] = disposition
            i["suppressed_until"] = until.isoformat() if until else None
            i["counts"] = False
    from .collect import write_snapshot_json

    write_snapshot_json(snapshot, snapshot_path())

    when = f" until {until.astimezone().strftime('%a %d %b %H:%M')}" if until else ""
    print(f"{disposition}: {item['uid']}{when}")
    return 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    snapshot = _load_snapshot()
    if args.json:
        json.dump(snapshot, sys.stdout, indent=1)
        print()
        return 0
    from .render import render

    print(render(snapshot, show_all=args.all))
    return 0


def cmd_why(args) -> int:
    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    if args.json:
        json.dump(item, sys.stdout, indent=1)
        print()
        return 0
    from .render import render_detail

    print(render_detail(item))
    return 0


def cmd_ack(args) -> int:
    return _mark(args, "acked", None, args.note or "")


def cmd_snooze(args) -> int:
    return _mark(args, "snoozed", args.when, args.note or "")


def cmd_mute(args) -> int:
    return _mark(args, "muted", None, args.note or "")


def cmd_unmute(args) -> int:
    snapshot = _load_snapshot()
    try:
        uid = _find(snapshot, args.item)["uid"]
    except SystemExit:
        uid = args.item
    removed = _store().unsuppress(uid)
    print(f"cleared {removed} suppression(s) for {uid}")
    return 0


def cmd_suppressions(args) -> int:
    from .model import parse_ts

    now = utcnow()
    rows = [
        {"uid": s.uid, "state_key": s.state_key, "disposition": s.disposition,
         "until": s.until.isoformat() if s.until else None, "note": s.note}
        for s in _store().suppressions()
    ]
    if not rows:
        print("no suppressions")
        return 0
    if args.json:
        json.dump(rows, sys.stdout, indent=1)
        print()
        return 0
    width = max(len(r["uid"]) for r in rows)
    for r in sorted(rows, key=lambda r: r["uid"]):
        until = parse_ts(r["until"])
        when = until.astimezone().strftime("%a %d %b %H:%M") if until else "further notice"
        scope = "any state" if r["state_key"] == "*" else r["state_key"]
        live = "" if (until is None or now < until) else "  (expired)"
        print(f"{r['disposition']:<9} {r['uid']:<{width}}  {scope:<18} until {when}{live}")
    return 0


def cmd_mode(args) -> int:
    cfg = config_mod.load()
    store = _store()
    if args.mode is None:
        modes = store.modes()
        for name, spec in sorted(cfg.monitors.items()):
            print(f"{name:<24} {modes.get(name, spec.mode)}")
        return 0
    if args.mode not in ("report", "investigate", "auto"):
        raise SystemExit("xa: mode must be report, investigate or auto")
    cfg.monitor(args.monitor)  # validate the name
    if args.mode == "auto" and not cfg.autonomy_enabled:
        print("note: autonomy_enabled is false in config.toml, so `auto` stays inert", file=sys.stderr)
    store.set_mode(args.monitor, args.mode)
    print(f"{args.monitor}: mode = {args.mode}")
    return 0


def cmd_threshold(args) -> int:
    cfg = config_mod.load()
    if "." not in args.name:
        raise SystemExit("xa: expected <monitor>.<threshold>, e.g. bump-branches.alert_after")
    monitor, field = args.name.split(".", 1)
    spec = cfg.monitor(monitor)
    if field not in ("warn_after", "alert_after", "ttl", "push"):
        raise SystemExit("xa: threshold must be warn_after, alert_after, ttl or push")

    if args.value is None:
        th = spec.thresholds
        current = {"warn_after": th.warn_after, "alert_after": th.alert_after, "ttl": th.ttl, "push": th.push}
        live = (_store().overrides().get(monitor) or {}).get(field)
        print(f"{args.name} = {current[field]}" + (f"  (override: {live})" if live is not None else ""))
        return 0

    if args.value in ("default", "unset", "-"):
        _store().execute("DELETE FROM overrides WHERE monitor=? AND name=?", (monitor, field))
        print(f"{args.name} back to the config default")
        return 0

    if field == "push":
        value: Any = args.value.lower() in ("1", "true", "yes", "on")
    else:
        parse_duration(args.value)  # validate before storing
        value = args.value
    _store().set_override(monitor, field, value)
    print(f"{args.name} = {value}")
    return 0


def cmd_collect(args) -> int:
    from .collect import collect, write_snapshot

    cfg = config_mod.load()
    store = _store()
    snapshot = collect(cfg, store, only=args.monitors or None, force=args.force)
    write_snapshot(snapshot, snapshot_path())
    if args.quiet:
        return 0
    from .render import render

    print(render(snapshot.to_json()))
    return 0


def cmd_open(args) -> int:
    """Escalate an item into a working session, seeded with what we already know."""
    from .actions import ActionError, execute, plan, resolve

    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    cfg = config_mod.load()
    agent = "codex" if args.codex else ("claude" if args.claude else None)

    try:
        action = resolve(item, cfg, args.action)
        inspecting = args.dry_run or args.show_prompt
        launch = plan(item, action, cfg, agent, ensure=not inspecting)
    except ActionError as exc:
        raise SystemExit(f"xa: {exc}")

    if args.show_prompt:
        print(launch.prompt)
        return 0
    if args.dry_run:
        print(launch.describe())
        print()
        print(launch.prompt)
        return 0

    _store().log_action(item["uid"], action.id, agent or action.agent, "manual",
                        detail=" ".join(launch.command))
    print(f"{action.label} → {agent or action.agent} in {launch.cwd}")
    return execute(launch)


def cmd_investigate(args) -> int:
    """Investigate one item now, rather than waiting for a collection."""
    from datetime import timedelta

    from .investigate import run as run_one

    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    cfg = config_mod.load()

    print(f"investigating {item['uid']} ... (this runs an agent, so it is not instant)")
    result = run_one(item, cfg, timedelta(minutes=args.timeout))
    if result is None:
        raise SystemExit(f"xa: {item['uid']} offers nothing to investigate")

    _store().save_plan(result.uid, result.state_key, result.plan, result.ok)
    _store().log_action(item["uid"], "investigate", "claude", "manual")
    print()
    print(result.plan)
    return 0


def cmd_actions(args) -> int:
    cfg = config_mod.load()
    rows = [
        (f"{name}.{action.id}", action.agent, action.kind, action.label)
        for name, spec in sorted(cfg.monitors.items())
        for action in spec.actions.values()
    ]
    if not rows:
        print("no actions configured")
        return 0
    width = max(len(r[0]) for r in rows)
    for ref, agent, kind, label in rows:
        print(f"{ref:<{width}}  {agent:<7} {kind:<9} {label}")
    return 0


def cmd_config(args) -> int:
    cfg = config_mod.load()
    target = cfg.root / "config.toml"
    if args.what == "suppressions":
        target = cfg.root / "suppressions.toml"
    if args.path:
        print(target)
        return 0
    editor = os.environ.get("EDITOR", "code")
    subprocess.run([editor, str(target)])
    return 0


def cmd_prompt(args) -> int:
    """Open the markdown template an action uses, so it can be edited freely."""
    cfg = config_mod.load()
    for spec in cfg.monitors.values():
        for action in spec.actions.values():
            if action.id == args.action or f"{spec.name}.{action.id}" == args.action:
                path = cfg.resolve(action.prompt)
                if path is None:
                    raise SystemExit(f"xa: action {action.id!r} has no prompt template")
                if args.path:
                    print(path)
                    return 0
                subprocess.run([os.environ.get("EDITOR", "code"), str(path)])
                return 0
    raise SystemExit(f"xa: no action named {args.action!r}")


def cmd_help(args) -> int:
    from .help import render

    print(render(args.topic))
    return 0


def cmd_tui(args) -> int:
    from .tui import main as tui_main

    return tui_main()


def cmd_doctor(args) -> int:
    cfg = config_mod.load()
    snap = snapshot_path()
    print(f"policy dir   {cfg.root}" + ("" if cfg.root.exists() else "   (missing)"))
    print(f"config.toml  {'present' if (cfg.root / 'config.toml').exists() else 'missing'}")
    print(f"monitors     {len(cfg.monitors)} configured")
    print(f"database     {db_path()}" + ("" if db_path().exists() else "   (not created yet)"))
    print(f"snapshot     {snap}" + ("" if snap.exists() else "   (not written yet)"))
    print(f"autonomy     {'enabled' if cfg.autonomy_enabled else 'disabled'}")
    missing = [
        f"{n}: {s.exec}"
        for n, s in cfg.monitors.items()
        if s.kind == "exec" and (cfg.resolve(s.exec) is None or not cfg.resolve(s.exec).exists())
    ]
    if missing:
        print("\nmissing monitor executables:")
        for m in missing:
            print(f"  {m}")

    # An item nobody can act on is a notification, not an alert, and a surface
    # full of them is one you stop reading. Faults and pending decisions must
    # always offer something; backlogs are metrics and may not.
    snapshot = _load_snapshot()
    unactionable = [
        i for i in snapshot.get("items", [])
        if i["kind"] in ("fault", "pending") and not i.get("actions")
    ]
    if unactionable:
        print(f"\n{len(unactionable)} item(s) with no action (each is a dead end):")
        for i in unactionable:
            print(f"  [{i['kind']:7}] {i['uid']}")
    else:
        print("\nevery reported fault and pending decision offers an action")

    dangling = []
    for i in snapshot.get("items", []):
        spec = cfg.monitors.get(i["monitor"])
        for a in i.get("actions") or []:
            if spec is not None and a not in spec.actions and a != "fix-monitor":
                dangling.append((i["uid"], a))
    if dangling:
        print("\nitems offering actions that are not configured:")
        for uid, a in dangling:
            print(f"  {uid} -> {a}")
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xa", description="external amygdala: what is on fire")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--all", action="store_true", help="include acked, snoozed and muted items")
    sub = p.add_subparsers(dest="command")

    def add(name, fn, help_text, **kw):
        sp = sub.add_parser(name, help=help_text, **kw)
        sp.set_defaults(func=fn)
        return sp

    s = add("why", cmd_why, "everything known about one item")
    s.add_argument("item")
    s.add_argument("--json", action="store_true")

    s = add("ack", cmd_ack, "I'll deal with this; quiet until the failure changes")
    s.add_argument("item")
    s.add_argument("--note", default="")

    s = add("snooze", cmd_snooze, "tell me again later (default: tomorrow morning)")
    s.add_argument("item")
    s.add_argument("when", nargs="?", default="tomorrow", help="3h, 2d, tomorrow, mon, next week, 2026-09-01")
    s.add_argument("--note", default="")

    s = add("mute", cmd_mute, "false positive; never mention this again")
    s.add_argument("item")
    s.add_argument("--note", default="")

    s = add("unmute", cmd_unmute, "clear every suppression on an item")
    s.add_argument("item")

    s = add("suppressions", cmd_suppressions, "list acks, snoozes and mutes")
    s.add_argument("--json", action="store_true")

    s = add("mode", cmd_mode, "switch a monitor between report, investigate and auto")
    s.add_argument("monitor", nargs="?")
    s.add_argument("mode", nargs="?")

    s = add("threshold", cmd_threshold, "read or set a threshold, e.g. bump-branches.alert_after 6h")
    s.add_argument("name")
    s.add_argument("value", nargs="?", help="a duration, or `default` to clear an override")

    s = add("collect", cmd_collect, "run monitors now and rewrite the snapshot")
    s.add_argument("monitors", nargs="*")
    s.add_argument("--force", action="store_true", help="ignore each monitor's interval")
    s.add_argument("--quiet", action="store_true")

    s = add("open", cmd_open, "escalate an item into an agent session")
    s.add_argument("item")
    s.add_argument("action", nargs="?", help="which action, if the item offers several")
    s.add_argument("--codex", action="store_true", help="use Codex instead of the configured agent")
    s.add_argument("--claude", action="store_true", help="use Claude instead of the configured agent")
    s.add_argument("--dry-run", action="store_true", help="print the command and prompt, launch nothing")
    s.add_argument("--show-prompt", action="store_true", help="print just the rendered prompt")

    s = add("investigate", cmd_investigate,
            "have an agent work out what is wrong, without fixing it")
    s.add_argument("item")
    s.add_argument("--timeout", type=int, default=10, help="minutes")

    add("actions", cmd_actions, "list the actions each monitor offers")

    s = add("config", cmd_config, "open the policy files")
    s.add_argument("what", nargs="?", default="config", choices=["config", "suppressions"])
    s.add_argument("--path", action="store_true", help="print the path instead of opening it")

    s = add("prompt", cmd_prompt, "open an action's prompt template")
    s.add_argument("action")
    s.add_argument("--path", action="store_true")

    s = add("help", cmd_help, "how xa works (not just what its flags are)")
    s.add_argument("topic", nargs="?",
                   help="model, snooze, actions, monitors, thresholds, autonomy")

    add("tui", cmd_tui, "interactive view: same verbs, one keystroke each")
    add("doctor", cmd_doctor, "check the installation")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        return cmd_status(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""The `xa` command.

Reads come from a precomputed snapshot on local disk and touch nothing else:
no socket, no subprocess, no clock-consuming work. Asking must never start
work, or the whole design collapses into "wait while I go and look".

Writes are a different matter, and are allowed to be slow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import config as config_mod
from .model import parse_ts, utcnow
from .policy import humanise, parse_duration, parse_when


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
    members = [
        member
        for item in items
        for member in (item.get("evidence") or {}).get("cluster_members", [])
        if member.get("uid")
    ]
    candidates = [*items, *members]
    exact = [i for i in candidates if i["uid"] == needle]
    if exact:
        return exact[0]
    partial = [i for i in candidates if needle in i["uid"]]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SystemExit(f"xa: no item matching {needle!r} (try `xa` to list them)")
    names = "\n  ".join(i["uid"] for i in partial)
    raise SystemExit(f"xa: {needle!r} is ambiguous:\n  {names}")


def _store():
    from .store import Store

    return Store(db_path())


def _work_marker(uid: str, state_key: str, action: str) -> Path:
    token = hashlib.sha256(f"{uid}\0{state_key}\0{action}".encode()).hexdigest()[:20]
    return config_mod.state_dir() / "work" / f"{token}.state"


def _publish_work(snapshot: dict[str, Any], uid: str, work) -> None:
    for item in snapshot.get("items", []):
        if item["uid"] == uid:
            item.setdefault("work", {})[work.action] = work.to_json()
    from .collect import write_snapshot_json

    write_snapshot_json(snapshot, snapshot_path())


def _finalize_work(
    uid: str, state_key: str, action: str, monitor: str,
    session_name: str, status: str,
) -> None:
    """Finalize long-running work in a coherent, new interpreter."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "xa.finish_work",
            uid, state_key, action, monitor, session_name, status,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "finalization failed").strip()
        raise RuntimeError(f"could not finish work on {uid}: {detail}")


def _mark(args, disposition: str, until_text: str | None, note: str = "") -> int:
    """Shared implementation of ack, snooze and mute."""
    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    cfg = config_mod.load()

    if (
        disposition == "muted"
        and item.get("cluster_key")
        and (item.get("evidence") or {}).get("cluster_members")
    ):
        raise SystemExit(
            f"xa: {item['uid']} is a summary; mute one of the members listed by"
            f" `xa why {item['uid']}`"
        )

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
    cluster_parent = None
    for i in snapshot.get("items", []):
        if i["uid"] == item["uid"]:
            i["disposition"] = disposition
            i["suppressed_until"] = until.isoformat() if until else None
            i["counts"] = False
        members = (i.get("evidence") or {}).get("cluster_members", [])
        if any(member.get("uid") == item["uid"] for member in members):
            cluster_parent = i

    if cluster_parent is not None:
        remaining = [
            member
            for member in cluster_parent["evidence"]["cluster_members"]
            if member.get("uid") != item["uid"]
        ]
        if not remaining:
            snapshot["items"] = [
                existing
                for existing in snapshot.get("items", [])
                if existing is not cluster_parent
            ]
        else:
            representative = remaining[0]
            cluster_parent["cluster_size"] = len(remaining)
            cluster_parent["title"] = cluster_parent["cluster_title"].format(
                count=len(remaining)
            )
            metric = cluster_parent.get("cluster_metric")
            if metric:
                cluster_parent.setdefault("metrics", {})[metric] = len(remaining)
            cluster_parent["state_key"] = hashlib.sha256(
                "\x1f".join(sorted(m["state_key"] for m in remaining)).encode()
            ).hexdigest()[:16]
            for field in ("detail", "since", "url", "links"):
                cluster_parent[field] = representative.get(field)
            evidence = dict(representative.get("evidence") or {})
            evidence["cluster_members"] = remaining
            cluster_parent["evidence"] = evidence
    snapshot["count"] = sum(
        1
        for existing in snapshot.get("items", [])
        if existing.get("counts") and existing.get("disposition", "active") == "active"
    )
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

    print(render_detail(item, actions_taken=_store().recent_actions(item["uid"])))
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
    store = _store()
    removed = store.unsuppress(uid)
    if removed:
        monitor = uid.partition("/")[0]
        if monitor in config_mod.load().monitors:
            # A monitor may have pushed this mute upstream and skipped the
            # expensive check. Make it due again rather than exposing a cheap
            # placeholder or waiting out a long interval.
            store.invalidate_report(monitor)
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


def cmd_run(args) -> int:
    """Explicitly run a mutating job, then refresh its read-only health monitor."""
    from .collect import collect, write_snapshot
    from .jobs import JobBusy, run_job

    cfg = config_mod.load()
    try:
        spec = cfg.job(args.job)
    except KeyError as exc:
        names = ", ".join(sorted(cfg.jobs)) or "none configured"
        raise SystemExit(f"xa: {exc.args[0]} (jobs: {names})") from None
    try:
        status = run_job(spec, cfg)
    except JobBusy as exc:
        raise SystemExit(f"xa: {exc}") from None

    if spec.monitor and spec.monitor in cfg.monitors:
        snapshot = collect(cfg, _store(), only=[spec.monitor], force=True)
        write_snapshot(snapshot, snapshot_path())
    if args.json:
        json.dump(status, sys.stdout, indent=1)
        print()
    else:
        outcome = "completed" if status["ok"] else "failed"
        print(f"{spec.name}: {outcome} — {status['summary']}")
        for failure in (status.get("details") or {}).get("failures") or []:
            print(
                f"  {failure.get('host', '?')} {failure.get('store', '?')}: "
                f"{failure.get('error', 'failed')}"
            )
    return 0 if status["ok"] else 1


def cmd_refresh(args) -> int:
    """Force one monitor now and summarize how its visible result changed."""
    from .collect import collect, write_snapshot

    cfg = config_mod.load()
    before = _load_snapshot()
    if args.target in cfg.monitors:
        monitor = args.target
    else:
        try:
            monitor = _find(before, args.target)["monitor"]
        except SystemExit:
            raise SystemExit(
                f"xa: no monitor or item matching {args.target!r}"
            ) from None

    snapshot = collect(cfg, _store(), only=[monitor], force=True)
    write_snapshot(snapshot, snapshot_path())
    after = snapshot.to_json()

    def states(payload: dict[str, Any]) -> dict[str, str]:
        return {
            item["uid"]: item["state_key"]
            for item in payload.get("items", [])
            if item["monitor"] == monitor
        }

    old, new = states(before), states(after)
    cleared = sorted(old.keys() - new.keys())
    appeared = sorted(new.keys() - old.keys())
    changed = sorted(uid for uid in old.keys() & new.keys() if old[uid] != new[uid])

    print(f"{monitor}: refreshed")
    for uid in cleared:
        print(f"  cleared  {uid}")
    for uid in appeared:
        print(f"  new      {uid}")
    for uid in changed:
        print(f"  changed  {uid}")
    if not (cleared or appeared or changed):
        print("  no visible change")
    return 0


def cmd_open(args) -> int:
    """Escalate an item into a working session, seeded with what we already know."""
    from .actions import ActionError, execute, plan, resolve
    from .sessions import SessionError, attach as attach_session
    from .sessions import create as create_session
    from .sessions import describe_new, is_registered, new_name, owner_folder
    from .work import adopted_session_alive

    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    cfg = config_mod.load()
    agent = "codex" if args.codex else ("claude" if args.claude else None)

    store = _store()
    # From the store rather than the snapshot: it is authoritative, it is on
    # this machine, and an investigation that finished since the last
    # collection should still reach the session it was produced for.
    investigation = store.stored_plan(item["uid"], item["state_key"])

    try:
        action = resolve(item, cfg, args.action)
        previous = store.work_for(item["uid"], item["state_key"], action.id)
        if previous is None or previous.status == "failed":
            previous = store.unfinished_work_for(item["uid"], action.id)
        reopening = (
            previous is not None
            and previous.action == action.id
            and previous.status != "failed"
            and (
                action.kind == "escalate"
                or action.kind == "session" and previous.status in ("starting", "active")
            )
        )
        chosen_agent = (agent or previous.agent) if reopening else (agent or action.agent)
        inspecting = args.dry_run or args.show_prompt
        marker = Path(previous.marker) if reopening and previous.marker else _work_marker(
            item["uid"], item["state_key"], action.id
        )
        launch = plan(item, action, cfg, chosen_agent if reopening else agent,
                      ensure=not inspecting,
                      investigation=investigation, resume=reopening,
                      lifecycle=marker if action.kind == "escalate" and not reopening else None)

        if args.show_prompt:
            print(launch.prompt)
            return 0

        if action.kind == "session" and reopening:
            age = (utcnow() - previous.started_at).total_seconds()
            if previous.session_backend == "ai-tmux" and previous.session_name:
                try:
                    registered = is_registered(
                        previous.session_name, Path(previous.session_registry)
                    )
                except SessionError as exc:
                    raise ActionError(f"could not verify the running session: {exc}") from exc
                if not registered:
                    if previous.status == "starting" and age < 60:
                        raise ActionError("the session is still starting; try again shortly")
                    if not inspecting:
                        retired = store.retire_work_if_current(previous)
                        previous = retired or store.work_for(
                            previous.uid, previous.state_key, previous.action
                        )
                    reopening = False
            elif not previous.session_name:
                if age < 60:
                    raise ActionError("the session is still starting; try again shortly")
                if not inspecting:
                    retired = store.retire_work_if_current(previous, "failed")
                    previous = retired or store.work_for(
                        previous.uid, previous.state_key, previous.action
                    )
                reopening = False
            elif not adopted_session_alive(previous.session_name):
                if not inspecting:
                    retired = store.retire_work_if_current(previous)
                    previous = retired or store.work_for(
                        previous.uid, previous.state_key, previous.action
                    )
                reopening = False
            else:
                raise ActionError(
                    "this legacy direct session is still running but cannot be reattached; "
                    "wait for it to finish before starting a durable session"
                )
    except ActionError as exc:
        raise SystemExit(f"xa: {exc}")

    if args.dry_run:
        if action.kind == "session":
            if reopening and previous is not None:
                print(
                    f"cd {shlex.quote(previous.session_cwd)} && ai-tmux attach "
                    f"{shlex.quote(previous.session_name or '')} --folder "
                    f"{shlex.quote(previous.session_registry)}"
                )
            else:
                try:
                    print(describe_new(chosen_agent, launch.cwd,
                                       owner=owner_folder()))
                except SessionError as exc:
                    raise SystemExit(f"xa: {exc}")
        else:
            print(launch.describe())
        print()
        print(launch.prompt)
        return 0

    if action.kind == "session":
        if reopening and previous is not None:
            work = previous
            session_cwd = Path(work.session_cwd)
            registry = Path(work.session_registry)
            _publish_work(snapshot, item["uid"], work)
            print(f"Reopening {action.label.lower()} → {work.agent} in {session_cwd}")
        else:
            session_cwd = launch.cwd
            # Where the agent works and which window reopens its tab are two
            # different folders whenever the item's repository is not the one
            # you are sitting in. ai-tmux keys the registry by the second.
            registry = owner_folder()
            name = new_name()
            work = store.claim_work(
                item["uid"], item["state_key"], item["monitor"], action.id,
                chosen_agent, session_backend="ai-tmux", session_cwd=str(launch.cwd),
                session_name=name, session_owner=str(registry),
            )
            if work is None:
                raise SystemExit("xa: this session is already starting; try again shortly")
            _publish_work(snapshot, item["uid"], work)
            try:
                create_session(
                    chosen_agent, name, launch.cwd, launch.prompt,
                    config_mod.state_dir(), owner=registry,
                )
            except SessionError as exc:
                failed = store.set_work_status(
                    item["uid"], item["state_key"], action.id, "failed"
                )
                if failed is not None:
                    _publish_work(snapshot, item["uid"], failed)
                raise SystemExit(f"xa: {exc}")
            work = store.set_work_session_name(
                item["uid"], item["state_key"], action.id, name,
                session_backend="ai-tmux", session_cwd=str(launch.cwd),
                session_owner=str(registry),
            )
            if work is None:
                raise SystemExit("xa: the new session could not be recorded")
            store.log_action(
                item["uid"], action.id, chosen_agent, "manual",
                detail=f"ai-tmux attach {name} --folder {registry}",
            )
            _publish_work(snapshot, item["uid"], work)
            print(f"{action.label} → {chosen_agent} in {launch.cwd}")

        try:
            rc = attach_session(work.session_name or "", registry)
            registered = is_registered(work.session_name or "", registry)
        except SessionError as exc:
            raise SystemExit(f"xa: {exc}")
        if not registered:
            _finalize_work(
                work.uid, work.state_key, work.action, work.monitor,
                work.session_name or "", "finished" if rc == 0 else "failed",
            )
        return rc

    if reopening:
        work = previous
        if work.status == "finished":
            if work.marker:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("starting\n")
            work = store.set_work_status(
                previous.uid, previous.state_key, previous.action, "starting"
            )
        _publish_work(snapshot, item["uid"], work)
        print(f"Reopening {action.label.lower()} → {work.agent} in {launch.cwd}")
        rc = execute(launch)
        if rc != 0:
            failed = store.set_work_status(
                previous.uid, previous.state_key, previous.action, "failed"
            )
            _publish_work(snapshot, item["uid"], failed)
        return rc

    marker_text = str(marker) if action.kind == "escalate" else ""
    if marker_text:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("starting\n")
    work = store.start_work(
        item["uid"], item["state_key"], item["monitor"], action.id,
        chosen_agent, marker=marker_text,
    )
    if action.kind != "escalate":
        work = store.set_work_status(
            item["uid"], item["state_key"], action.id, "active"
        )
    _publish_work(snapshot, item["uid"], work)
    store.log_action(item["uid"], action.id, chosen_agent, "manual",
                     detail=" ".join(launch.command))
    print(f"{action.label} → {chosen_agent} in {launch.cwd}")
    rc = execute(launch)
    if rc != 0:
        work = store.set_work_status(
            item["uid"], item["state_key"], action.id, "failed"
        )
        _publish_work(snapshot, item["uid"], work)
    return rc


def cmd_investigate(args) -> int:
    """Investigate one item now, rather than waiting for a collection."""
    from datetime import timedelta

    from .investigate import RETRY_FAILED_AFTER, run as run_one

    snapshot = _load_snapshot()
    item = _find(snapshot, args.item)
    cfg = config_mod.load()

    print(f"investigating {item['uid']} ... (this runs an agent, so it is not instant)")
    result = run_one(item, cfg, timedelta(minutes=args.timeout))
    if result is None:
        raise SystemExit(f"xa: {item['uid']} offers nothing to investigate")

    store = _store()
    store.save_plan(result.uid, result.state_key, result.plan, result.ok,
                    from_agent=result.from_agent)
    store.log_action(item["uid"], "investigate", result.agent, "manual")

    # Publish rather than waiting for the collector, exactly as `_mark` does.
    # A report that only appears after the next sweep -- minutes, if a sweep is
    # running -- reads as though the investigation did not happen.
    for i in snapshot.get("items", []):
        if i["uid"] == item["uid"]:
            i["plan"], i["plan_ok"] = result.plan, result.ok
    from .collect import write_snapshot_json

    write_snapshot_json(snapshot, snapshot_path())

    print()
    print(result.plan)
    if not result.ok:
        print()
        print(f"xa: that investigation did not finish; it will be tried again "
              f"in {RETRY_FAILED_AFTER}", file=sys.stderr)
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

    # This is an easy category error: status and most verbs address item IDs,
    # while `prompt` addresses the reusable action template. Recognise the item
    # and point at both useful operations instead of pretending it is unknown.
    snapshot = _load_snapshot()
    try:
        item = _find(snapshot, args.action)
    except SystemExit:
        item = None
    if item is not None and item.get("uid") == args.action:
        uid = shlex.quote(item["uid"])
        action_ids = list(item.get("actions") or [])
        lines = [
            f"xa: {args.action!r} is an item ID; `xa prompt` expects an action name."
        ]
        if action_ids:
            lines += ["", "To print the prompt rendered for this item:"]
            several = len(action_ids) > 1
            for action_id in action_ids:
                selection = f" {shlex.quote(action_id)}" if several else ""
                lines.append(f"  xa open {uid}{selection} --show-prompt")

            spec = cfg.monitors.get(item.get("monitor", ""))
            editable = [
                action_id
                for action_id in action_ids
                if spec is not None
                and action_id in spec.actions
                and spec.actions[action_id].prompt
            ]
            if editable:
                lines += ["", "To open the editable template:"]
                for action_id in editable:
                    lines.append(f"  xa prompt {spec.name}.{action_id}")
        else:
            lines += ["", f"This item offers no action prompt. Inspect it with: xa why {uid}"]
        raise SystemExit("\n".join(lines))

    raise SystemExit(
        f"xa: no action named {args.action!r} (run `xa actions` to list action names)"
    )


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
    problems = 0
    print(f"policy dir   {cfg.root}" + ("" if cfg.root.exists() else "   (missing)"))
    print(f"config.toml  {'present' if (cfg.root / 'config.toml').exists() else 'missing'}")
    print(f"monitors     {len(cfg.monitors)} configured")
    print(f"jobs         {len(cfg.jobs)} configured")
    print(f"database     {db_path()}" + ("" if db_path().exists() else "   (not created yet)"))
    snapshot = _load_snapshot()
    generated = parse_ts(snapshot.get("generated_at"))
    snapshot_age = (utcnow() - generated).total_seconds() if generated else None
    if snapshot_age is None:
        snapshot_state = "not written yet"
        problems += 1
    elif snapshot_age > 600:
        snapshot_state = f"stale ({humanise(snapshot_age)} old)"
        problems += 1
    else:
        snapshot_state = f"fresh ({humanise(snapshot_age)} old)"
    print(f"snapshot     {snap}   ({snapshot_state})")
    print(f"autonomy     {'enabled' if cfg.autonomy_enabled else 'disabled'}")

    label = "com.kim.xa-daemon"
    try:
        service = subprocess.run(
            ["launchctl", "print", f"system/{label}"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        service = None
    service_running = (
        service is not None
        and service.returncode == 0
        and "state = running" in service.stdout
        and "pid = " in service.stdout
    )
    print(f"daemon       {'running (system)' if service_running else 'not running'}")
    if not service_running:
        problems += 1

    try:
        legacy = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except FileNotFoundError:
        legacy = False
    if legacy:
        print("legacy agent present   (remove the GUI-domain collector)")
        problems += 1

    has_sessions = any(
        action.kind == "session"
        for spec in cfg.monitors.values()
        for action in spec.actions.values()
    )
    if has_sessions:
        from .sessions import SessionError, helper_path

        try:
            helper = helper_path()
            helper_state = helper
        except SessionError as exc:
            helper_state = f"missing ({exc})"
            problems += 1
        print(f"session tool {helper_state}")
        tmux = shutil.which("tmux")
        print(f"tmux         {tmux or 'missing'}")
        if tmux is None:
            problems += 1
    missing = [
        f"{n}: {s.exec}"
        for n, s in cfg.monitors.items()
        if s.kind == "exec" and (cfg.resolve(s.exec) is None or not cfg.resolve(s.exec).exists())
    ]
    if missing:
        print("\nmissing monitor executables:")
        for m in missing:
            print(f"  {m}")
    missing_jobs = [
        f"{n}: {s.exec}" for n, s in cfg.jobs.items()
        if cfg.resolve(s.exec) is None or not cfg.resolve(s.exec).exists()
    ]
    if missing_jobs:
        print("\nmissing job executables:")
        for job in missing_jobs:
            print(f"  {job}")

    # An item nobody can act on is a notification, not an alert, and a surface
    # full of them is one you stop reading. Faults and pending decisions must
    # always offer something; backlogs are metrics and may not.
    unactionable = [
        i for i in snapshot.get("items", [])
        if i.get("disposition", "active") == "active"
        and i["kind"] in ("fault", "pending")
        and not (i.get("actions") or i.get("commands"))
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
    return 1 if problems else 0


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

    s = add("refresh", cmd_refresh, "force one monitor to check again now")
    s.add_argument("target", help="a monitor name or an item ID")

    s = add("run", cmd_run, "run a configured mutating job now")
    s.add_argument("job")

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

    s = add(
        "prompt", cmd_prompt, "open an action's editable prompt template",
        description=(
            "Open the editable template for an action. To print the prompt rendered "
            "for an item, use `xa open <item> [action] --show-prompt`."
        ),
    )
    s.add_argument("action", help="action name from `xa actions`, e.g. toolchains.bump")
    s.add_argument("--path", action="store_true", help="print the template path instead")

    s = add("help", cmd_help, "how xa works (not just what its flags are)")
    s.add_argument("topic", nargs="?",
                   help="model, snooze, actions, monitors, jobs, thresholds, autonomy")

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

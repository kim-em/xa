"""Terminal rendering.

Pure stdlib on purpose. `xa` is meant to feel instantaneous, and pulling in a
formatting framework costs more startup time than the whole read path.
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from typing import Any, Sequence

from .model import parse_ts, utcnow
from .policy import humanise

GLYPH = {"alert": "●", "warn": "◐", "unknown": "?", "info": "·"}
COLOUR = {"alert": "31", "warn": "33", "unknown": "35", "info": "90"}
DISPOSITION_MARK = {"acked": "✓", "snoozed": "z", "muted": "m", "active": " "}


def _tty() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def dim(text: str) -> str:
    return paint(text, "90")


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 24)).columns, 140))


def _row(item: dict[str, Any], now: datetime, name_w: int) -> list[str]:
    sev = item["severity"]
    glyph = paint(GLYPH.get(sev, "·"), COLOUR.get(sev, "0"))
    since = parse_ts(item.get("since"))
    age = humanise((now - since).total_seconds()) if since else "-"
    mark = DISPOSITION_MARK.get(item.get("disposition", "active"), " ")

    title = item["title"]
    if item.get("cluster_size", 1) > 1:
        title += dim(f"  (+{item['cluster_size'] - 1})")

    head = f"  {mark}{glyph} {age:>4}  {item['uid'][:name_w]:<{name_w}}  {title}"
    lines = [head]

    trailer = []
    if item.get("detail"):
        trailer.append(item["detail"])
    if item.get("actions"):
        trailer.append(dim("[" + " ".join(item["actions"]) + "]"))
    if trailer:
        lines.append(" " * (name_w + 13) + "  ".join(trailer))
    return lines


def _metrics_line(metrics: dict[str, Any]) -> str:
    parts = []
    for k, v in metrics.items():
        label = k.replace("_", " ")
        if isinstance(v, bool):
            # A flag reads as a caveat, not as a quantity: "3000 · True capped"
            # is noise, "3000+ (capped)" is information.
            if v:
                parts.append(f"({label})")
        else:
            parts.append(f"{v} {label}")
    return dim(" · ".join(parts))


def render(snapshot: dict[str, Any], show_all: bool = False) -> str:
    now = utcnow()
    generated = parse_ts(snapshot.get("generated_at"))
    all_items: Sequence[dict[str, Any]] = snapshot.get("items", [])
    items = all_items if show_all else [i for i in all_items if i.get("disposition", "active") == "active"]
    hidden = len(all_items) - len(items)

    count = sum(1 for i in items if i.get("counts"))
    age = humanise((now - generated).total_seconds()) if generated else "?"

    out: list[str] = []
    # Faults exist below the threshold too. Saying "nothing on fire" while
    # showing a list of faults reads as a contradiction, so name them: they are
    # real, they are just too young to be worth interrupting for yet.
    young = sum(1 for i in items if i["kind"] == "fault" and not i.get("counts"))
    if count:
        headline = f"{count} fault{'s' if count != 1 else ''}"
    elif young:
        headline = f"nothing urgent, {young} below threshold"
    else:
        headline = "nothing on fire"
    stale = generated is not None and (now - generated).total_seconds() > 600
    age_text = f"snapshot {age} ago"
    out.append(f"{paint('xa', '1')}  {dim('·')}  {headline}  {dim('·')}  "
               f"{paint(age_text, '31') if stale else dim(age_text)}")

    faults = [i for i in items if i["kind"] == "fault"]
    pending = [i for i in items if i["kind"] == "pending"]
    backlog = [i for i in items if i["kind"] == "backlog"]

    name_w = min(34, max([len(i["uid"]) for i in items], default=20))

    if faults:
        out.append("")
        out.append(paint(f"FAULTS ({len(faults)})", "1"))
        for i in faults:
            out.extend(_row(i, now, name_w))

    if pending:
        out.append("")
        out.append(paint(f"PENDING ({len(pending)})", "1"))
        for i in pending:
            out.extend(_row(i, now, name_w))

    if backlog:
        out.append("")
        out.append(paint("BACKLOG", "1"))
        for i in backlog:
            line = f"    {i['uid'][:name_w]:<{name_w}}  {i['title']}"
            out.append(line)
            if i.get("metrics"):
                out.append(" " * (name_w + 6) + _metrics_line(i["metrics"]))

    healthy = [
        m["name"]
        for m in snapshot.get("monitors", [])
        if m.get("ok") and not any(x["monitor"] == m["name"] and x["kind"] != "backlog" for x in all_items)
    ]
    if healthy:
        out.append("")
        out.append(dim("HEALTHY  " + " · ".join(sorted(healthy))))

    broken = [m for m in snapshot.get("monitors", []) if not m.get("ok")]
    if broken:
        out.append("")
        out.append(paint("MONITORS NOT RUNNING", "35"))
        for m in broken:
            out.append(f"    {m['name']:<24}  {dim((m.get('error') or '')[:80])}")

    if hidden and not show_all:
        out.append("")
        out.append(dim(f"  {hidden} item{'s' if hidden != 1 else ''} acked, snoozed or muted"
                       f" ({paint('xa --all', '0') if _tty() else 'xa --all'} to see them)"))

    if not all_items and not broken:
        out.append("")
        out.append(dim("  (no monitors have reported yet: run `xa collect --force`)"))

    return "\n".join(out)


def render_detail(item: dict[str, Any]) -> str:
    """Everything the check already knows, so an escalation need not re-derive it."""
    import json

    now = utcnow()
    since = parse_ts(item.get("since"))
    out = [
        paint(item["title"], "1"),
        "",
        f"  uid         {item['uid']}",
        f"  kind        {item['kind']}   severity {item['severity']}   mode {item.get('mode')}",
        f"  state_key   {item['state_key']}",
    ]
    if since:
        out.append(f"  since       {since.isoformat()}  ({humanise((now - since).total_seconds())} ago)")
    if item.get("disposition") != "active":
        until = parse_ts(item.get("suppressed_until"))
        out.append(f"  {item['disposition']:<11} until {until.isoformat() if until else 'further notice'}")
    if item.get("url"):
        out.append(f"  url         {item['url']}")
    if item.get("detail"):
        out += ["", "  " + item["detail"]]
    if item.get("metrics"):
        out += ["", paint("  metrics", "1")]
        for k, v in item["metrics"].items():
            out.append(f"    {k:<24} {v}")
    if item.get("actions"):
        out += ["", paint("  actions", "1"), "    " + " ".join(item["actions"])]
    if item.get("plan"):
        out += ["", paint("  plan", "1"), *("    " + ln for ln in item["plan"].splitlines())]
    if item.get("evidence"):
        out += ["", paint("  evidence", "1")]
        for line in json.dumps(item["evidence"], indent=2).splitlines():
            out.append("    " + line)
    return "\n".join(out)

"""Terminal rendering.

Pure stdlib on purpose. `xa` is meant to feel instantaneous, and pulling in a
formatting framework costs more startup time than the whole read path.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import textwrap
from datetime import datetime
from typing import Any, Sequence

from .model import parse_ts, utcnow
from .policy import humanise

GLYPH = {"alert": "●", "warn": "◐", "unknown": "?", "info": "·"}
COLOUR = {"alert": "31", "warn": "33", "unknown": "35", "info": "90"}
DISPOSITION_MARK = {"acked": "✓", "snoozed": "z", "muted": "m", "active": " "}


def _tty() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# Set to False to force plain output regardless of the terminal, for consumers
# that do their own styling (the TUI) or none at all (a pipe).
_COLOUR: bool | None = None


def paint(text: str, code: str) -> str:
    use = _tty() if _COLOUR is None else _COLOUR
    return f"\033[{code}m{text}\033[0m" if use else text


def dim(text: str) -> str:
    return paint(text, "90")


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 24)).columns, 140))


def _wrap_row(prefix: str, text: str, content_col: int) -> list[str]:
    """Wrap row copy with every continuation aligned under its first word."""
    chunks = textwrap.wrap(text, width=max(1, width() - content_col)) or [""]
    return [prefix + chunks[0], *(" " * content_col + chunk for chunk in chunks[1:])]


def _action_lines(item: dict[str, Any]) -> list[str]:
    """Human next steps, with IDs only where choosing between them requires one."""
    action_ids = item.get("actions") or []
    labels = item.get("action_labels") or {}
    several = len(action_ids) > 1
    lines = []
    for suggestion in item.get("commands") or []:
        command = suggestion.get("command")
        if command:
            lines.append(
                f"{suggestion.get('label') or 'Run'}: {paint(str(command), '35')}"
            )
    if item.get("why_label"):
        command = f"xa why {shlex.quote(item['uid'])}"
        lines.append(f"{item['why_label']}: {paint(command, '35')}")
    works = item.get("work") or {}
    for action_id in action_ids:
        command = f"xa open {shlex.quote(item['uid'])}"
        if several:
            command += f" {shlex.quote(action_id)}"
        label = labels.get(action_id, action_id)
        work = works.get(action_id) or {}
        if work.get("status") in ("starting", "active"):
            label = "Reopen the in-progress session"
        lines.append(f"{label}: {paint(command, '35')}")
    return lines


def _link_lines(item: dict[str, Any]) -> list[str]:
    """Labeled destinations, kept as whole logical lines for copy/paste."""
    return [
        f"{link.get('label') or 'Link'}: {paint(str(link['url']), '36')}"
        for link in item.get("links") or []
        if link.get("url")
    ]


def _summary_row(item: dict[str, Any], name_w: int) -> list[str]:
    """A backlog or status row: a headline number and what to do about it.

    These carry no age, because a backlog is a level rather than an event.
    They do carry links, for the case where a slice has shrunk to something a
    person can just read: three pull requests to merge are better shown than
    handed to a session that would list them and stop.
    """
    content_col = name_w + 6
    lines = [f"    {item['uid'][:name_w]:<{name_w}}  {item['title']}"]
    if item.get("metrics"):
        lines.append(" " * content_col + _metrics_line(item["metrics"]))
    for link in _link_lines(item):
        lines.append(" " * content_col + paint("↗ ", "36") + link)
    for action in _action_lines(item):
        lines.append(" " * content_col + paint("→ ", "32") + action)
    return lines


def _row(item: dict[str, Any], now: datetime, name_w: int) -> list[str]:
    sev = item["severity"]
    glyph = paint(GLYPH.get(sev, "·"), COLOUR.get(sev, "0"))
    since = parse_ts(item.get("since"))
    age = humanise((now - since).total_seconds()) if since else "-"
    mark = DISPOSITION_MARK.get(item.get("disposition", "active"), " ")

    title = item["title"]
    cluster_note = ""
    if item.get("cluster_size", 1) > 1 and not item.get("cluster_key"):
        cluster_note = f"(+{item['cluster_size'] - 1})"
        title += f"  {cluster_note}"

    content_col = name_w + 13
    head = f"  {mark}{glyph} {age:>4}  {item['uid'][:name_w]:<{name_w}}  "
    lines = _wrap_row(head, title, content_col)
    if cluster_note:
        lines = [line.replace(cluster_note, dim(cluster_note), 1) for line in lines]

    trailer: list[str] = []
    styled: list[tuple[str, str]] = []
    works = item.get("work") or {}
    several_work = len(works) > 1
    for action_id, work in works.items():
        name = f"{action_id}: " if several_work else ""
        if work.get("status") in ("starting", "active"):
            started = parse_ts(work.get("started_at"))
            age = humanise((now - started).total_seconds()) if started else "?"
            changed = ", alert changed" if work.get("state_changed") else ""
            note = f"[{name}in progress: {work.get('agent', 'agent')}, {age}{changed}]"
            trailer.append(note)
            styled.append((note, "36"))
        elif work.get("status") == "finished":
            finished = parse_ts(work.get("updated_at"))
            ago = humanise((now - finished).total_seconds()) if finished else "?"
            note = f"[{name}session finished {ago} ago; rechecked]"
            trailer.append(note)
            styled.append((note, "90"))
        elif work.get("status") == "failed":
            note = f"[{name}session failed to open]"
            trailer.append(note)
            styled.append((note, "31"))
    if item.get("plan"):
        from .investigate import verdict

        v = verdict(item["plan"])
        if not item.get("plan_ok", True):
            # Say so on the row. A failed investigation that renders as nothing
            # is worse than one that renders as a failure: it looks untouched,
            # so nobody asks why the rung never produced anything.
            note = "[investigation failed]"
            trailer.append(note)
            styled.append((note, "31"))
        elif v.get("fixable"):
            mark = {"yes": "32", "needs-a-decision": "33"}.get(v["fixable"], "90")
            note = f"[investigated: {v['fixable']}]"
            trailer.append(note)
            styled.append((note, mark))
    if item.get("detail"):
        trailer.append(item["detail"])
    if trailer:
        wrapped = _wrap_row(" " * content_col, "  ".join(trailer), content_col)
        for note, colour in styled:
            wrapped = [line.replace(note, paint(note, colour), 1) for line in wrapped]
        lines.extend(wrapped)
    for link in _link_lines(item):
        lines.append(" " * content_col + paint("↗ ", "36") + link)
    for action in _action_lines(item):
        # Commands are the exception to prose wrapping. Let the terminal
        # soft-wrap the logical line: inserting indentation into the command
        # makes selecting and pasting the purple text produce a broken command.
        lines.append(" " * content_col + paint("→ ", "32") + action)
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
    if count and young:
        headline += dim(f" (+{young} below threshold)")
    stale = generated is not None and (now - generated).total_seconds() > 600
    age_text = f"snapshot {age} ago"
    out.append(f"{paint('xa', '1')}  {dim('·')}  {headline}  {dim('·')}  "
               f"{paint(age_text, '31') if stale else dim(age_text)}")

    faults = [i for i in items if i["kind"] == "fault"]
    pending = [i for i in items if i["kind"] == "pending"]
    backlog = [i for i in items if i["kind"] == "backlog"]
    status = [i for i in items if i["kind"] == "status"]

    name_w = min(34, max([len(i["uid"]) for i in items], default=20))

    # Split the section the same way the headline does. Printing FAULTS (4)
    # above a headline that says 3 invites exactly one question, and the answer
    # is that a fault below its threshold is real but not yet your problem.
    counting = [i for i in faults if i.get("counts")]
    waiting = [i for i in faults if not i.get("counts")]

    if counting:
        out.append("")
        out.append(paint(f"FAULTS ({len(counting)})", "1"))
        for i in counting:
            out.extend(_row(i, now, name_w))

    if waiting:
        out.append("")
        out.append(dim(f"below threshold ({len(waiting)}) — real, not yet urgent"))
        for i in waiting:
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
            out.extend(_summary_row(i, name_w))

    if status:
        out.append("")
        out.append(paint("STATUS", "1"))
        for i in status:
            out.extend(_summary_row(i, name_w))

    healthy = [
        m["name"]
        for m in snapshot.get("monitors", [])
        if m.get("ok") and not any(
            x["monitor"] == m["name"] and x["kind"] not in ("backlog", "status")
            for x in all_items
        )
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

    if any(i.get("actions") for i in items):
        out.append("")
        out.append(dim("Next: xa why <id> · xa investigate <id> · xa open <id>"))

    return "\n".join(out)


def render_detail(item: dict[str, Any], colour: bool | None = None,
                  actions_taken: Sequence[Any] = ()) -> str:
    """Everything the check already knows, so an escalation need not re-derive it."""
    import json

    global _COLOUR
    previous, _COLOUR = _COLOUR, colour
    try:
        return _render_detail(item, actions_taken)
    finally:
        _COLOUR = previous


def _render_detail(item: dict[str, Any], actions_taken: Sequence[Any] = ()) -> str:
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
    linked_urls = {link.get("url") for link in item.get("links") or []}
    if item.get("url") and item["url"] not in linked_urls:
        out.append(f"  url         {item['url']}")
    links = _link_lines(item)
    if links:
        out += ["", paint("  links", "1"), *("    " + link for link in links)]
    if item.get("detail"):
        out += ["", "  " + item["detail"]]
    if item.get("metrics"):
        out += ["", paint("  metrics", "1")]
        for k, v in item["metrics"].items():
            out.append(f"    {k:<24} {v}")
    actions = _action_lines(item)
    if actions:
        out += ["", paint("  next", "1"), *("    " + action for action in actions)]
    works = item.get("work") or {}
    if works:
        out += ["", paint("  work", "1")]
        for action_id, work in works.items():
            started = parse_ts(work.get("started_at"))
            updated = parse_ts(work.get("updated_at"))
            out.append(
                f"    {action_id}: {work.get('status')} with {work.get('agent')}"
                + (f" since {started.astimezone().isoformat()}" if started else "")
            )
            if work.get("state_changed"):
                out.append("      the alert changed after this session started")
            if work.get("status") == "finished" and updated:
                out.append(
                    f"      finished and rechecked at {updated.astimezone().isoformat()}"
                )
    if item.get("plan"):
        from .investigate import verdict

        v = verdict(item["plan"])
        if not item.get("plan_ok", True):
            heading = "  investigation (did not finish; will be tried again)"
        else:
            heading = "  investigation"
            # The prompt asks for one word. When an agent writes a sentence
            # instead, it belongs in the body -- where it already is -- not
            # spliced into a heading that then runs off the terminal.
            if 0 < len(v.get("confidence", "")) <= 12:
                heading += f"  ({v['confidence']} confidence)"
        out += ["", paint(heading, "1"),
                *("    " + ln for ln in item["plan"].splitlines())]
    if actions_taken:
        # What has already been done about this, from the action log. Worth
        # knowing before starting anything: an item escalated an hour ago
        # probably has a session open on it somewhere.
        out += ["", paint("  already done", "1")]
        for a in actions_taken:
            when = parse_ts(a["ts"])
            ago = humanise((now - when).total_seconds()) if when else "?"
            out.append(f"    {ago:>4} ago   {a['action']} ({a['mode']}, {a['agent']})")
    members = (item.get("evidence") or {}).get("cluster_members")
    if members:
        if item.get("cluster_key"):
            out += ["", paint(f"  members ({len(members)})", "1")]
            for m in members[:20]:
                out.append(f"    {m['title'][:100]}")
                command = f"xa mute {shlex.quote(m['uid'])}"
                out.append(f"      Stop watching: {paint(command, '35')}")
        else:
            out += ["", paint(f"  clustered with {len(members)} other(s)", "1")]
            for m in members[:20]:
                when = (m.get("since") or "")[:10]
                out.append(f"    {when:12} {m['title'][:80]}")
    # An addressable aggregate stores each complete member observation in its
    # evidence so nested commands can resolve it. That is engine bookkeeping,
    # not useful detail: the curated member list above is the human view. An
    # individual member still shows its ordinary evidence when addressed.
    if item.get("evidence") and not (item.get("cluster_key") and members):
        out += ["", paint("  evidence", "1")]
        for line in json.dumps(item["evidence"], indent=2).splitlines():
            out.append("    " + line)
    return "\n".join(out)

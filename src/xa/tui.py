"""The `xa tui` interface.

The same verbs as the command line, one keystroke each. It reads the same
snapshot file and calls the same code paths, so there is nothing it can show or
do that `xa --json` could not.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static

from .model import parse_ts, utcnow
from .policy import humanise

KIND_ORDER = {"fault": 0, "pending": 1, "backlog": 2}
SEVERITY_STYLE = {"alert": "bold red", "warn": "yellow", "unknown": "magenta", "info": "dim"}


class Ask(ModalScreen[str]):
    """One-line prompt, for things like a snooze duration."""

    BINDINGS = [Binding("escape", "dismiss_prompt", "cancel")]

    def __init__(self, question: str, initial: str = ""):
        super().__init__()
        self.question = question
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="ask"):
            yield Static(self.question, id="ask-label")
            yield Input(value=self.initial, id="ask-input")

    def on_mount(self) -> None:
        self.query_one("#ask-input", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_dismiss_prompt(self) -> None:
        self.dismiss("")


class XA(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #table { width: 3fr; }
    #detail { width: 2fr; border-left: solid $panel; padding: 0 1; overflow-y: auto; }
    #status { height: 1; padding: 0 1; background: $panel; }
    #ask { align: center middle; width: 60; height: auto; border: thick $accent; padding: 1 2; background: $surface; }
    """

    BINDINGS = [
        Binding("a", "ack", "ack"),
        Binding("s", "snooze", "snooze"),
        Binding("m", "mute", "mute"),
        Binding("u", "unmute", "unmute"),
        Binding("o", "open", "open session"),
        Binding("c", "toggle_agent", "claude/codex"),
        Binding("t", "threshold", "threshold"),
        Binding("M", "mode", "mode"),
        Binding("r", "refresh", "refresh"),
        Binding("A", "toggle_all", "show hidden"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self):
        super().__init__()
        self.snapshot: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self.show_all = False
        self.agent: str | None = None   # None means each action's configured default

    # -- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
            yield Static("", id="detail", markup=False)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("", "age", "item", "what")
        self.action_refresh()

    # -- data ------------------------------------------------------------

    def load(self) -> None:
        from .cli import _load_snapshot

        self.snapshot = _load_snapshot()
        items = self.snapshot.get("items", [])
        if not self.show_all:
            items = [i for i in items if i.get("disposition", "active") == "active"]
        self.rows = sorted(items, key=lambda i: (KIND_ORDER.get(i["kind"], 3),
                                                 -{"alert": 3, "warn": 2, "unknown": 1, "info": 0}
                                                 .get(i["severity"], 0)))

    def action_refresh(self) -> None:
        self.load()
        table = self.query_one("#table", DataTable)
        cursor = table.cursor_row
        table.clear()
        now = utcnow()
        for item in self.rows:
            since = parse_ts(item.get("since"))
            age = humanise((now - since).total_seconds()) if since else "-"
            style = SEVERITY_STYLE.get(item["severity"], "")
            mark = {"acked": "✓", "snoozed": "z", "muted": "m"}.get(item.get("disposition", ""), "")
            glyph = {"alert": "●", "warn": "◐", "unknown": "?", "info": "·"}.get(item["severity"], "·")
            title = item["title"]
            if item.get("cluster_size", 1) > 1:
                title += f" (+{item['cluster_size'] - 1})"
            table.add_row(f"[{style}]{mark}{glyph}[/]", age, item["uid"][:32], title)
        if self.rows:
            table.move_cursor(row=min(cursor, len(self.rows) - 1))
        self.update_status()
        self.update_detail()

    def update_status(self) -> None:
        generated = parse_ts(self.snapshot.get("generated_at"))
        age = humanise((utcnow() - generated).total_seconds()) if generated else "?"
        count = sum(1 for i in self.rows if i.get("counts"))
        agent = self.agent or "per-action"
        hidden = len(self.snapshot.get("items", [])) - len(self.rows)
        extra = f"  ·  {hidden} hidden" if hidden and not self.show_all else ""
        self.query_one("#status", Static).update(
            f" {count} fault(s)  ·  snapshot {age} ago  ·  agent: {agent}{extra}"
        )

    @property
    def current(self) -> dict[str, Any] | None:
        table = self.query_one("#table", DataTable)
        if not self.rows or table.cursor_row is None or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def update_detail(self) -> None:
        item = self.current
        panel = self.query_one("#detail", Static)
        if item is None:
            panel.update("")
            return
        from .render import render_detail

        # Reuse the command line's renderer, so the two can never disagree.
        # Plain text, with markup off on the widget: evidence routinely contains
        # shell snippets and Lean terms full of brackets, and interpreting those
        # as markup crashes the pane on exactly the items worth reading.
        panel.update(render_detail(item, colour=False))

    @on(DataTable.RowHighlighted)
    def row_changed(self) -> None:
        self.update_detail()

    # -- verbs -----------------------------------------------------------

    def _cli(self, *args: str) -> None:
        """Run the real command, so the TUI has no private code path."""
        from .cli import main as cli_main

        try:
            cli_main(list(args))
        except SystemExit as exc:
            self.notify(str(exc), severity="error")
            return
        self.action_refresh()

    def action_ack(self) -> None:
        if item := self.current:
            self._cli("ack", item["uid"])
            self.notify(f"acked {item['uid']}")

    def action_mute(self) -> None:
        if item := self.current:
            self._cli("mute", item["uid"])
            self.notify(f"muted {item['uid']}")

    def action_unmute(self) -> None:
        if item := self.current:
            self._cli("unmute", item["uid"])
            self.notify(f"cleared {item['uid']}")

    def action_snooze(self) -> None:
        item = self.current
        if item is None:
            return

        def done(when: str) -> None:
            if when:
                self._cli("snooze", item["uid"], when)
                self.notify(f"snoozed {item['uid']} for {when}")

        self.push_screen(Ask("Snooze until? (3h, 2d, tomorrow, mon, 2026-09-01)", "tomorrow"), done)

    def action_threshold(self) -> None:
        item = self.current
        if item is None:
            return

        def done(value: str) -> None:
            if value:
                self._cli("threshold", f"{item['monitor']}.alert_after", value)
                self.notify(f"{item['monitor']}.alert_after = {value}")

        self.push_screen(Ask(f"alert_after for {item['monitor']}?", "12h"), done)

    def action_mode(self) -> None:
        item = self.current
        if item is None:
            return

        def done(mode: str) -> None:
            if mode:
                self._cli("mode", item["monitor"], mode)
                self.notify(f"{item['monitor']} → {mode}")

        self.push_screen(Ask(f"Mode for {item['monitor']}? (report/investigate/auto)",
                             item.get("mode", "report")), done)

    def action_toggle_agent(self) -> None:
        self.agent = {None: "claude", "claude": "codex", "codex": None}[self.agent]
        self.update_status()

    def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.action_refresh()

    def action_open(self) -> None:
        """Escalate. Suspends the TUI so the agent owns the terminal."""
        item = self.current
        if item is None:
            return
        args = ["open", item["uid"]]
        if self.agent:
            args.append(f"--{self.agent}")
        with self.suspend():
            from .cli import main as cli_main

            try:
                cli_main(args)
            except SystemExit as exc:
                if str(exc) not in ("0", "None"):
                    print(exc)
                input("\n[enter] to return to xa ")
        self.action_refresh()


def main(argv: list[str] | None = None) -> int:
    XA().run()
    return 0

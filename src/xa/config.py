"""Loading the policy directory.

The engine ships with no monitors and no thresholds. Everything specific to
what a particular person considers "on fire" lives in a policy directory,
found via `$XA_POLICY` and defaulting to `~/metacortex/xa`.

If a domain-specific string ever appears in this package, it is in the wrong
repository.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .policy import MonitorPolicy, Suppression, Thresholds, parse_duration, parse_when

DEFAULT_POLICY_DIRS = ("~/metacortex/xa", "~/.config/xa")
DEFAULT_INTERVAL = timedelta(minutes=30)


def policy_dir() -> Path:
    """Where this user's monitors, thresholds, prompts and actions live."""
    env = os.environ.get("XA_POLICY")
    if env:
        return Path(env).expanduser()
    for candidate in DEFAULT_POLICY_DIRS:
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return Path(DEFAULT_POLICY_DIRS[0]).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get("XA_CACHE", "~/.cache/xa")).expanduser()


def state_dir() -> Path:
    return Path(os.environ.get("XA_STATE", "~/.local/state/xa")).expanduser()


@dataclass(slots=True)
class Action:
    """A named way to act on an item.

    Rendered from an editable markdown template at launch time, so changing
    what an escalated session is told needs no code change and no restart.
    """

    id: str
    label: str
    prompt: str | None = None          # path to a template, relative to policy dir
    agent: str = "claude"              # claude | codex; --codex overrides per call
    cwd: str | None = None             # directory to run `wt` from
    # Where to clone if `cwd` is missing. The daemon runs on one host but
    # escalation happens wherever the user is sitting, so an action that
    # assumes a checkout exists is an action that works on one machine.
    repo: str | None = None
    target: str | None = None          # PR number, branch, or URL, templated
    name: str | None = None            # wt --name, so parallel sessions do not collide
    # `escalate` opens an interactive session; `run` executes a command directly.
    kind: str = "escalate"
    command: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "Action":
        return cls(
            id=str(raw["id"]),
            label=str(raw.get("label", raw["id"])),
            prompt=raw.get("prompt"),
            agent=str(raw.get("agent", "claude")),
            cwd=raw.get("cwd"),
            repo=raw.get("repo"),
            target=raw.get("target"),
            name=raw.get("name"),
            kind=str(raw.get("kind", "escalate")),
            command=list(raw.get("command") or []),
        )


@dataclass(slots=True)
class MonitorSpec:
    """How to run one monitor, and what to do about what it finds."""

    name: str
    kind: str = "exec"                 # exec | github-search
    exec: str | None = None            # path, relative to the policy dir
    args: list[str] = field(default_factory=list)
    interval: timedelta = DEFAULT_INTERVAL
    timeout: timedelta = timedelta(minutes=10)
    enabled: bool = True
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Keyed by observation-key prefix; see MonitorPolicy.thresholds_for.
    key_thresholds: dict[str, Thresholds] = field(default_factory=dict)
    mode: str = "report"
    actions: dict[str, Action] = field(default_factory=dict)
    # Passed to the monitor as XA_OPT_* environment variables, so a monitor can
    # be tuned without editing it.
    options: dict[str, Any] = field(default_factory=dict)
    # Free-form fields used by declarative monitor kinds.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def policy(self) -> MonitorPolicy:
        return MonitorPolicy(name=self.name, thresholds=self.thresholds, mode=self.mode,
                             key_thresholds=dict(self.key_thresholds))


@dataclass(slots=True)
class Config:
    root: Path
    monitors: dict[str, MonitorSpec] = field(default_factory=dict)
    # Global kill switch. Even a monitor set to `auto` does nothing while this
    # is false, so there is one place to stop everything.
    autonomy_enabled: bool = False
    snooze_hour: int = 9
    ack_expiry: timedelta = timedelta(days=90)
    daemon_url: str = "http://127.0.0.1:8787"
    raw: dict[str, Any] = field(default_factory=dict)

    def monitor(self, name: str) -> MonitorSpec:
        try:
            return self.monitors[name]
        except KeyError:
            raise KeyError(f"no monitor named {name!r} in {self.root}/config.toml") from None

    def policies(self) -> dict[str, MonitorPolicy]:
        return {name: spec.policy for name, spec in self.monitors.items()}

    def resolve(self, relative: str | None) -> Path | None:
        if not relative:
            return None
        path = Path(relative).expanduser()
        return path if path.is_absolute() else (self.root / path)


def load(root: Path | None = None) -> Config:
    root = root or policy_dir()
    path = root / "config.toml"
    raw: dict[str, Any] = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())

    cfg = Config(
        root=root,
        autonomy_enabled=bool(raw.get("autonomy_enabled", False)),
        snooze_hour=int(raw.get("snooze_hour", 9)),
        ack_expiry=parse_duration(raw.get("ack_expiry")) or timedelta(days=90),
        daemon_url=str(raw.get("daemon_url", "http://127.0.0.1:8787")),
        raw=raw,
    )

    for name, block in (raw.get("monitor") or {}).items():
        interval = parse_duration(block.get("interval")) or DEFAULT_INTERVAL

        # A `thresholds` table may contain scalars (the monitor default) and
        # sub-tables (per-key overrides). TOML makes them indistinguishable
        # without looking, so split on type.
        raw_thresholds = block.get("thresholds") or {}
        defaults = {k: v for k, v in raw_thresholds.items() if not isinstance(v, dict)}
        key_thresholds = {
            prefix: Thresholds.from_config({**defaults, **override}, interval)
            for prefix, override in raw_thresholds.items()
            if isinstance(override, dict)
        }

        actions = {}
        for a in block.get("actions") or []:
            action = Action.from_config(a)
            actions[action.id] = action
        cfg.monitors[name] = MonitorSpec(
            name=name,
            kind=str(block.get("kind", "exec")),
            exec=block.get("exec"),
            args=list(block.get("args") or []),
            interval=interval,
            timeout=parse_duration(block.get("timeout")) or timedelta(minutes=10),
            enabled=bool(block.get("enabled", True)),
            thresholds=Thresholds.from_config(defaults, interval),
            key_thresholds=key_thresholds,
            mode=str(block.get("mode", "report")),
            actions=actions,
            options=dict(block.get("options") or {}),
            raw=block,
        )
    return cfg


# ---------------------------------------------------------------------------
# Suppressions file
# ---------------------------------------------------------------------------

def load_suppressions(root: Path | None = None) -> list[Suppression]:
    """Read `suppressions.toml`: the false positives to stop mentioning.

    This file is written by `xa mute`, and grows as false positives appear.
    That is deliberately the opposite of curating an opt-in list up front: a
    practice run showed an activity gate cutting 79 candidate repositories to
    15 with a single false positive, so the cheap move is to emit everything
    and suppress the rare mistake.
    """
    root = root or policy_dir()
    path = root / "suppressions.toml"
    if not path.exists():
        return []
    raw = tomllib.loads(path.read_text())
    out = []
    for entry in raw.get("suppress") or []:
        until = entry.get("until")
        out.append(
            Suppression(
                uid=str(entry["uid"]),
                state_key=str(entry.get("state_key", "*")),
                disposition=str(entry.get("disposition", "muted")),  # type: ignore[arg-type]
                until=parse_when(until) if isinstance(until, str) else None,
                note=str(entry.get("note", "")),
            )
        )
    return out

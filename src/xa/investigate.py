"""The `investigate` rung.

An agent digs into an item during collection and attaches a plan, so that saying
yes is instant rather than the start of an investigation. This is the whole
reason to do it eagerly: it costs compute on items that get declined, and buys
an answer that is ready when the question is asked.

Nothing here decides to act. An investigation reads and reports; the plan it
produces is a proposal attached to the item, and running it is still a separate,
human decision.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .actions import ActionError, build_prompt, resolve
from .config import Config
from .model import Item, utcnow
from .store import Store

log = logging.getLogger("xa.investigate")

# Prepended to the action's own prompt. The action prompt says what the problem
# is and what a fix would involve; this says that we are not fixing it yet.
BRIEF = """You are investigating a problem so that someone can decide what to do
about it. You are NOT fixing it.

Read whatever you need to. Do not edit files, do not push, do not comment
anywhere, and do not run anything with side effects beyond reading.

Finish with a short report in exactly this shape:

FINDING: one sentence on what is actually wrong.
CONFIDENCE: high | medium | low
FIXABLE: yes | needs-a-decision | no
PLAN:
- the steps you would take, or the question that has to be answered first

Be honest about uncertainty. "I could not tell" is a useful answer and a wrong
confident one is not. If the problem appears to have resolved itself, say so.

---

"""

# Generous: a truncated plan that stops mid-sentence is worse than a long one,
# and this is read by a person deciding whether to act.
MAX_OUTPUT = 20000


@dataclass(slots=True)
class Investigation:
    uid: str
    state_key: str
    plan: str
    ok: bool


def _agent_command(agent: str, prompt: str) -> list[str]:
    if agent == "codex":
        return ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", prompt]
    return ["claude", "-p", prompt, "--dangerously-skip-permissions"]


def run(item: dict[str, Any], cfg: Config, timeout: timedelta) -> Investigation | None:
    """Run one investigation. Returns None when the item offers nothing to do."""
    try:
        action = resolve(item, cfg, None)
        prompt = BRIEF + build_prompt(item, action, cfg)
    except ActionError as exc:
        log.debug("no investigable action for %s: %s", item["uid"], exc)
        return None

    cwd = Path(action.cwd or ".").expanduser()
    if not cwd.is_dir():
        log.debug("skipping %s: %s does not exist", item["uid"], cwd)
        return None

    env = dict(os.environ)
    # An API key in the environment makes `claude -p` bill the API instead of
    # using the subscription, which is a surprising way to spend money on a
    # background task that runs every collection.
    env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = subprocess.run(
            _agent_command(action.agent, prompt),
            capture_output=True, text=True, cwd=str(cwd), env=env,
            timeout=timeout.total_seconds(),
        )
    except subprocess.TimeoutExpired:
        return Investigation(item["uid"], item["state_key"],
                             f"(investigation timed out after {timeout})", ok=False)
    except FileNotFoundError:
        return Investigation(item["uid"], item["state_key"],
                             f"({action.agent} not found on PATH)", ok=False)

    out = (proc.stdout or "").strip()
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + "\n\n[truncated]"
    if proc.returncode != 0 and not out:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return Investigation(item["uid"], item["state_key"],
                             f"(investigation failed: {' / '.join(tail)[:300]})", ok=False)
    return Investigation(item["uid"], item["state_key"], out, ok=True)


VERDICT_KEYS = ("FINDING", "CONFIDENCE", "FIXABLE")


def verdict(plan: str) -> dict[str, str]:
    """Pull the structured verdict out of a plan, for the one-line summary.

    The prompt asks for it in a fixed shape precisely so a reader does not have
    to open the whole report to learn whether this is worth their afternoon.
    """
    out: dict[str, str] = {}
    for line in (plan or "").splitlines():
        for key in VERDICT_KEYS:
            prefix = f"{key}:"
            if line.strip().upper().startswith(prefix):
                out[key.lower()] = line.split(":", 1)[1].strip()
    return out


def wanted(item: Item, store: Store) -> bool:
    """Whether this item should be investigated now.

    Only active faults and pending decisions, only in `investigate` mode, and
    only once per state: re-investigating an unchanged problem every half hour
    would burn money to reproduce an answer already on the item.
    """
    if item.mode != "investigate" or item.disposition != "active":
        return False
    if item.obs.kind == "backlog" or not item.obs.actions:
        return False
    return store.plan(item.uid, item.obs.state_key) is None


def attach(items: list[Item], store: Store) -> None:
    """Attach any plan already stored for each item's current state."""
    for item in items:
        plan = store.plan(item.uid, item.obs.state_key)
        if plan is not None:
            item.plan = plan

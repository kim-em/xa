"""Turning an item into a working session.

The point of collecting evidence eagerly is that escalation can hand an agent
everything the check already worked out, instead of asking it to rediscover a
failure from a URL. The prompt is a markdown template in the policy directory,
rendered at launch, so changing what a session is told needs no code change and
no restart.

Sessions are started through `~/bin/wt`, which already knows how to make a
worktree, seed a prompt, choose between Claude and Codex, and open VS Code.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Action, Config
from .model import parse_ts, utcnow
from .policy import humanise
from .template import render

WT = Path("~/bin/wt").expanduser()


class ActionError(RuntimeError):
    pass


def context_for(item: dict[str, Any]) -> dict[str, Any]:
    """The variables a prompt template can use.

    Everything on the item, plus a few conveniences that a template should not
    have to compute.
    """
    since = parse_ts(item.get("since"))
    ctx = dict(item)
    ctx["age"] = humanise((utcnow() - since).total_seconds()) if since else "an unknown time"
    ctx["since_human"] = since.astimezone().strftime("%a %d %b %H:%M") if since else "unknown"
    return ctx


def build_prompt(item: dict[str, Any], action: Action, cfg: Config) -> str:
    path = cfg.resolve(action.prompt)
    if path is None:
        # Without a template, still hand over the essentials rather than nothing.
        ctx = context_for(item)
        return (
            f"{ctx['title']}\n\n{ctx.get('detail', '')}\n\n"
            f"This has been the case for {ctx['age']}.\n{ctx.get('url') or ''}"
        ).strip()
    if not path.exists():
        raise ActionError(f"prompt template not found: {path}")
    return render(path.read_text(), context_for(item))


def _expand(value: str | None, item: dict[str, Any]) -> str | None:
    """Config fields may themselves be templates, e.g. a PR number in evidence."""
    if value is None:
        return None
    return render(value, context_for(item))


@dataclass(slots=True)
class Launch:
    command: list[str]
    cwd: Path
    env: dict[str, str]
    prompt: str

    def describe(self) -> str:
        """What would actually run, accurately: a dry run that misreports the
        command is worse than no dry run."""
        env = " ".join(f"{k}=<{k.split('_')[-1].lower()}>" for k in self.env)
        command = " ".join(shlex.quote(c) for c in self.command)
        return f"cd {self.cwd} && " + (f"{env} {command}" if env else command)


def plan(item: dict[str, Any], action: Action, cfg: Config, agent: str | None = None,
         ensure: bool = True) -> Launch:
    prompt = build_prompt(item, action, cfg)
    agent = agent or action.agent
    if agent not in ("claude", "codex"):
        raise ActionError(f"unknown agent {agent!r}; expected claude or codex")

    if action.kind == "session":
        # No worktree, no window: start the agent right here, in `cwd`. This is
        # the right shape for work that spans many repositories, where there is
        # no single branch to check out.
        cwd = Path(_expand(action.cwd, item) or ".").expanduser()
        if not cwd.is_dir():
            raise ActionError(f"action {action.id!r} wants to run in {cwd}, which does not exist")
        binary = "codex" if agent == "codex" else "claude"
        flag = "--dangerously-skip-permissions" if binary == "claude" else "--dangerously-bypass-approvals-and-sandbox"
        return Launch([binary, flag, prompt], cwd, {}, prompt)

    if action.kind == "run":
        if not action.command:
            raise ActionError(f"action {action.id!r} is kind=run but has no command")
        command = [_expand(c, item) or "" for c in action.command]
        cwd = Path(_expand(action.cwd, item) or ".").expanduser()
        return Launch(command, cwd, {}, prompt)

    if not WT.exists():
        raise ActionError(f"wt not found at {WT}")

    target = _expand(action.target, item)
    if not target:
        raise ActionError(f"action {action.id!r} has no target to open")

    cwd = Path(_expand(action.cwd, item) or ".").expanduser()
    if ensure and not cwd.is_dir():
        # Only when actually launching: inspecting a plan should never have
        # side effects, least of all a multi-hundred-megabyte one.
        ensure_checkout(cwd, action.repo, action.id)

    command = [str(WT), f"--{agent}"]
    name = _expand(action.name, item)
    if name:
        command += ["--name", name]
    command.append(target)

    return Launch(command, cwd, {"WT_CLAUDE_PROMPT": prompt, "WT_AGENT": agent}, prompt)


def ensure_checkout(cwd: Path, repo: str | None, action_id: str) -> None:
    """Clone the repository an action needs, if it is not already here.

    `wt` derives the repository from the origin remote of its working
    directory, so an action without a checkout has nowhere to stand. Cloning is
    blobless: mathlib-sized histories are otherwise a slow surprise at exactly
    the moment the user wanted to start working.
    """
    if repo is None:
        raise ActionError(
            f"action {action_id!r} wants to run in {cwd}, which does not exist, "
            "and declares no `repo` to clone. Set `repo = \"owner/name\"` on the action, "
            "or point `cwd` at an existing checkout."
        )
    if os.environ.get("XA_NO_CLONE"):
        raise ActionError(f"{cwd} is missing and XA_NO_CLONE is set")

    print(f"xa: {cwd} is missing; cloning {repo} (blobless, this may take a minute)")
    cwd.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", "--quiet", "--filter=blob:none", f"https://github.com/{repo}", str(cwd)],
    )
    if proc.returncode != 0 or not cwd.is_dir():
        raise ActionError(f"could not clone {repo} into {cwd}")


def execute(launch: Launch) -> int:
    env = dict(os.environ)
    env.update(launch.env)
    proc = subprocess.run(launch.command, cwd=str(launch.cwd), env=env)
    return proc.returncode


def resolve(item: dict[str, Any], cfg: Config, action_id: str | None) -> Action:
    """Pick the action to run, defaulting to the item's only one."""
    spec = cfg.monitors.get(item["monitor"])
    if spec is None:
        raise ActionError(f"no configuration for monitor {item['monitor']!r}")

    available = [a for a in item.get("actions") or [] if a in spec.actions]
    if action_id:
        if action_id not in spec.actions:
            known = ", ".join(sorted(spec.actions)) or "(none)"
            raise ActionError(f"{item['monitor']} has no action {action_id!r}; known: {known}")
        return spec.actions[action_id]

    if not available:
        known = ", ".join(sorted(spec.actions)) or "(none configured)"
        raise ActionError(f"{item['uid']} offers no actions; configured for this monitor: {known}")
    if len(available) > 1:
        raise ActionError(
            f"{item['uid']} offers several actions ({', '.join(available)}); name one"
        )
    return spec.actions[available[0]]

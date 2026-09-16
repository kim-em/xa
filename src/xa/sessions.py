"""Durable interactive sessions backed by the local ai-tmux helper."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path


class SessionError(RuntimeError):
    pass


def helper_path() -> str:
    configured = os.environ.get("XA_SESSION_HELPER")
    candidates = [configured] if configured else []
    found = shutil.which("ai-tmux")
    if found:
        candidates.append(found)
    candidates.append(str(Path("~/bin/ai-tmux").expanduser()))
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise SessionError(
        "durable sessions require ai-tmux; put it on PATH, install ~/bin/ai-tmux, "
        "or set XA_SESSION_HELPER"
    )


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, **kwargs)
    except FileNotFoundError as exc:
        raise SessionError(f"could not run {command[0]}: {exc}") from exc


def _helper_env(helper: str) -> dict[str, str]:
    env = dict(os.environ)
    parent = str(Path(helper).expanduser().parent)
    env["PATH"] = parent + os.pathsep + env.get("PATH", "")
    return env


def session_names(cwd: Path, helper: str | None = None) -> set[str]:
    command = [helper or helper_path(), "list-json", "--folder", str(cwd)]
    proc = _run(command, capture_output=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "registry lookup failed").strip()
        raise SessionError(detail)
    try:
        payload = json.loads(proc.stdout or "[]")
        if not isinstance(payload, list):
            raise TypeError
        return {
            str(record["name"])
            for record in payload
            if isinstance(record, dict) and record.get("name")
        }
    except (json.JSONDecodeError, TypeError) as exc:
        raise SessionError("ai-tmux returned invalid registry JSON") from exc


def is_registered(name: str, cwd: Path, helper: str | None = None) -> bool:
    return name in session_names(cwd, helper)


def new_name() -> str:
    return f"xa-{uuid.uuid4().hex}"


# Directories that mark the root of a checkout, and so of an editor window.
_ROOT_MARKERS = (".git", ".vscode")


def owner_folder(start: Path | None = None) -> Path:
    """The folder whose window should reopen a session started from here.

    ai-tmux files a session under one folder and the editor reopens the tabs of
    the folders its window has open, so a session filed under the directory the
    *agent* works in is invisible to every window and is silently lost on the
    next reload. The window we were invoked from is the honest owner.

    An editor opens its terminals at the workspace root, but you are free to cd
    before typing, so walk up to the nearest checkout root rather than trusting
    the working directory as given. XA_SESSION_OWNER overrides the answer for
    anything invoked outside an editor.
    """
    override = os.environ.get("XA_SESSION_OWNER")
    if override:
        return Path(override).expanduser()
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        if any((folder / marker).exists() for marker in _ROOT_MARKERS):
            return folder
    return here


def create(agent: str, name: str, cwd: Path, prompt: str, state_dir: Path,
           helper: str | None = None, owner: Path | None = None) -> str:
    helper = helper or helper_path()
    prompt_dir = state_dir / "session-prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="prompt-", suffix=".txt", dir=prompt_dir)
    prompt_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(prompt)
        command = [helper, "new", agent, "--name", name, "--folder", str(cwd)]
        # Omitted when they agree, so the helper sees exactly the old command.
        if owner is not None and Path(owner) != Path(cwd):
            command += ["--owner-folder", str(owner)]
        command += ["--prompt-file", str(prompt_path), "--detach"]
        proc = _run(command, capture_output=True, env=_helper_env(helper))
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "session launch failed").strip()
            raise SessionError(detail)
        names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if names != [name]:
            raise SessionError("ai-tmux did not return the requested session name")
        return names[0]
    except Exception:
        prompt_path.unlink(missing_ok=True)
        raise


def attach(name: str, cwd: Path, helper: str | None = None) -> int:
    helper = helper or helper_path()
    command = [helper, "attach", name, "--folder", str(cwd)]
    return _run(command, env=_helper_env(helper)).returncode


def describe_new(agent: str, cwd: Path, helper: str | None = None,
                 owner: Path | None = None) -> str:
    executable = helper or helper_path()
    owned = ""
    if owner is not None and Path(owner) != Path(cwd):
        owned = f"--owner-folder {shlex.quote(str(owner))} "
    return (
        f"cd {shlex.quote(str(cwd))} && {shlex.quote(executable)} new {agent} "
        f"--folder {shlex.quote(str(cwd))} {owned}"
        "--prompt-file <prompt-file> --detach"
    )

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from xa import sessions


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_helper_prefers_the_explicit_environment(tmp_path, monkeypatch):
    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o700)
    monkeypatch.setenv("XA_SESSION_HELPER", str(helper))

    assert sessions.helper_path() == str(helper)


def test_registry_names_are_read_from_structured_json(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        sessions,
        "_run",
        lambda command, **kwargs: (
            seen.append((command, kwargs))
            or completed(stdout=json.dumps([{"name": "one"}, {"name": "two"}]))
        ),
    )

    assert sessions.session_names(tmp_path, "/helper") == {"one", "two"}
    assert seen[0][0] == ["/helper", "list-json", "--folder", str(tmp_path)]


@pytest.mark.parametrize("payload", ["not-json", "{}"])
def test_bad_registry_output_is_unknown_not_empty(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(
        sessions, "_run", lambda *args, **kwargs: completed(stdout=payload)
    )

    with pytest.raises(sessions.SessionError, match="invalid registry JSON"):
        sessions.session_names(tmp_path, "/helper")


def test_create_writes_a_private_prompt_and_returns_the_name(tmp_path, monkeypatch):
    seen = []

    def run(command, **kwargs):
        prompt = Path(command[command.index("--prompt-file") + 1])
        seen.append((command, prompt.read_text(), os.stat(prompt).st_mode & 0o777))
        prompt.unlink()
        return completed(stdout="ai-claude-work-1\n")

    monkeypatch.setattr(sessions, "_run", run)

    name = sessions.create(
        "claude", "ai-claude-work-1", tmp_path, "diagnose this",
        tmp_path / "state", "/helper",
    )

    assert name == "ai-claude-work-1"
    assert seen[0][1:] == ("diagnose this", 0o600)
    assert seen[0][0][seen[0][0].index("--name") + 1] == "ai-claude-work-1"
    assert seen[0][0][-1] == "--detach"


def test_create_keeps_the_owner_out_of_the_command_when_it_matches(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        sessions,
        "_run",
        lambda command, **kwargs: (
            seen.append(command)
            or Path(command[command.index("--prompt-file") + 1]).unlink()
            or completed(stdout="ai-claude-work-1\n")
        ),
    )

    sessions.create(
        "claude", "ai-claude-work-1", tmp_path, "prompt", tmp_path / "state",
        "/helper", owner=tmp_path,
    )

    assert "--owner-folder" not in seen[0]


def test_create_files_the_session_under_the_owner_it_is_given(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        sessions,
        "_run",
        lambda command, **kwargs: (
            seen.append(command)
            or Path(command[command.index("--prompt-file") + 1]).unlink()
            or completed(stdout="ai-claude-work-1\n")
        ),
    )
    work = tmp_path / "metacortex"
    window = tmp_path / "some-repo"

    sessions.create(
        "claude", "ai-claude-work-1", work, "prompt", tmp_path / "state",
        "/helper", owner=window,
    )

    command = seen[0]
    assert command[command.index("--folder") + 1] == str(work)
    assert command[command.index("--owner-folder") + 1] == str(window)


def test_owner_folder_is_the_checkout_you_are_standing_in(tmp_path, monkeypatch):
    monkeypatch.delenv("XA_SESSION_OWNER", raising=False)
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    deep = root / "src" / "xa"
    deep.mkdir(parents=True)

    assert sessions.owner_folder(deep) == root.resolve()


def test_owner_folder_falls_back_to_where_it_was_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("XA_SESSION_OWNER", raising=False)
    plain = tmp_path / "no-markers"
    plain.mkdir()

    assert sessions.owner_folder(plain) == plain.resolve()


def test_owner_folder_takes_the_environment_over_the_search(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setenv("XA_SESSION_OWNER", str(tmp_path / "elsewhere"))

    assert sessions.owner_folder(root) == tmp_path / "elsewhere"


def test_failed_create_removes_the_prompt(tmp_path, monkeypatch):
    prompt = None

    def run(command, **kwargs):
        nonlocal prompt
        prompt = Path(command[command.index("--prompt-file") + 1])
        return completed(returncode=3, stderr="tmux is missing")

    monkeypatch.setattr(sessions, "_run", run)

    with pytest.raises(sessions.SessionError, match="tmux is missing"):
        sessions.create(
            "claude", "ai-claude-work-2", tmp_path, "prompt",
            tmp_path / "state", "/helper",
        )
    assert prompt is not None and not prompt.exists()

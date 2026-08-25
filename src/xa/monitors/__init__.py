"""Declarative monitor kinds.

Deliberately small. Anything that needs real parsing graduates to an
executable in the policy directory, in whatever language suits it.
"""

from __future__ import annotations

from ..config import Config, MonitorSpec
from ..model import MonitorReport


def run_declarative(spec: MonitorSpec, cfg: Config) -> MonitorReport:
    if spec.kind == "github-search":
        from .github import run_github_search

        return run_github_search(spec, cfg)
    raise ValueError(f"unknown monitor kind {spec.kind!r} for {spec.name!r}")

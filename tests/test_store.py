"""Database retention and contention between the daemon and CLI."""

import sqlite3
from datetime import timedelta

import pytest

from xa import cli
from xa.model import Item, Observation, utcnow
from xa.store import Store


def test_retention_indexes_upgrade_existing_databases(tmp_path):
    path = tmp_path / "xa.db"
    store = Store(path)
    old = utcnow() - timedelta(days=401)
    recent = utcnow() - timedelta(days=399)
    item = Item(monitor="m", obs=Observation(key="k", title="t"))
    for at in (old, recent):
        store.record_items([item], at)
        store.record_run("m", at, True, None, 1)
    # Simulate a database created before retention had its own indexes.
    store.execute("DROP INDEX history_prune")
    store.execute("DROP INDEX runs_prune")
    store.close()

    store = Store(path)
    for table, column in (("history", "ts"), ("runs", "collected_at")):
        plan = store.execute(
            f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE {column} < ?",
            (old.isoformat(),),
        ).fetchall()
        assert any("SEARCH" in row["detail"] for row in plan)
        assert not any("SCAN" in row["detail"] for row in plan)

    assert store.prune_history() == 1
    assert [row["ts"] for row in store.execute("SELECT ts FROM history")] == [
        recent.isoformat()
    ]
    assert store.last_run("m") == (recent, True)
    assert store.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    store.close()


def test_cli_reports_writer_contention_and_can_claim_after_release(tmp_path, monkeypatch):
    path = tmp_path / "xa.db"
    daemon, client = Store(path), Store(path)
    client.execute("PRAGMA busy_timeout=0")
    monkeypatch.setattr(
        cli, "cmd_open",
        lambda args: client.claim_work("m/k", "s", "m", "fix", "claude"),
    )
    daemon.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SystemExit, match="xa: state database is busy; try again shortly"):
            cli.main(["open", "m/k"])
        assert client.work_for("m/k", "s", "fix") is None
    finally:
        daemon.execute("ROLLBACK")

    assert client.claim_work("m/k", "s", "m", "fix", "claude") is not None
    assert daemon.claim_work("m/k", "s", "m", "fix", "claude") is None
    client.close()
    daemon.close()


def test_cli_does_not_hide_other_database_errors(tmp_path, monkeypatch):
    store = Store(tmp_path / "xa.db")
    monkeypatch.setattr(cli, "cmd_open", lambda args: store.execute("SELECT missing_column"))
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            cli.main(["open", "m/k"])
    finally:
        store.close()

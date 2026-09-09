import json
from datetime import timedelta

from xa.config import Config, JobSpec, load
from xa.jobs import due, read_status, run_job, write_status
from xa.model import utcnow


def executable(tmp_path, body):
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    path = jobs / "backup"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_config_loads_jobs_separately_from_monitors(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[job.backup]\nexec="jobs/backup"\ninterval="1w"\n'
        'retry_after="1d"\ntimeout="30m"\nmonitor="health"\n'
    )

    job = load(tmp_path).job("backup")

    assert job.interval == timedelta(weeks=1)
    assert job.retry_after == timedelta(days=1)
    assert job.monitor == "health"


def test_job_result_is_persisted_and_drives_cadence(tmp_path, monkeypatch):
    monkeypatch.setenv("XA_STATE", str(tmp_path / "state"))
    executable(tmp_path, "printf '%s\\n' '{\"ok\":true,\"summary\":\"saved\"}'\n")
    cfg = Config(root=tmp_path)
    spec = JobSpec(name="backup", exec="jobs/backup", interval=timedelta(weeks=1))

    status = run_job(spec, cfg)

    assert status["ok"] is True
    assert status["summary"] == "saved"
    assert read_status("backup")["last_success_at"] is not None
    assert not due(spec)


def test_failed_job_retries_on_its_shorter_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("XA_STATE", str(tmp_path / "state"))
    now = utcnow()
    write_status("backup", {
        "ok": False,
        "finished_at": (now - timedelta(hours=2)).isoformat(),
        "last_success_at": None,
    })
    spec = JobSpec(
        name="backup", exec="jobs/backup", interval=timedelta(weeks=1),
        retry_after=timedelta(hours=1),
    )

    assert due(spec, now)


def test_structured_failure_preserves_previous_success(tmp_path, monkeypatch):
    monkeypatch.setenv("XA_STATE", str(tmp_path / "state"))
    previous = (utcnow() - timedelta(days=3)).isoformat()
    write_status("backup", {
        "ok": True, "finished_at": previous, "last_success_at": previous,
    })
    executable(
        tmp_path,
        "printf '%s\\n' "
        "'{\"ok\":false,\"summary\":\"rejected\",\"details\":{\"failures\":[1]}}'\n",
    )
    spec = JobSpec(name="backup", exec="jobs/backup")

    status = run_job(spec, Config(root=tmp_path))

    assert status["ok"] is False
    assert status["summary"] == "rejected"
    assert status["last_success_at"] == previous
    assert status["details"]["failures"] == [1]

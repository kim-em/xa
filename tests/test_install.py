import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def fake_launchctl(tmp_path: Path, *, system_running: bool = True) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "launchctl.log"
    script = bindir / "launchctl"
    running = "true" if system_running else "false"
    script.write_text(
        "#!/bin/bash\n"
        "echo \"$*\" >> \"$XA_TEST_LAUNCHCTL_LOG\"\n"
        "if [[ \"$1\" == print && \"$2\" == system/* ]]; then\n"
        f"  if {running}; then echo 'state = running'; echo 'pid = 123'; fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    script.chmod(0o700)
    return bindir, log


def run_installer(tmp_path: Path, *, system_running: bool = True):
    bindir, log = fake_launchctl(tmp_path, system_running=system_running)
    home = tmp_path / "home"
    user_dir = home / "Library" / "LaunchAgents"
    system_dir = tmp_path / "LaunchDaemons"
    user_dir.mkdir(parents=True)
    old = user_dir / "com.kim.xa-daemon.plist"
    old.write_text("old user agent")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
        "XA_TEST_LAUNCHCTL_LOG": str(log),
        "XA_LAUNCHD_NO_SUDO": "1",
        "XA_LAUNCHD_USER_DIR": str(user_dir),
        "XA_LAUNCHD_SYSTEM_DIR": str(system_dir),
        "XA_LAUNCHD_VALIDATE_ATTEMPTS": "1",
        "XA_LAUNCHD_VALIDATE_DELAY": "0",
    }
    proc = subprocess.run(
        [str(ROOT / "scripts" / "install-launchd.sh")],
        text=True,
        capture_output=True,
        env=env,
    )
    return proc, log.read_text(), old, system_dir / "com.kim.xa-daemon.plist"


def test_launchd_installer_migrates_the_user_agent_after_validation(tmp_path):
    proc, calls, old, installed = run_installer(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert installed.exists()
    assert not old.exists()
    assert "bootout gui/" in calls
    assert "bootstrap system" in calls
    assert "kickstart -k system/com.kim.xa-daemon" in calls


def test_launchd_installer_rolls_back_when_the_daemon_does_not_stay_up(tmp_path):
    proc, calls, old, _installed = run_installer(tmp_path, system_running=False)

    assert proc.returncode == 1
    assert old.exists()
    assert "bootout system/com.kim.xa-daemon" in calls
    assert "bootstrap gui/" in calls
    assert "restored the previous user agent" in proc.stderr

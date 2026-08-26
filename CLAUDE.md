# xa

## One machine collects, and it is this one

The daemon runs here, under `com.kim.xa-daemon`, and writes
`~/.cache/xa/snapshot.json`, which `xa` reads. The policy — every monitor,
threshold, prompt and action — lives in `~/metacortex/xa`, a separate git
repository (`kim-em/metacortex`). Nothing in this repository should contain a
domain-specific string; if a change seems to need one, it belongs there.

A monitor that needs another host reaches it itself, over ssh:
`monitors/corpus` runs carica's health check that way. The engine has no idea
other machines exist, which is the point.

xa used to collect on carica and sync a snapshot here over HTTP. It doesn't. A
reference to `xa-sync`, `daemon_url` or port 8787 anywhere is stale.

## Changing behaviour

- **A monitor executable** takes effect within a tick. `due()` hashes the
  file's bytes every 30 seconds, so an edited monitor is due immediately
  regardless of its interval. Commit and push it from `~/metacortex`.
- **`config.toml`** needs `launchctl kickstart -k gui/$(id -u)/com.kim.xa-daemon`.
  The daemon calls `config.load()` once at startup, so edited thresholds,
  options, intervals and actions are invisible to it until it restarts.
  Thresholds set with `xa threshold` live in sqlite instead and apply at once.
- **Engine code**, in this repository, also needs that kickstart. The install is
  editable, so the files change under a process that already imported the old
  modules.

## Reading the output honestly

The header age is daemon liveness: when the snapshot was last published, not
when each monitor last ran. A monitor that has not been due in six hours is
still shown, and its report is still the last one it made. To check freshness
per monitor, read `ok` and `collected_at` in `xa --json`, or force a full sweep
with `xa collect --force`.

Two ways collection stops without anything looking wrong: this is a laptop, so
it collects nothing while asleep; and a LaunchAgent only runs in a logged-in
GUI session, so a reboot that stops at the login window collects nothing
either. A monitor that fails is retried after five minutes rather than its full
interval, so waking with no network costs minutes, not hours.

## Working here

`scripts/install.sh` is idempotent; run it after changing a plist. The engine is
pure stdlib apart from the optional TUI, and it should stay that way: a
dependency on the read path is a dependency on a good day.

`.venv/bin/pytest` here, and `~/projects/xa/.venv/bin/pytest tests/` in
`~/metacortex/xa` for the monitors.

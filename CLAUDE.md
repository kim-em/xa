# xa

## One machine collects, and it is carica

The system LaunchDaemon runs on carica, under `com.kim.xa-daemon`, and writes
`~/.cache/xa/snapshot.json` there, which `xa` reads. You reach both by ssh. The
policy, meaning every monitor, threshold, prompt and action, lives in
`~/metacortex/xa`, a separate git repository (`kim-em/metacortex`). Nothing in
this repository should contain a domain-specific string; if a change seems to
need one, it belongs there.

A monitor that needs another host reaches it itself, over ssh: `monitors/machines`
probes persica that way. The engine has no idea other machines exist, which is
the point.

Persica keeps a checkout and a venv, so that tests run there, and nothing else.
No LaunchAgent, no `~/.local/bin/xa`, no `~/.cache/xa`, no database. It is
somewhere to edit and test from, not somewhere that runs. If `xa` is a command
on the machine you are sitting at and that machine is not carica, something has
been installed that should not have been.

xa used to collect on carica and sync a snapshot to persica over HTTP; then it
collected on persica alone. It does neither. A reference to `xa-sync`,
`daemon_url` or port 8787 anywhere is stale, and so is a claim that the
collector is the machine you edit on.

## Changing behaviour

Nothing you edit takes effect until carica has it. That is the cost of the
split, and it is worth naming rather than rediscovering: an edited monitor on
persica changes nothing at all, however many times you re-read `xa`.

- **A monitor executable** takes effect within a tick of reaching carica.
  `due()` hashes the file's bytes every 30 seconds, so an edited monitor is due
  immediately regardless of its interval. Commit and push it from
  `~/metacortex`, then `ssh carica 'cd ~/metacortex && git pull'`.
- **`config.toml`** additionally needs
  `ssh carica 'sudo launchctl kickstart -k system/com.kim.xa-daemon'`. The
  daemon calls `config.load()` once at startup, so edited thresholds, options,
  intervals, jobs and actions are invisible to it until it restarts. Thresholds
  set with `xa threshold` live in sqlite instead and apply at once.
- **Engine code**, in this repository, needs a push, a pull on carica, and that
  same kickstart. The install there is editable, so the files change under a
  process that already imported the old modules.

## Monitors observe, jobs mutate

`xa collect --force` runs every monitor and has to stay safe to type, so a
monitor must not write anything. Anything that does belongs in `[job.*]`, which
`collect` and `refresh` cannot reach. A job runs on its own cadence or when you
type `xa run <job>`, writes one small JSON document under
`~/.local/state/xa/jobs/`, and a cheap read-only monitor turns that into an
ordinary item. The config entry is the authorization.

## The one thing the engine writes into a prompt

An escalation prompt is a policy template, rendered, and nothing else, with a
single exception: when an investigation is attached to the item, `xa open`
prepends it, framed as evidence rather than instructions, and the template
follows. So a template is no longer the whole of what a session is told.

It goes in front deliberately. A plan is agent prose summarising whatever the
monitor saw, and monitors read other people's pull request titles, chat
messages and CI logs, so the template gets the last word rather than text
nobody here wrote. `xa open --show-prompt` shows the result, wrapper included.
A template that references `{{plan}}` itself is left alone.

The wrapper lives in `actions.plan`, not `build_prompt`, and must stay there:
`investigate.run` builds its brief from `build_prompt`, so a wrapper any deeper
would hand a re-investigation its own previous answer and turn an independent
second look into a confirmation pass.

## Sessions run where the daemon does

`xa open` launches an agent on carica through `ai-tmux` and records its stable
session name and working directory in `work_sessions`. A second `xa open`
attaches that session in the terminal where the command was run instead of
starting a rival. The ai-tmux registry survives a reboot and resumes the exact
Claude or Codex conversation when it is attached again.

## Reading the output honestly

The header age is daemon liveness: when the snapshot was last published, not
when each monitor last ran. A monitor that has not been due in six hours is
still shown, and its report is still the last one it made. To check freshness
per monitor, read `ok` and `collected_at` in `xa --json`, or force a full sweep
with `xa collect --force`.

Carica is a Mac Studio that stays awake, so the sleep gaps that came with
collecting on a laptop are gone. The LaunchDaemon starts at boot and runs as
`kim`, without depending on a GUI login. A monitor that fails is retried after
five minutes rather than its full interval.

## Working here

`scripts/install.sh` is idempotent and installs the collector with `sudo`; run
it on carica after changing a plist, and do not run it on persica, which is what
removing the runtime from persica was for. The engine is pure stdlib apart from
the optional TUI; `ai-tmux` and tmux are required only on the action path for
durable `session` actions.

Tests run anywhere, and are the reason the checkout on persica is worth
keeping. `.venv/bin/pytest` here, and `~/projects/xa/.venv/bin/pytest tests/`
in `~/metacortex/xa` for the monitors.

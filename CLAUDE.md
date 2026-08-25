# xa

## The machine you are on is not the machine that collects

`carica` runs the daemon. `persica` and every other machine run `xa-sync`,
which pulls `snapshot.json` over the tailnet every thirty seconds into
`~/.cache/xa`, and `xa` prints that file. So the rows you are looking at were
produced on carica, by carica's checkout of this repository and carica's copy
of the policy directory (`~/metacortex/xa`, per `launchd/com.kim.xa-daemon.plist`).

Two consequences, both of which have already cost an afternoon:

- Editing a monitor here changes nothing anyone can see. Running the monitor
  directly, or `xa collect <monitor>`, proves the edit works and is then
  overwritten by the next sync within thirty seconds. That is a test, never a
  confirmation.
- `xa doctor` prints the *local* policy directory and calls the snapshot a
  local file. It says nothing about the collector, so it cannot tell you a fix
  has landed. Neither can a fresh `snapshot Ns ago`: that timestamp is
  carica's, and it stays fresh while serving stale beliefs.

## Changing xa's behaviour means deploying to carica

When Kim asks for a change in what `xa` reports, the change is not done when it
is committed. It is done when carica is running it. Do the deploy in the same
turn, unless she says otherwise.

Engine change, in this repository:

```sh
git push                                     # only when asked
ssh carica 'cd ~/projects/xa && git pull --ff-only'
ssh carica 'launchctl kickstart -k gui/$(id -u)/com.kim.xa-daemon'
```

The restart is not optional. The install is editable, so the pull updates the
files on disk, but the daemon process imported the old modules at startup and
will go on running them for as long as it lives.

Policy change, in `~/metacortex/xa`:

```sh
cd ~/metacortex && git push                  # only when asked
ssh carica 'cd ~/metacortex && git pull --ff-only --no-rebase'
```

No restart needed for a monitor executable: `due()` hashes the file's bytes on
disk every tick, so a changed monitor is due immediately regardless of its
interval, and the new rows appear within a minute. `config.toml` is the
exception, and does need the kickstart above: the daemon calls `config.load()`
once at startup, so edited thresholds, options, intervals and actions are
invisible to it until it restarts. Thresholds changed with `xa threshold` live
in sqlite instead and take effect without one.

Prompts are read at use time by whichever machine runs the action: persica for
`xa open`, carica for a monitor in `investigate` or `auto` mode. Keep both
current rather than reasoning about which one will need it.

## Verifying that it landed

```sh
ssh carica 'tail -5 ~/.local/state/xa/daemon.log'   # "snapshot: N item(s), M fault(s)"
xa                                                  # after one sync interval
```

Watch the item count change, then read the row. Saying a fix is live without
having seen it come back from carica is how the afternoon above was spent.

## Everything domain-specific lives in the policy directory

The engine knows nothing about mathlib, Zulip or GitHub. If a change needs a
domain-specific string, it belongs in `~/metacortex/xa` (a separate git
repository, `kim-em/metacortex`), not here. A monitor that reads a threshold,
decides a severity or knows about acknowledgements is in the wrong layer; see
README.md for the split and `src/xa/monitors/lib.py` for the contract.

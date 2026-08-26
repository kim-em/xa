# xa

External amygdala: know what is on fire without having to ask.

`xa` watches the things you are responsible for and tells you which of them
need you. Asking is instantaneous, because asking never starts work: a daemon
collects in the background and `xa` reads a file.

The engine knows nothing about what it is watching. Every monitor, threshold,
prompt and action lives in a **policy directory** (`$XA_POLICY`, defaulting to
`~/metacortex/xa`). If a domain-specific string appears in this repository, it
is in the wrong place.

## The idea

Monitors emit **facts**. The engine owns **policy**.

A monitor reports what it saw and when the condition started. It never decides
severity, never reads a threshold, never knows about acknowledgements. That
split is why "how long overdue matters" is a config edit rather than a code
change, and why someone else's monitors run unchanged on this engine.

Three kinds of thing, because they need different treatment:

- **fault** - something is wrong. Only faults contribute to the count and are
  eligible for a phone push. The count is the scarce resource: a badge reading
  7 has to mean seven things are wrong.
- **pending** - nothing is broken; a decision is waiting on you.
- **backlog** - a standing pile, reported as metrics and a trend, never as a
  list. Some queues are four figures long and will never fit on a screen.

## Using it

`xa help` explains the model; `xa --help` lists the flags.

```sh
xa                          # what needs me, right now
xa --json                   # the same, for scripts, a menu bar, or a bot
xa why <item>               # everything the check already worked out
```

Responding:

```sh
xa ack <item>               # I'll deal with this; quiet until the failure changes
xa snooze <item> tomorrow   # also: 3h, 2d, mon, next week, 2026-09-01
xa mute <item>              # false positive; never mention it again
xa suppressions             # what is currently silenced, and until when
```

Acknowledgements bind to `(item, state_key)`, where `state_key` is a digest of
*what is currently wrong*. A snooze therefore survives new commits, new runs and
the clock ticking, and lapses the instant the failure becomes a different
failure. Nothing is silenced permanently, even deliberately: `mute` and `ack`
carry a long but finite expiry, so a hasty keystroke costs weeks rather than
forever.

Changing policy:

```sh
xa mode <monitor> report|investigate|auto
xa threshold nightly-testing.alert_after 6h
xa threshold nightly-testing.alert_after default
xa config                   # edit the policy files
xa prompt <action>          # edit what an escalated session gets told
```

## Writing a monitor

A monitor is any executable that prints one JSON document. Python gets a helper
library; bash and Go are equally welcome.

```python
from xa.monitors import lib

def check(report):
    if thing_is_broken():
        report.add(
            key="ci",
            title="the thing is red",
            state_key=lib.digest(failing_step, *modules),
            since=when_it_started,
            evidence={"log": excerpt, "files": files},
            actions=["fix"],
        )

lib.main(check)
```

The contract:

- Emit facts, not judgements. Report `since`, never a severity.
- Choose `state_key` carefully. Too fine-grained and acknowledgements never
  stick, because something incidental churns and every run looks new. Too
  coarse and a genuinely new failure is silenced by an old acknowledgement.
- Attach evidence. Whatever an escalated session would otherwise have to go and
  fetch belongs in `evidence`, gathered now while it is cheap. This is also what
  keeps reads instant.
- Fail loudly. A crashed monitor reports `unknown`; it never reports health.

## Running it continuously

```sh
scripts/install.sh
```

A launchd agent runs the collector. It works through the monitors on their own
intervals, writes `snapshot.json`, and `xa` reads that file: asking never starts
work, and the read path opens no socket. A test asserts exactly that, including
for `xa ack`.

One machine collects, and it is the machine you read on. A monitor that needs
something from another host reaches it itself, over ssh, which keeps the
awkward part inside the monitor that cares rather than in the engine. If the
collector is asleep, nothing collects; the header age tells you so, because it
is written here.

A monitor that fails is retried in five minutes rather than after its full
interval, which matters on a machine that wakes with no network.

```sh
xa tui                       # the same verbs, one keystroke each
```

## Layout

```
src/xa/model.py     Observation, Item, state_key, kinds
src/xa/policy.py    thresholds, ageing, severity, clustering, suppression
src/xa/config.py    loading the policy directory
src/xa/store.py     sqlite: acks, snoozes, modes, overrides, history
src/xa/collect.py   running monitors, building the snapshot
src/xa/render.py    terminal output (pure stdlib, so startup stays instant)
src/xa/cli.py       the `xa` command
src/xa/actions.py   prompt rendering and session launching
src/xa/template.py  the small mustache subset prompts are written in
src/xa/daemon.py    the collector loop
src/xa/tui.py       the interactive view
```

## Install

```sh
uv venv && uv pip install -e .
```

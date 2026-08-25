"""`xa help`: what the tool is, not what its flags are.

`--help` already lists subcommands. The thing that is actually hard to pick up
from a flag list is the model: why acknowledgements behave the way they do, why
some rows count and others do not, and where to change any of it. So this
explains the model, and points at the command for each idea rather than the
other way round.
"""

from __future__ import annotations

from .render import dim, paint

OVERVIEW = """
{title}

xa watches the things you are responsible for and tells you which of them need
you. Asking is instantaneous because asking never starts work: a daemon collects
in the background and `xa` reads a file.

{h_kinds}

  {fault}  something is wrong. Only faults contribute to the count and are
           eligible for a phone push. The count is the scarce resource: a
           badge reading 7 has to mean seven things are wrong.
  {pending}  nothing is broken; a decision is waiting on you.
  {backlog}  a standing pile, reported as metrics and a trend. Never a list,
           because some of these are four figures long.

Every fault and pending decision offers an action. An item nobody can act on is
a notification, not an alert, and `xa doctor` will tell you if one creeps in.

{h_looking}

  xa                    what needs me, now
  xa --json             the same, for scripts, a bot, or a menu bar
  xa why <item>         everything the check already worked out
  xa --all              include what you have silenced

{h_responding}

  xa ack <item>         I'll deal with this
  xa snooze <item> 2d   tell me again later
  xa mute <item>        false positive; never mention it again
  xa open <item>        hand it to an agent, with everything the check knows

{h_changing}

  xa threshold <monitor>.<name> <value>    when something starts to matter
  xa mode <monitor> report|investigate|auto
  xa config             edit the policy files
  xa prompt <action>    edit what an escalated session is told

{h_more}

  xa help model         facts, policy, and why acknowledgements lapse
  xa help snooze        the difference between ack, snooze and mute
  xa help actions       how an item becomes a working session
  xa help monitors      writing one
  xa help thresholds    tuning what counts as urgent
  xa help autonomy      the ladder, and what is switched on
"""

MODEL = """
{title}

Monitors emit facts. The engine owns policy.

A monitor reports what it saw and when the condition started. It never decides
severity, never reads a threshold, never knows about acknowledgements. That
split is why "how long overdue matters" is a config edit rather than a code
change, and why someone else's monitors run unchanged on this engine.

{h_key}

Every item has two identifiers:

  key         stable identity, e.g. nightly-testing/ci
  state_key   a digest of *what is currently wrong*

Acknowledgements bind to the pair. A snooze therefore survives new commits, new
runs and the clock ticking, and lapses the instant the failure becomes a
different failure. If you snooze a red build and it later goes red for an
unrelated reason, you hear about it immediately.

Choosing state_key is the subtlest part of writing a monitor. Too fine-grained
and a snooze never sticks, because something incidental churns and every
collection looks new. Too coarse and a genuinely new failure is silenced by an
old acknowledgement. If a check nags after you have silenced it, that is the
bug, and the fix is the key rather than a mute.

{h_unknown}

A monitor that crashes, times out, or produces unparseable output reports
`unknown`. It never reports health. Losing sight of something is not the same as
seeing that it is fine, and the header tells you how old the snapshot is for the
same reason: stale data is not good news.
"""

SNOOZE = """
{title}

  xa ack <item>              quiet until the failure changes. Expires in 90 days.
  xa snooze <item> [when]    quiet until a time you pick. Default tomorrow 09:00.
  xa mute <item>             a false positive, whatever shape it takes next.
  xa unmute <item>           undo any of the above.
  xa suppressions            what is silenced, and until when.

`when` accepts a duration (3h, 2d, 1w), a weekday (mon), tomorrow, tonight,
next week, or a date (2026-09-01). A bare day means the morning, because "tell
me again tomorrow" does not mean one minute past midnight.

{h_forever}

Nothing is silenced permanently, including deliberately. Both ack and mute carry
a long but finite expiry, so a hasty keystroke costs weeks rather than forever.

Ack and snooze bind to the current state_key, so they lapse when the problem
changes. Mute binds to any state, because a false positive will not become true
by changing shape. If you find yourself muting something repeatedly, the check
is wrong and worth fixing rather than silencing.
"""

ACTIONS = """
{title}

  xa actions                 what each monitor offers
  xa open <item> [action]    run it
  xa open <item> --dry-run   print the command and prompt, launch nothing
  xa open <item> --codex     use Codex instead of the configured agent
  xa prompt <action>         edit what the session is told

The point of collecting evidence eagerly is that escalation hands an agent
everything the check already worked out. A session for a failing build starts
from the failing files, lines and diagnostics, not from a URL and an instruction
to go and look.

{h_kinds}

  escalate   a worktree and a VS Code window on a branch or PR, via wt
  session    an agent started in place, for work spanning many repositories
  run        a plain command

{h_prompts}

Prompts are markdown in the policy directory, rendered at launch, so an edit
takes effect immediately with no restart. They are the most valuable thing here
to have your judgement in: several deliberately ask for a diagnosis rather than
a fix, and several stop short of acting at all.
"""

MONITORS = """
{title}

A monitor is any executable that prints one JSON document. Python gets a helper
library; bash and Go are equally welcome.

    from xa.monitors import lib

    def check(report):
        if thing_is_broken():
            report.add(key="ci", title="the thing is red",
                       state_key=lib.digest(step, *modules),
                       since=when_it_started,
                       evidence={{"log": excerpt}}, actions=["fix"])

    lib.main(check)

{h_contract}

  - Emit facts, not judgements. Report `since`, never a severity.
  - Choose state_key carefully. See `xa help model`.
  - Attach evidence. Whatever an escalated session would otherwise have to go
    and fetch belongs in `evidence`, gathered now while it is cheap. This is
    also what keeps reads instant.
  - Fail loudly. A crashed monitor reports unknown; it never reports health.

Simple cases need no executable at all: a `github-search` monitor is a query and
a list of metrics in config. Anything needing real parsing writes a program.

Editing a monitor makes it due immediately, so a fix lands on the next tick
rather than at the end of its interval.
"""

THRESHOLDS = """
{title}

An observation exists because something is true. It becomes worth interrupting
for only once it has been true long enough.

  xa threshold nightly-testing.alert_after        read it
  xa threshold nightly-testing.alert_after 6h     set it, live
  xa threshold nightly-testing.alert_after default  back to the config file

  warn_after    when it starts counting
  alert_after   when it becomes urgent
  ttl           how long this monitor's data stays trustworthy
  push          whether an alert here is worth a phone notification

push is deliberately separate from severity, so severity does not quietly become
a paging policy.

One monitor often reports several kinds of thing that deserve different
deadlines, so thresholds can be set per observation-key prefix in config. A
branch nobody has created is not urgent on the same timescale as a build that
has just gone red.

If something is noisy, the threshold is usually the answer, and muting it is
usually not.
"""

AUTONOMY = """
{title}

  report        purely programmatic. The check states a fact.
  investigate   an agent digs in during collection and attaches a plan, so
                saying yes is instant rather than the start of an investigation.
  auto          handled without asking, reported afterwards.

  xa mode <monitor> <rung>
  xa mode                     what each monitor is set to

Everything ships at `report`. Promotion is per monitor, earned by track record,
and reversible. There is also a global `autonomy_enabled` switch in config, so
there is one place to stop everything.

{h_rules}

At every rung, autonomous actions must be reversible, and must never post
publicly: no messages, no PR comments, no merges, no pushes to shared branches
without confirmation. Some things should never be promoted at all. Deleting a
branch in a shared repository is a good example: mechanical, and still not
something to do unattended.
"""

TOPICS = {
    "model": ("The model", MODEL),
    "snooze": ("Silencing things", SNOOZE),
    "ack": ("Silencing things", SNOOZE),
    "mute": ("Silencing things", SNOOZE),
    "actions": ("Acting on an item", ACTIONS),
    "open": ("Acting on an item", ACTIONS),
    "monitors": ("Writing a monitor", MONITORS),
    "thresholds": ("Thresholds", THRESHOLDS),
    "autonomy": ("Autonomy", AUTONOMY),
    "mode": ("Autonomy", AUTONOMY),
}


def _fmt(template: str, title: str, **fields) -> str:
    subs = {(f"h_{k}" if not k.startswith("_") else k[1:]): v for k, v in fields.items()}
    subs = {k: (paint(v, "1") if k.startswith("h_") else v) for k, v in subs.items()}
    return template.format(title=paint(title, "1"), **subs).strip("\n")


def render(topic: str | None = None) -> str:
    if topic is None:
        return _fmt(
            OVERVIEW, "xa: external amygdala",
            kinds="Three kinds of thing", looking="Looking",
            responding="Responding", changing="Changing what it does",
            more="More",
            _fault=paint("fault".ljust(7), "31"),
            _pending=paint("pending", "33"),
            _backlog=dim("backlog"),
        )

    key = topic.lower().lstrip("-")
    if key not in TOPICS:
        known = ", ".join(sorted(set(TOPICS)))
        return f"xa: no help topic {topic!r}. Try: {known}"
    title, body = TOPICS[key]
    return _fmt(body, title, key="Identity and change", unknown="Not knowing",
                forever="Nothing is forever", kinds="Kinds of action",
                prompts="Prompts", contract="The contract", rules="The rules")

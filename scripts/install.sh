#!/usr/bin/env bash
# Idempotent setup for xa. Safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY="${XA_POLICY:-$HOME/metacortex/xa}"

cd "$ROOT"

echo "==> virtualenv"
# Creating the environment is the only non-idempotent step, so guard it rather
# than clearing: re-running install.sh should never throw away a working venv.
if command -v uv >/dev/null; then
    [ -d .venv ] || uv venv --quiet .venv
    uv pip install --quiet -e ".[tui]"
else
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install --quiet -e ".[tui]"
fi

echo "==> state directories"
mkdir -p "$HOME/.local/state/xa" "$HOME/.cache/xa"
chmod 700 "$HOME/.local/state/xa"

if [ ! -d "$POLICY" ]; then
    cat >&2 <<MSG

No policy directory at $POLICY.

xa ships with no monitors: what counts as on fire is yours to declare. Create
that directory with a config.toml and a monitors/ directory, or point XA_POLICY
somewhere else. See README.md.
MSG
    exit 1
fi

echo "==> symlink"
# ~/.local/bin rather than ~/bin: the latter is a git repository of scripts, and
# a generated symlink does not belong in it.
mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/.venv/bin/xa" "$HOME/.local/bin/xa"

echo "==> launchd"
mkdir -p "$HOME/Library/LaunchAgents"

# xa used to collect on one host and sync a snapshot to the others. It does not
# any more. Remove the sync agent here rather than by hand: it has KeepAlive, so
# a half-finished upgrade would leave it overwriting the snapshot this machine
# is now generating for itself.
legacy="$HOME/Library/LaunchAgents/com.kim.xa-sync.plist"
if [ -f "$legacy" ]; then
    launchctl bootout "gui/$(id -u)/com.kim.xa-sync" 2>/dev/null || true
    rm -f "$legacy"
    echo "    removed com.kim.xa-sync"
fi

label="com.kim.xa-daemon"
cp "$ROOT/launchd/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/$label.plist"
echo "    loaded $label"

echo
echo "Done. Try: xa doctor"

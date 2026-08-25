#!/usr/bin/env bash
# Idempotent setup for xa. Safe to re-run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POLICY="${XA_POLICY:-$HOME/metacortex/xa}"
ROLE="${1:-client}"   # `daemon` on the collecting host, `client` everywhere else

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

echo "==> launchd ($ROLE)"
mkdir -p "$HOME/Library/LaunchAgents"
case "$ROLE" in
    daemon) plists="com.kim.xa-daemon" ;;
    client) plists="com.kim.xa-sync" ;;
    *) echo "usage: install.sh [daemon|client]" >&2; exit 2 ;;
esac
for label in $plists; do
    cp "$ROOT/launchd/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
    launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
    launchctl load "$HOME/Library/LaunchAgents/$label.plist"
    echo "    loaded $label"
done

echo
echo "Done. Try: xa doctor"

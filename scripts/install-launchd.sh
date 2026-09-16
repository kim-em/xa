#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
label="com.kim.xa-daemon"
uid="$(id -u)"
source_plist="$ROOT/launchd/$label.plist"
user_dir="${XA_LAUNCHD_USER_DIR:-$HOME/Library/LaunchAgents}"
system_dir="${XA_LAUNCHD_SYSTEM_DIR:-/Library/LaunchDaemons}"
user_plist="$user_dir/$label.plist"
system_plist="$system_dir/$label.plist"
attempts="${XA_LAUNCHD_VALIDATE_ATTEMPTS:-20}"
delay="${XA_LAUNCHD_VALIDATE_DELAY:-0.25}"
backup_dir="$(mktemp -d)"
backup_plist="$backup_dir/$label.plist"
trap 'rm -rf "$backup_dir"' EXIT

as_root() {
    if [[ "${XA_LAUNCHD_NO_SUDO:-}" == "1" ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

if [[ "${XA_LAUNCHD_NO_SUDO:-}" != "1" ]]; then
    sudo -v
fi

mkdir -p "$user_dir"
as_root mkdir -p "$system_dir"

had_user_plist=false
user_was_loaded=false
had_system_plist=false
[[ -f "$user_plist" ]] && had_user_plist=true
if [[ -f "$system_plist" ]]; then
    cp "$system_plist" "$backup_plist"
    had_system_plist=true
fi
if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
    user_was_loaded=true
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
fi

rollback() {
    as_root launchctl bootout "system/$label" 2>/dev/null || true
    if [[ "$had_system_plist" == true ]]; then
        if [[ "${XA_LAUNCHD_NO_SUDO:-}" == "1" ]]; then
            install -m 0644 "$backup_plist" "$system_plist"
        else
            as_root install -o root -g wheel -m 0644 "$backup_plist" "$system_plist"
        fi
        as_root launchctl bootstrap system "$system_plist" 2>/dev/null || true
        as_root launchctl kickstart -k "system/$label" 2>/dev/null || true
    elif [[ "$had_user_plist" == true || "$user_was_loaded" == true ]]; then
        launchctl bootstrap "gui/$uid" "$user_plist" 2>/dev/null || true
    fi
}

as_root launchctl bootout "system/$label" 2>/dev/null || true
if [[ "${XA_LAUNCHD_NO_SUDO:-}" == "1" ]]; then
    install -m 0644 "$source_plist" "$system_plist"
else
    as_root install -o root -g wheel -m 0644 "$source_plist" "$system_plist"
fi
if ! as_root launchctl bootstrap system "$system_plist"; then
    rollback
    echo "xa: could not bootstrap $label in the system domain" >&2
    exit 1
fi
as_root launchctl kickstart -k "system/$label"

running() {
    local state
    state="$(as_root launchctl print "system/$label" 2>/dev/null || true)"
    grep -q 'state = running' <<<"$state" && grep -q 'pid = ' <<<"$state"
}

ok=false
for ((i = 0; i < attempts; i++)); do
    if running; then
        sleep "$delay"
        if running; then
            ok=true
            break
        fi
    fi
    sleep "$delay"
done

if [[ "$ok" != true ]]; then
    rollback
    echo "xa: $label did not remain running; restored the previous user agent" >&2
    exit 1
fi

rm -f "$user_plist"
echo "    running $label in the system domain"

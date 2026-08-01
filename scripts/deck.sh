#!/usr/bin/env bash
#
# Steam Deck helper — logs, status and service control for the deployed plugin.
#
#   ./scripts/deck.sh logs [N]   Print the last N lines of the plugin log (default 200)
#   ./scripts/deck.sh follow     Stream the plugin log live
#   ./scripts/deck.sh loader     Stream the decky plugin_loader journal
#   ./scripts/deck.sh status     Reader/device/plugin state snapshot
#   ./scripts/deck.sh restart    Restart plugin_loader (reloads the plugin)
#   ./scripts/deck.sh shell      Interactive SSH session
#
# Config is read from .vscode/settings.json when present, else these defaults.
# Tip: run `ssh-copy-id deck@steamdeck.local` once to stop the password prompts.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SETTINGS="${SCRIPT_DIR}/../.vscode/settings.json"

read_setting() {
    local key="$1" fallback="$2"
    if [[ -f "$SETTINGS" ]]; then
        python3 -c "
import json,re,sys
try:
    raw = open('$SETTINGS').read()
    raw = re.sub(r'//.*', '', raw)
    print(json.loads(raw).get('$key') or '$fallback')
except Exception:
    print('$fallback')
" 2>/dev/null || echo "$fallback"
    else
        echo "$fallback"
    fi
}

DECK_IP="$(read_setting deckip steamdeck.local)"
DECK_PORT="$(read_setting deckport 22)"
DECK_USER="$(read_setting deckuser deck)"
DECK_PASS="$(read_setting deckpass ssap)"

SSH=(ssh -p "$DECK_PORT" "${DECK_USER}@${DECK_IP}")

# The log directory is named after plugin.json's "name" ("Decky Links"), not the
# extracted directory name ("Decky-Links"), so glob rather than hardcode it.
# shellcheck disable=SC2016
FIND_LOG='ls -t "$HOME"/homebrew/logs/*[Dd]ecky*[Ll]ink*/*.log 2>/dev/null | head -1'

remote() { "${SSH[@]}" "$@"; }

cmd_logs() {
    local lines="${1:-200}"
    remote "log=\$($FIND_LOG); \
        if [ -z \"\$log\" ]; then \
            echo 'No plugin log found. Available log dirs:' >&2; \
            ls -1 \"\$HOME\"/homebrew/logs/ 2>/dev/null >&2 || echo '  (none)' >&2; \
            exit 1; \
        fi; \
        echo \"── \$log ──\"; tail -n $lines \"\$log\""
}

cmd_follow() {
    remote "log=\$($FIND_LOG); \
        if [ -z \"\$log\" ]; then echo 'No plugin log found.' >&2; exit 1; fi; \
        echo \"── following \$log ──\"; tail -f \"\$log\""
}

cmd_loader() {
    remote "echo '$DECK_PASS' | sudo -S journalctl -u plugin_loader -f -n 100" 2>&1
}

cmd_status() {
    remote "bash -s" <<'REMOTE'
echo "── serial devices ──"
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  none found"
echo
echo "── USB serial adapters ──"
lsusb 2>/dev/null | grep -iE "ch34|cp210|ftdi|prolific|acr|pn53" || echo "  none matched"
echo
echo "── python ──"
python3 -V
echo
# -type d matters: a leftover "Decky Links.zip" sits alongside the extracted
# "Decky-Links" directory and sorts first (space < '-'), so a plain glob picks
# the archive and every check below silently inspects a path inside nothing.
plugin_dir=$(find "$HOME/homebrew/plugins" -maxdepth 1 -type d -iname "*decky*link*" 2>/dev/null | head -1)

echo "── plugin dir ──"
if [ -n "$plugin_dir" ]; then
    echo "  $plugin_dir"
else
    echo "  not installed"
fi
echo
echo "── log dirs ──"
ls -1 "$HOME"/homebrew/logs/ 2>/dev/null || echo "  none"
echo
echo "── python extension tags in py_modules (must match the python above) ──"
if [ -n "$plugin_dir" ]; then
    find "$plugin_dir/py_modules" -name "*.cpython-*.so" -printf "%f\n" 2>/dev/null \
        | sed 's/.*\(cpython-[0-9]*\).*/  \1/' | sort -u || echo "  none found"
fi
echo
echo "── mach-o contamination check (should be empty) ──"
if [ -n "$plugin_dir" ]; then
    find "$plugin_dir/py_modules" \( -name "*darwin*.so" -o -name "*.dylib" \) 2>/dev/null || true
fi
echo
echo "── import check ──"
if [ -n "$plugin_dir" ]; then
    PYTHONPATH="$plugin_dir/py_modules" python3 - <<'PY'
for mod in ("serial", "ndef", "adafruit_pn532.uart", "cryptography", "pyudev", "paho.mqtt.client"):
    try:
        __import__(mod)
        print(f"  OK   {mod}")
    except Exception as e:
        print(f"  FAIL {mod}: {type(e).__name__}: {e}")
PY
else
    echo "  skipped — plugin not installed"
fi
REMOTE
}

cmd_restart() {
    remote "echo '$DECK_PASS' | sudo -S systemctl restart plugin_loader" \
        && echo "plugin_loader restarted"
}

case "${1:-logs}" in
    logs)    cmd_logs "${2:-200}" ;;
    follow)  cmd_follow ;;
    loader)  cmd_loader ;;
    status)  cmd_status ;;
    restart) cmd_restart ;;
    shell)   exec "${SSH[@]}" ;;
    *)
        sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac

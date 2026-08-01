#!/usr/bin/env bash
#
# Steam Deck helper — logs, status and service control for the deployed plugin.
#
#   ./scripts/deck.sh logs [N]   Print the last N lines of the plugin log (default 200)
#   ./scripts/deck.sh follow     Stream the plugin log live
#   ./scripts/deck.sh loader     Stream the decky plugin_loader journal
#   ./scripts/deck.sh status     Reader/device/plugin state snapshot
#   ./scripts/deck.sh udev       Watch block-device udev events live
#   ./scripts/deck.sh mount-test /dev/sdX   Try StorageSource's read-only mount
#   ./scripts/deck.sh format /dev/sdX       ERASE a disk and format it as FAT
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
echo "── serial port identity (for the trigger registry key) ──"
# A stable key needs a serial number; CH340 bridges often have none, in which
# case we must fall back to the physical port path (ID_PATH / location).
for dev in /dev/ttyUSB* /dev/ttyACM*; do
    [ -e "$dev" ] || continue
    echo "  $dev"
    udevadm info --query=property --name="$dev" 2>/dev/null \
        | grep -E "^(ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT|ID_SERIAL|ID_PATH|ID_USB_INTERFACE_NUM)=" \
        | sed 's/^/      /'
done
[ -e /dev/ttyUSB0 ] || [ -e /dev/ttyACM0 ] || echo "  no serial devices present"
echo
echo "── block devices ──"
lsblk -o NAME,PATH,SIZE,TYPE,RM,FSTYPE,LABEL,MOUNTPOINT 2>/dev/null || echo "  lsblk unavailable"
echo
echo "── removable block devices (floppy/USB/optical) ──"
found_removable=0
for dev in /dev/sd? /dev/sr? /dev/fd?; do
    [ -b "$dev" ] || continue
    found_removable=1
    name=$(basename "$dev")
    size=$(cat "/sys/class/block/$name/size" 2>/dev/null || echo "?")
    removable=$(cat "/sys/class/block/$name/removable" 2>/dev/null || echo "?")
    # size is in 512-byte sectors; 0 means the drive is present but has no media
    echo "  $dev  sectors=$size removable=$removable$([ "$size" = "0" ] && echo '  <-- NO MEDIA INSERTED')"
    udevadm info --query=property --name="$dev" 2>/dev/null \
        | grep -E "^(ID_BUS|ID_TYPE|ID_FS_TYPE|ID_FS_UUID|ID_MODEL|ID_VENDOR|ID_SERIAL|ID_SERIAL_SHORT|ID_PATH)=" \
        | sed 's/^/      /'
done
[ "$found_removable" = "0" ] && echo "  none present"
echo
echo "── all USB devices ──"
lsusb 2>/dev/null || echo "  lsusb unavailable"
echo
echo "── mounted filesystems of interest ──"
grep -E "^/dev/(sd|sr|fd|mmcblk)" /proc/mounts 2>/dev/null || echo "  none mounted"
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
echo "── plugin process (user must be root to mount disks) ──"
ps -eo user:16,pid,args 2>/dev/null | grep -i "[D]ecky Links" | head -3 || echo "  not running"
if [ -n "$plugin_dir" ]; then
    echo "  plugin.json flags: $(grep -A4 '"flags"' "$plugin_dir/plugin.json" 2>/dev/null | tr -d ' \n')"

    # A deploy only copies files; the running process keeps executing the code
    # it was spawned with — and its uid is fixed at spawn, so a "root" flag
    # cannot take effect without a restart. Compare the two directly.
    pid=$(pgrep -f "Decky Links" 2>/dev/null | head -1)
    src_mtime=$(stat -c %Y "$plugin_dir/main.py" 2>/dev/null)
    if [ -n "$pid" ] && [ -n "$src_mtime" ]; then
        proc_start=$(stat -c %Y "/proc/$pid" 2>/dev/null)
        echo "  main.py deployed: $(date -d @$src_mtime '+%F %T' 2>/dev/null)"
        echo "  process started:  $(date -d @$proc_start '+%F %T' 2>/dev/null)"
        if [ -n "$proc_start" ] && [ "$src_mtime" -gt "$proc_start" ]; then
            echo "  *** STALE: deployed files are NEWER than the running process."
            echo "      The old code is still running. Run: pnpm deck:restart"
        fi
    fi
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

cmd_udev() {
    echo "Watching block-device udev events. Plug in the drive, then insert/eject a disk."
    echo "Ctrl-C to stop."
    echo
    echo "What to look for: a drive being plugged in emits 'add'. Inserting a disk into"
    echo "an already-connected drive usually emits 'change', NOT 'add' — StorageSource"
    echo "must handle both or media insertion goes unnoticed."
    echo
    remote "echo '$DECK_PASS' | sudo -S udevadm monitor --udev --subsystem-match=block --property" 2>&1
}

cmd_mount_test() {
    local dev="${1:-}"
    if [ -z "$dev" ]; then
        echo "usage: ./scripts/deck.sh mount-test /dev/sdX" >&2
        exit 1
    fi
    echo "Attempting the same read-only mount StorageSource performs on $dev ..."
    # The script is staged to a file first. Piping the password into `sudo -S`
    # makes the pipe sudo's stdin, which would swallow a heredoc sent over ssh
    # and leave the remote bash with nothing to run.
    remote "cat > /tmp/decky-mount-test.sh; \
        echo '$DECK_PASS' | sudo -S bash /tmp/decky-mount-test.sh '$dev'; \
        rm -f /tmp/decky-mount-test.sh" <<'REMOTE'
dev="$1"
if [ ! -b "$dev" ]; then
    echo "  $dev is not a block device — check lsblk; a USB floppy is usually /dev/sda"
    exit 1
fi
name=$(basename "$dev")
echo "── device state ──"
sectors=$(cat "/sys/class/block/$name/size" 2>/dev/null)
echo "  sectors: $sectors"
if [ "$sectors" = "0" ]; then
    echo "  no media inserted — StorageSource waits for a 'change' event before mounting"
    exit 0
fi
tmp=$(mktemp -d /tmp/decky-links-XXXXXX)
echo "── mount attempt ──"
if mount -o ro "$dev" "$tmp" 2>&1; then
    echo "  mounted at $tmp"
    echo "── contents ──"
    ls -la "$tmp" 2>&1 | head -20
    echo "── decky-links.json ──"
    if [ -f "$tmp/decky-links.json" ]; then
        cat "$tmp/decky-links.json"
    else
        echo "  NOT PRESENT — StorageSource needs this file at the filesystem root"
    fi
    umount "$tmp"
else
    echo "  mount FAILED (see error above)"
fi
rmdir "$tmp" 2>/dev/null
REMOTE
}

cmd_format() {
    local dev="${1:-}"
    if [ -z "$dev" ]; then
        echo "usage: ./scripts/deck.sh format /dev/sdX" >&2
        echo "Formats a disk as FAT so Decky Links can read and pair it." >&2
        exit 1
    fi
    echo "This ERASES EVERYTHING on $dev (on the Steam Deck) and formats it as FAT."
    echo "Check 'pnpm deck:status' first — a USB floppy is usually /dev/sda, but"
    echo "on a Deck with other USB storage attached it may not be."
    printf "Type the device path again to confirm: "
    read -r confirm
    if [ "$confirm" != "$dev" ]; then
        echo "Aborted." >&2
        exit 1
    fi
    remote "cat > /tmp/decky-format.sh; \
        echo '$DECK_PASS' | sudo -S bash /tmp/decky-format.sh '$dev'; \
        rm -f /tmp/decky-format.sh" <<'REMOTE'
dev="$1"
if [ ! -b "$dev" ]; then
    echo "  $dev is not a block device — aborting"
    exit 1
fi
if [ "$(cat "/sys/class/block/$(basename "$dev")/removable" 2>/dev/null)" != "1" ]; then
    echo "  $dev is not a removable drive — refusing to format it"
    exit 1
fi
umount "$dev" 2>/dev/null
echo "── formatting $dev ──"
mkfs.vfat -I "$dev" && echo "  done — reinsert the disk and it should appear as blank media"
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
    udev)    cmd_udev ;;
    mount-test) cmd_mount_test "${2:-}" ;;
    format)  cmd_format "${2:-}" ;;
    restart) cmd_restart ;;
    shell)   exec "${SSH[@]}" ;;
    *)
        sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac

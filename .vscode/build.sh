#!/usr/bin/env bash
set -euo pipefail

CLI_LOCATION="$(pwd)/cli"
echo "Building plugin in $(pwd)"

# Must match the Python that decky-loader runs plugins with on the Deck.
# SteamOS ships 3.13.5 as of 2026-07. Native extensions are tagged with the
# interpreter version (e.g. _cffi_backend.cpython-313-x86_64-linux-gnu.so) and
# will NOT load under a different one, so a mismatch here silently disables
# cryptography, Pillow and pyzbar on-device.
# Check with: ssh deck@steamdeck.local python3 -V   (or `pnpm deck:status`)
DECK_PYTHON="${DECK_PYTHON:-3.13}"

# ---------------------------------------------------------------------------
# Python dependencies
#
# The decky toolchain does NOT install requirements.txt — the builder image's
# entrypoint only runs pnpm and then rsyncs the tree. So we vendor Python deps
# into py_modules/ ourselves.
#
# These MUST be Linux x86_64 wheels: the Steam Deck is linux/amd64, and the
# decky loader appends py_modules/ to sys.path. Installing with host pip on
# macOS produces Mach-O .so/.dylib files that cannot load on the Deck, which
# is what broke NFC detection previously. Run pip inside a linux/amd64
# container so the wheels always match the target.
# ---------------------------------------------------------------------------
echo "Installing Linux x86_64 Python ${DECK_PYTHON} dependencies into py_modules/..."
rm -rf py_modules
mkdir -p py_modules
# py_modules/ is gitignored apart from this tracked placeholder, which keeps the
# directory present in a fresh clone. Recreate it so a build never shows up as a
# deletion in `git status`.
touch py_modules/.keep

docker run --rm \
    --platform linux/amd64 \
    -v "$(pwd)":/plugin \
    -w /plugin \
    "python:${DECK_PYTHON}-slim" \
    pip install -r requirements.txt --target=./py_modules --upgrade

# Guard: fail loudly rather than shipping macOS binaries to the Deck again.
if find py_modules \( -name "*darwin*.so" -o -name "*.dylib" \) | grep -q .; then
    echo "ERROR: macOS binaries found in py_modules/ — refusing to build." >&2
    find py_modules \( -name "*darwin*.so" -o -name "*.dylib" \) >&2
    exit 1
fi

# Guard: every version-tagged extension must match the Deck's interpreter.
# abi3 wheels are version-independent and are correctly ignored here.
expected_tag="cpython-${DECK_PYTHON//./}"
if mismatched=$(find py_modules -name "*.cpython-*.so" ! -name "*${expected_tag}*" | grep .); then
    echo "ERROR: extensions built for the wrong Python (expected ${expected_tag}):" >&2
    echo "$mismatched" >&2
    echo "Set DECK_PYTHON to the Deck's version (ssh deck@… python3 -V)." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Local packages
#
# `decky plugin build` zips a FIXED allowlist of paths — main.py, plugin.json,
# package.json, dist/, py_modules/, LICENSE, README.md. Top-level sources/ and
# nfc/ are NOT packaged even though the builder rsyncs them into its staging
# dir, so they must be vendored into py_modules/ to reach the device.
#
# main.py puts the plugin dir ahead of py_modules on sys.path, so this copy is
# only ever used on-device (where it is the sole copy); during local dev the
# checked-out tree wins and these copies cannot shadow the files being edited.
# ---------------------------------------------------------------------------
echo "Copying local Python packages into py_modules/..."
rm -rf py_modules/sources py_modules/nfc py_modules/assets
cp -r sources py_modules/sources
cp -r nfc py_modules/nfc
# assets/ is not in the CLI's allowlist either, so the sounds ride along in
# py_modules or they are simply absent from the installed plugin — which is
# exactly what happened: every _play_sound call logged "Sound file not found".
cp -r assets py_modules/assets
find py_modules/sources py_modules/nfc -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# -t: the CLI's default staging dir (/tmp/decky) is not visible to Docker
# Desktop on macOS; $HOME is shared by default.
mkdir -p "$HOME/.decky-build"

# The template wraps this in `sudo` for Linux hosts where the Docker socket
# needs root. That is not the case on macOS with Docker Desktop, and requiring
# a TTY for the password breaks non-interactive builds — so make it opt-in:
#   DECKY_BUILD_SUDO=1 ./.vscode/build.sh
if [[ "${DECKY_BUILD_SUDO:-0}" == "1" ]]; then
    sudo -E "$CLI_LOCATION/decky" plugin build "$(pwd)" -t "$HOME/.decky-build"
else
    "$CLI_LOCATION/decky" plugin build "$(pwd)" -t "$HOME/.decky-build"
fi

#!/usr/bin/env bash
#
# Run the Python test suite in the environment the plugin actually targets.
#
#   ./.vscode/test.sh [pytest args...]
#
# Decky Links runs on a Steam Deck: linux/amd64, on the interpreter Decky
# Loader carries. A developer host is usually neither — an arm64 Mac cannot
# load the Linux wheels this plugin depends on, so `pytest` there fails for
# reasons that have nothing to do with the code. That is what made the suite
# effectively unrunnable and left 600+ tests ungated.
#
# The container is the same one build.sh installs py_modules/ with, for the
# same reason: it is the only place the answer means anything.
#
# CI runs this on ubuntu-latest, which is already linux/amd64, so it skips the
# emulation. Pass DECKY_TEST_NATIVE=1 to do the same locally on a Linux x86_64
# host.

set -euo pipefail

cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

# Must match build.sh — see the long note there on why this is not the SteamOS
# python3. Both read the same variable so they cannot drift apart.
DECK_PYTHON="${DECK_PYTHON:-3.11}"

PYTEST_ARGS=("$@")
if [[ ${#PYTEST_ARGS[@]} -eq 0 ]]; then
    PYTEST_ARGS=(-q)
fi

run_suite() {
    python3 -m pip install --quiet --disable-pip-version-check \
        -r requirements.txt -r tests/requirements.txt
    exec python3 -m pytest "${PYTEST_ARGS[@]}"
}

# Already on the target platform: no container, no emulation.
if [[ "${DECKY_TEST_NATIVE:-0}" == "1" ]]; then
    echo "Running tests natively (DECKY_TEST_NATIVE=1)"
    run_suite
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is required to run the tests off a Linux x86_64 host." >&2
    echo "       Install Docker, or set DECKY_TEST_NATIVE=1 if you are already" >&2
    echo "       on linux/amd64." >&2
    exit 1
fi

echo "Running tests in python:${DECK_PYTHON}-slim (linux/amd64)..."

# --no-cache-dir keeps the image layer small; the run is throwaway anyway.
# py_modules/ is excluded from sys.path via PYTHONPATH ordering: the checked
# out tree already wins (main.py puts it first), and pip installs into the
# container's own site-packages, so the host's build artefacts are never used.
docker run --rm \
    --platform linux/amd64 \
    -v "$(pwd)":/plugin \
    -w /plugin \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "python:${DECK_PYTHON}-slim" \
    bash -c "
        set -e
        pip install --quiet --disable-pip-version-check --no-cache-dir \
            -r requirements.txt -r tests/requirements.txt
        python -m pytest ${PYTEST_ARGS[*]}
    "

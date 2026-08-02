#!/usr/bin/env bash
#
# Kept as a signpost. This script used to build a host-native .venv and run
# pytest in it, which cannot work for this project on most machines: Decky
# Links targets a Steam Deck — linux/amd64, on the interpreter Decky Loader
# carries — and installing its requirements on an arm64 Mac produces wheels
# that will not load. The suite then fails for reasons unrelated to the code,
# which is exactly what kept 600+ tests from being taken seriously.
#
# Use the container instead. It is the same one .vscode/build.sh vendors
# py_modules/ with, for the same reason.

set -euo pipefail

cat <<'EOF'
This script has been replaced.

  pnpm test                 run the suite in a linux/amd64 container
  pnpm test:native          run it directly (only on a Linux x86_64 host)
  ./.vscode/test.sh -k foo  any pytest arguments are passed through

Both are the same script: .vscode/test.sh

Why: the plugin targets linux/amd64 on the interpreter Decky Loader carries,
so a host-native venv gives an answer that does not mean anything unless the
host happens to match. CI (.github/workflows/test.yml) runs the same thing.
EOF

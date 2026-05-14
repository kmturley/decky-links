#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$SCRIPT_DIR/out"
cp -r "$SCRIPT_DIR/src/sources" "$SCRIPT_DIR/out/"
cp -r "$SCRIPT_DIR/src/nfc" "$SCRIPT_DIR/out/"

#!/bin/sh
set -e

cd /backend

mkdir -p out
cp -r src/sources out/
cp -r src/nfc out/

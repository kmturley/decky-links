#!/usr/bin/env bash
CLI_LOCATION="$(pwd)/cli"
echo "Building plugin in $(pwd)"

echo "Installing Python dependencies into py_modules/..."
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt --target=./py_modules --upgrade

echo "Copying local Python packages into py_modules/..."
rm -rf py_modules/sources py_modules/nfc
cp -r sources py_modules/sources
cp -r nfc py_modules/nfc

printf "Please input sudo password to proceed.\n"

# read -s sudopass

# printf "\n"

echo $sudopass | sudo -E $CLI_LOCATION/decky plugin build $(pwd)

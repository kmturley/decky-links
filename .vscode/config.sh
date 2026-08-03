#!/usr/bin/env bash
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]:-$0}"; )" &> /dev/null && pwd 2> /dev/null; )";
# printf "${SCRIPT_DIR}\n"
# printf "$(dirname $0)\n"
if ! [[ -e "${SCRIPT_DIR}/settings.json" ]]; then 
     printf '.vscode/settings.json does not exist. Creating it with default settings. Exiting afterwards. Run your task again.\n\n'
     cp "${SCRIPT_DIR}/defsettings.json" "${SCRIPT_DIR}/settings.json"
     printf 'NOTE: "deckpass" is deliberately empty. settings.json is gitignored,\n'
     printf 'so it is the right place for it — set it there, or export DECK_PASSWORD.\n'
     printf 'It is left blank on purpose so a password can never be committed.\n\n'
     exit 1
else
    printf '.vscode/settings.json does exist. Congrats.\n'
    printf 'Make sure to change settings.json to match your deck.\n'
fi
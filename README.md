# Decky Links

Decky Links is a Steam Deck plugin that launches games and other approved URIs by tapping NFC tags.

This project implements the v1 direction defined in [SPEC.md](./SPEC.md): deterministic launch behavior, no game stacking, portable tag data, and minimal SteamOS-native integration.

## Project Context

Decky Links exists to make physical game cards/tags usable on Steam Deck without maintaining a local library index.

Core goals:

- Store launch info directly on NFC tags (portable across devices).
- Prevent launching over an already running game.
- Avoid auto-relaunch loops after manual game exit.
- Use SteamOS-native behavior for launch/close flows.
- Keep runtime behavior predictable and state-driven.

## What It Does

- Detects NFC tag presence/removal via PN532 (UART).
- Reads/writes NDEF URI records on tags.
- Supports approved URI types:
  - `steam://`
  - `https://`
- Supports Steam and non-Steam game launching:
  - Steam titles via `steam://run/<appid>`
  - Non-Steam shortcuts via `steam://rungameid/<gameID64>`
- Provides pairing mode to write the current game URI to a tag.
- Launches Steam URIs through Steam client APIs (`SteamClient.URL.ExecuteSteamURL` / Steam launch APIs).
- Handles card removal while a paired game is running:
  - optional auto-close
  - otherwise opens Steam side menu flow

## Settings Behavior

### Auto-Launch (`auto_launch`)

- Enabled:
  - Tag tap launches the linked game/URI (if no game is already running).
- Disabled:
  - No launch is performed.
  - For Steam-linked tags, Decky Links opens the game details page instead.

### Auto-Close (`auto_close`)

- Enabled:
  - Removing a paired tag while its game is running triggers game termination.
  - Non-Steam shortcuts are terminated using their `rungameid`/gameID64 target.
- Disabled:
  - Removing a paired tag does not terminate the game.
  - Decky Links opens the Steam side menu (pause flow).

## Architecture

### Backend (`main.py`)

- Python plugin service.
- Owns NFC polling loop, state machine, NDEF parsing/writing, URI allowlist validation.
- Emits events to frontend (`tag_detected`, `uri_detected`, `tag_removed`, etc.).
- Exposes RPC endpoints (`get_settings`, `set_setting`, `start_pairing`, ...).

### Frontend (`src/`)

- Decky UI plugin panel and game-page pair UI.
- `BackgroundManager` starts immediately on plugin load and registers event listeners.
- Shared state module coordinates UI status, pairing state, and settings.

## Repository Layout

- `main.py`: backend runtime and NFC/state logic
- `src/index.tsx`: plugin entry + settings/status UI
- `src/BackgroundManager.tsx`: event + polling orchestration
- `src/GamePagePairer.tsx`: in-game pairing button/modal
- `src/shared.ts`: RPC/event/shared-state wiring
- `tests/`: Python tests for backend and NFC tooling
- `SPEC.md`: product/behavior spec used to guide implementation

## Developer Setup

### Prerequisites

- Node.js `>=18` recommended
- npm or pnpm
- Python `3.11+` (project tested with `3.12`)
- `venv` module available

### Install

```bash
# JavaScript deps
npm install

# Python test env
python3 -m venv .venv
. .venv/bin/activate
pip install -r tests/requirements.txt
pip install pytest pytest-asyncio
```

## Build and Run

### Frontend Build

```bash
npm run build
```

### Watch Mode

```bash
npm run watch
```

### Deploy to Steam Deck (from this repo)

```bash
npm run deploy
```

### Restart Decky Loader on Deck

```bash
npm run stop
npm run start
```

## Testing

Run all tests:

```bash
PYTHONPATH=. .venv/bin/pytest -q
```

Run backend-focused suite only:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin.py
```

Notes:

- `./tests` is a directory, not an executable test command.
- `PYTHONPATH=.` ensures `main.py` is importable in test runs.

## Current Behavior vs Spec

Aligned with v1 spec intent:

- URI-based payloads
- allowlisted URI schemes only (`steam://`, `https://`)
- single active-card/game-safe behavior
- no stacking launches
- no auto-relaunch after game exit
- pairing mode with explicit write flow
- deterministic state transitions and debounced tag removal
- non-Steam shortcut support through `rungameid` game IDs

Implementation detail:

- Tag payload format is NDEF URI records (not JSON text records), as documented in `SPEC.md`.

## Security and Safety Constraints

- URI scheme/path allowlist is enforced backend-side.
- Current allowlist is intentionally strict: `steam://` and `https://` only.
- Setting updates are validated server-side before persistence.
- Launch logic blocks redundant launches for currently running app IDs.

## Troubleshooting

- No events in UI:
  - confirm plugin loaded and `BackgroundManager` started at plugin init.
- NFC reader not detected:
  - verify configured device path in plugin settings.
- Tests fail with import errors:
  - ensure virtualenv is active and run with `PYTHONPATH=.`

## License

GNU General Public License v3.0
See [LICENSE](./LICENSE).

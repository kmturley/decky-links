# 🔗 Decky Links

**Launch games and apps on SteamOS using physical NFC tags.**

![Steam Deck with NFC tag reader](https://raw.githubusercontent.com/kmturley/decky-links/refs/heads/main/decky-links.jpg)

Decky Links is a [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for SteamOS that allows you to launch games and apps by tapping physical NFC tags. By storing portable URI payloads directly on NFC cards, stickers, or 3D-printed cartridges, you can create a physical, cartridge-like experience for your digital games.

![Steam Deck with NFC cards, stickers and 3d-printed cartridges](https://raw.githubusercontent.com/kmturley/decky-links/refs/heads/main/decky-links-media-types.jpg)

---

## 🛠 Key Features

* **Simple installation:** Utilize Decky Loader for installation and updates.
* **NFC Detection:** High-speed polling via PN532 (UART) for instant tag recognition.
* **Flexible:** Works with Steam games `steam://run/`, Non-Steam games `steam://rungameid/`, and Web URLs `https://`.
* **Pairing Mode:** Custom SteamOS interface to pair games with NFC tags.
* **Auto-Launch:** Automatically launch a game when a tag is tapped. If disabled, the game's details page will be displayed.
* **Auto-Close:** Automatically close games when a tag is removed. If disabled, the Steam menu/pause screen will be displayed.

Watch demo: [https://www.youtube.com/watch?v=dVYhwyseCYk](https://www.youtube.com/watch?v=dVYhwyseCYk)

---

## Decky Links vs Zaparoo

[Zaparoo](https://zaparoo.org) is a cross‑platform "universal loading" system with support for Windows, Linux, SteamOS, MiSTer, Batocera and more; it comes with a Designer app, scripting API, and a wide range of reader types (NFC, barcode, MQTT, etc.).

[Decky Links](https://github.com/kmturley/decky-links) is intentionally narrower scope with deeper SteamOS/Steam Deck integration and utilizes Steam’s native URI launch and quit behaviour. The result is a simpler, lower‑overhead experience for Deck owners who just want to flash a tag and launch games without needing a separate service.

### Key differences

* **Platform scope** – Zaparoo runs everywhere; Decky Links is SteamOS‑only.
* **Integration depth** – Decky Links uses Steam client APIs and respects Steam’s session rules; Zaparoo is more generic.
* **Setup** – Zaparoo requires installing a core service and (optionally) the Designer/Mobile app; Decky Links installs via Decky Loader with card pairing built into the plugin UI.
* **Flexibility vs simplicity** – Zaparoo’s feature set (zap:// URIs, scripting, multiple reader types, queueing, media player support) is broader but also more complex. Decky Links keeps behaviour deterministic (single active card, no stacking/re‑launch) and its URI allowlist is strictly limited.
* **Community & tooling** – Zaparoo has a larger community, more active development, and hardware partners; Decky Links is a small niche project aimed at Steam Deck enthusiasts.

**When to choose which?**
Use Zaparoo if you need a unified solution across multiple devices or want advanced rules and APIs; pick Decky Links when you just want a lightweight, Steam‑native NFC launcher on SteamOS and don’t need the extra bells and whistles.

---

## 🏗 Architecture

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
- allowlisted launch URIs only (`steam://run/*`, `steam://rungameid/*`, `https://*`)
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
- Current allowlist is intentionally strict: `steam://run/*`, `steam://rungameid/*`, and `https://` only.
- Setting updates are validated server-side before persistence.
- Settings loaded from disk are validated before being applied.
- Mifare Classic writes skip trailer blocks to avoid key/access-bit corruption.
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

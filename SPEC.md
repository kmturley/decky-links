# 🎴 Decky Links – Specification v1

## 1. Overview

Decky Links enables launching games and other URIs on SteamOS by tapping NFC cards. Each NFC tag contains a portable URI payload. When tapped, the system launches the associated URI if no game is currently running.

The system is designed to:

* Be portable across devices
* Avoid indexing Steam libraries
* Avoid dependence on private Steam APIs
* Use SteamOS-native behaviors for quitting games
* Provide predictable, deterministic behavior
* Be installable as a self-contained extension (v1 scope)
* Support both native Steam titles and non-Steam shortcuts

---

## 2. Core Principles

1. **URI-Based Launching**

   * NFC tags store a `uri` string.
   * The system validates URI protocol against an allowlist.
   * `steam://` URIs are launched through Steam client APIs.
   * `https://` URIs are launched through system URI handling (`xdg-open`).

2. **Single Active Card Model**

   * Only the first detected NFC tag is considered active.
   * Additional tags are ignored while a game is running.

3. **No Game Stacking**

   * If any game is running, no new URI is launched.
   * Prevents save corruption or lost progress.

4. **No Auto-Relaunch**

   * If a user manually exits a game, it does not relaunch.
   * Relaunch requires card removal and reinsertion.

5. **Steam-Native Quit Behavior**

   * On card removal during gameplay:

     * Default: Open Steam menu (Steam button)
     * Optional: Force quit (Steam button + B button hold for several seconds)

6. **Portable Tags**

   * Tags are writable.
   * Tags are not permanently locked.
   * All required launch data is stored on the tag.

---

## 3. NFC Tag Format

### 3.1 Storage Format

NFC tags store a URI payload in a single **NDEF URI record** (NDEF Record Type Name `U`).

> **Implementation decision**: NDEF URI records are used instead of JSON inside a text record.
> This was a deliberate choice to maximise compatibility with standard NFC hardware, readers,
> mobile apps, and operating systems that natively understand NDEF URI records without
> requiring any custom parsing layer.

### 3.2 Format

The tag contains a single NDEF message consisting of one URI record.

Example URI stored on tag:

```
steam://rungameid/400
```

Non-Steam shortcuts use:

```
steam://rungameid/<gameID64>
```

where `<gameID64>` is Steam's 64-bit game identifier for the shortcut.

The URI is written/read using an NDEF TLV wrapper (Type `0x03` / Length / Value / `0xFE` terminator),
as is standard for Type 2 and Mifare Classic NFC tags.

### 3.3 Requirements

* `uri` (string): approved URI (see §4 allowlist)
* Must be UTF-8 encoded
* Total NDEF payload must fit within NTAG213 minimum capacity (~140 bytes usable after TLV overhead)

### 3.4 Compatibility

Because the format is standard NDEF, tags written by this plugin can be read by:

* Any standard NFC reader app on Android or iOS
* Any PC/SC compatible reader with NDEF support
* Other Decky Links installations

The system must gracefully ignore tags that contain NDEF records of unexpected types.

---

## 4. Supported URI Types

Protocol allowlist. Only allow specific URI schemes in v1:

* steam://
* https://

Examples:

* `steam://rungameid/400`
* `https://example.com`

`steam://` launch handling is performed by the frontend/Steam client integration.
`https://` launch handling is performed by backend system URI execution.

---

## 5. System States

### 5.1 State Definitions

#### IDLE

* No NFC reader detected.

#### READY

* NFC reader connected.
* No active card.
* No game running.

#### CARD_PRESENT

* First UID detected.
* URI parsed.
* Awaiting launch decision.

#### GAME_RUNNING

* A game is currently running.
* Active UID locked.

---

## 6. State Transitions

### 6.1 NFC Reader Connected

`IDLE → READY`

### 6.2 Card Inserted (No Game Running)

`READY → GAME_RUNNING`

Actions:

* Play scan audio
* Parse JSON payload
* Launch URI
* Mark UID as active

### 6.3 Card Removed (Game Running)

Trigger:

* Default: Simulate Steam button press
* Optional: Simulate Steam + B hold

Remain in `GAME_RUNNING` until process exits.

### 6.4 Game Exits

`GAME_RUNNING → READY`

Actions:

* Clear active UID
* Do not relaunch if card still present

### 6.5 Manual Game Exit (Card Still Present)

Remain in `READY`

* No automatic relaunch

### 6.6 Card Removed While in READY

No action.

---

## 7. Pairing Mode

### 7.1 Trigger

User selects "Pair Card" while viewing a game.

### 7.2 Behavior

1. Enter pairing mode.
2. Wait for NFC tag.
3. Retrieve URI of current game.
   * Steam title: `steam://run/<appid>`
   * Non-Steam shortcut: `steam://rungameid/<gameID64>`
4. Overwrite tag payload with new JSON.
5. Play confirmation sound.
6. Exit pairing mode.
7. Do NOT launch the game.

### 7.3 Requirements

* Writing must overwrite previous payload.
* Tag must not be permanently locked.
* Pairing mode must disable auto-launch logic temporarily.

---

## 8. Launch Rules

1. If any game is running → do not launch.
2. Only launch if:

   * No game running
   * A card is newly detected
   * No active UID already recorded
3. Only first detected UID is considered active.
4. Subsequent tag reads are ignored until:

   * Card removed AND
   * Game exited

### 8.1 Auto-Launch Setting (`auto_launch`)

* **Enabled**: tapping a valid tag launches the linked game/URI (subject to no-game-running rule).
* **Disabled**: no game launch is performed; for Steam-linked tags, the game details page is opened instead (`steam://open/games/details/<appid>`).

### 8.2 Auto-Close Setting (`auto_close`)

When a paired tag is removed while its game is running:

* **Enabled**: Decky Links requests Steam to terminate the running game.
  * For non-Steam shortcuts, termination uses the paired `rungameid`/gameID64 target.
* **Disabled**: Decky Links does not terminate the game; it opens Steam side menu flow (pause behavior).

---

## 9. Game Detection Requirements

The system must detect whether a game is currently running.

Acceptable strategies (implementation-defined):

* Monitor active Steam game process
* Track process spawned by URI launch
* Query Steam state if available
* Detect fullscreen game window process

The detection method must:

* Prevent multiple concurrent launches
* Detect when game has fully exited

---

## 10. NFC Reader Requirements

* Must support PC/SC compatible readers
* Must detect:

  * Tag present
  * Tag removed
* Must debounce brief removal events
* Must only consider first detected UID

---

## 11. Audio Feedback

Audio feedback should be provided for:

* Card detected
* Pairing success
* Optional: Launch initiated

Audio must be lightweight and non-intrusive.

---

## 12. Error Handling

### Invalid Tag Data

If JSON parsing fails:

* Play error sound
* Do not attempt launch

### Missing URI Field

* Play error sound
* Ignore tag

### Reader Disconnected During Game

* No action

---

## 13. Non-Goals (v1)

* Desktop Mode support
* Cross-platform support
* Parental restrictions
* Multiple simultaneous card management
* Custom UI overlays for quitting
* Metadata storage (title, art, etc.)
* Tag locking

---

## 14. Implementation Considerations (Technology-Agnostic)

The system requires:

1. NFC polling loop
2. NDEF read/write support
3. System URI launching capability
4. Process or game-running detection
5. Input simulation (Steam button / Steam + B)
6. Lightweight state machine controller

Key technical unknowns to validate early:

* NFC reader access in SteamOS Game Mode
* Reliable detection of running game
* Ability to simulate Steam button inputs
* Reliable removal detection without excessive polling

---

## 15. Suggested Initial Development Milestones

1. Prototype NFC detection and UID logging.
2. Implement NDEF JSON parsing.
3. Implement URI launch test.
4. Implement game-running detection.
5. Implement state machine controller.
6. Add pairing write flow.
7. Add removal-triggered quit behavior.
8. Add audio feedback.

---

## 16. Design Constraints

* Must behave deterministically.
* Must never auto-launch over an active game.
* Must not relaunch unless card is reinserted.
* Must require no manual background service installation (v1 goal).
* Must remain minimal and stable.

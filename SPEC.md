# 🎴 Decky Links – Specification v1

> [!IMPORTANT]
> **Scope.** This spec was written when NFC was the only trigger, and the code
> still cites it by section (`Spec §6.4`, `Spec §7` …) — those references are
> live, and the state machine, launch rules, pairing flow, audio feedback and
> error handling below are all still what the plugin does.
>
> **Read "card" as "medium" throughout.** A disk in a drive or a QR code in
> frame is governed by the same rules as a tag on a reader. Where the two
> genuinely differ, the section says so. Sections superseded since v1 are marked
> inline. For the trigger list and architecture see [the README](README.md).

## 1. Overview

Decky Links enables launching games and other URIs on SteamOS by presenting
physical media — tapping an NFC tag, inserting a disk, showing a QR code. Each
medium carries a portable URI payload. When presented, the system launches the
associated URI if no game is currently running.

The system is designed to:

* Be portable across devices
* Avoid indexing Steam libraries
* Avoid dependence on private Steam APIs
* Use SteamOS-native behaviors for quitting games
* Provide predictable, deterministic behavior
* Be installable as a self-contained extension
* Support both native Steam games and non-Steam games

---

## 2. Core Principles

1. **URI-Based Launching**

   * Media store a `uri` string — in an NDEF record on a tag, in
     `decky-links.json` on a disk, encoded directly in a QR code.
   * The system validates URI protocol against an allowlist.
   * `steam://` URIs are launched through Steam client APIs.
   * `https://` URIs are launched through system URI handling (`xdg-open`).

2. **One Active Medium Per Source** *(superseded: was "Single Active Card")*

   * Each source holds at most one medium. A tag on the reader and a disk in the
     drive are both active at once, each with its own state.
   * A second medium on the *same* source is reported as a collision and ignored.
   * Only the medium that launched the running game may quit it.

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

6. **Portable Media**

   * Media are writable and never permanently locked.
   * All required launch data is stored on the medium itself, so it works on any
     Deck running the plugin — there is no database to carry with it.

---

## 3. Media Payload Formats

The NFC format is specified in full because it is the most constrained.
Other media are covered in §3.5.

Decky Links is written to communicate with **ISO14443‑A** type tags.  Two
families are explicitly supported:

* **Mifare Classic 1K/4K** – the small white cards that ship with many PN532
  kits.  These require authentication with one of the well‑known default
  keys and provide 16‑byte blocks starting at block 4.  This is the original
  hardware supported by the plugin.
* **NTAG21x** (NTAG213, NTAG215, etc.) – the black game‑copy cards many users
  purchase online.  These tags do **not** implement Classic authentication and
  instead expose 4‑byte pages; the code automatically falls back to the
  NTAG read/write commands when a Classic auth attempt fails.

These two formats are functionally equivalent from the user's perspective –
both can encode a single NDEF URI record – but the plugin handles them
slightly differently under the hood.  Choosing either family will work as
long as the card has sufficient capacity for the URI (see §3.3 below).

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

### 3.3 Capacity

* `uri` (string): approved URI (see §4 allowlist)
* Must be UTF-8 encoded
* Capacity is **read from the tag**, not assumed: page 3 of an NTAG21x holds a
  capability container whose third byte is the usable size divided by 8 (144
  bytes on an NTAG213, 496 on a 215, 872 on a 216)
* A payload that does not fit is refused **before the first page write**, so a
  failed pair leaves the tag's previous value intact
* For Mifare Classic tags, writes must avoid sector trailer blocks (key/access-bit blocks)

### 3.4 Compatibility

Because the format is standard NDEF, tags written by this plugin can be read by:

* Any standard NFC reader app on Android or iOS
* Any PC/SC compatible reader with NDEF support
* Other Decky Links installations

The system must gracefully ignore tags that contain NDEF records of unexpected types.

### 3.5 Other media

Added after v1. All carry the same URI and obey the same allowlist.

| Medium | Format |
| --- | --- |
| Storage (floppy, optical, memory card, USB) | `decky-links.json` at the filesystem root: `{"version": 1, "uri": "…", "title": "…", "icon": "…"}`. Mounted read-only; remounted read-write only for the moment of a pair. |
| QR code | The URI encoded directly, at error-correction level Q. Generated by the plugin rather than written to — see [the README](README.md#features). |
| MQTT / serial / file watch | The URI as a message or file, opt-in and off by default. |

---

## 4. Supported URI Types

Protocol allowlist. Only allow specific URI targets in v1:

* `steam://run/<appid>`
* `steam://rungameid/<gameID64>`
* `https://...`

Examples:

* `steam://run/400`
* `steam://rungameid/400`
* `https://example.com`

`steam://run/*` and `steam://rungameid/*` launch handling is performed by the frontend/Steam client integration.
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
* Parse NDEF URI payload
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
4. Overwrite tag payload with a new NDEF URI record.
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

If NDEF URI parsing fails:

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
* Custom UI overlays for quitting
* Tag locking

Since shipped, and no longer non-goals:

* **Multiple simultaneous media** — one per source, tracked independently.
* **Metadata storage** — storage payloads carry optional `title` and `icon`.
* **Parental restrictions** — see §16. Listed here as a non-goal in v1 on the
  grounds that a launcher should not also be a parental control. That still
  holds, and is why the plugin supplies the *key* while Steam's Family View
  supplies the lock.

---

## 16. Restricted Mode

### 16.1 The key

A key is a medium carrying `decky-links://key/<token>`, where the token is 128
random bits, written through the ordinary pairing path. The device stores only
its SHA-256. Any medium the plugin can write can be a key.

Registering one is *targeted at a trigger*, for the same reason pairing a game
is (§7): with a tag on the reader and a stick in a drive, an untargeted write
goes to whichever source the backend reads first and the user cannot say which.
The panel arms the Triggers list into a "choose the key" state, and the row
pressed is the target — `register_key` takes that `source_id`. A medium that
already holds a game is confirmed with a second press, since registering over
it destroys that pairing.

The token is committed only once the write succeeds, and any pending token is
dropped when a pairing is cancelled or re-armed — otherwise a cancelled
registration would be committed by whichever pairing succeeded next, registering
a key whose token is on no medium at all.

The scheme is deliberately outside the §4 allowlist: a control payload is not
something a tapped card may launch, so a copy reaching any other code path is
rejected as an unknown scheme. It is recognised before the allowlist is
consulted, and its token never reaches the frontend, the media registry, or any
emitted event.

### 16.2 States

Two facts, and only one of them is stored:

* **Restricted mode is on** when a key is registered (`restricted.key_hash` is
  non-empty). Registering a key switches it on; "Disable Key" switches it off.
* **The plugin is locked** when restricted mode is on and the key is *not*
  presented on any source. This is derived from the media registry on every
  read and never persisted.

Deriving the lock is the design. A stored `locked` flag is a second answer to a
question the registry already answers, and the two disagree the moment anything
changes while the plugin is not running — which is exactly how a user ends up
with a key that says one thing and a panel that says another. A Deck that boots
with the key out is locked because the key is out.

There is therefore no RPC that locks or unlocks, and no lock control in the
panel.

Presenting a key payload that is *not* the registered one changes nothing and is
reported to the user; it remains an ordinary medium, and may be paired to a
game like any other.

While locked, the backend refuses: pairing, `set_setting`, `set_source_setting`,
`format_media`, `set_tag_key`, `lock_sector`, `simulate_tag`, and every restricted
RPC other than reading the state.

### 16.2.1 Disabling the key

`disable_key` clears the stored hash *and* erases the payload from the medium —
`MediaSource.erase`, which storage implements by deleting `decky-links.json` and
the default implements by writing an empty URI. Both halves or neither: a medium
still carrying a token the device has forgotten reads as an unknown key forever,
and a registered hash with no medium is a lock with no key. If the erase fails
the hash is kept, so the user is told rather than left with the two out of step.

It requires the key to be present, which being unlocked already guarantees.

### 16.3 The launch rule

While locked, a game may run only if a medium vouches for it:

1. a medium launched it (`MediaRegistry.launch_origin` is set), or
2. a medium naming it is presented right now.

The second clause is not redundant: with `auto_launch` disabled the plugin only
opens the game's page and the user presses Play, producing a launch with no
attribution. Comparison goes through `uri.launch_appid`, because a medium
carries a gameID64 for a non-Steam shortcut while Steam reports the app id.

Anything else is reported as `restricted_game` and terminated by the frontend,
which owns the mechanism. Evaluated only when a game *starts*: locking
mid-session never terminates the game being played.

This is a deterrent, not a boundary — the game visibly starts before it closes.

### 16.4 Family View, where it exists

Family View is Steam's older per-account PIN mode, and the only Valve control
that restricts the account holding the library. Steam Families, which replaced
it, applies its controls to *child* accounts and therefore cannot restrict the
account a shared Deck is signed into.

The client offers Family View setup only to accounts where `wasEverEnabled` is
true, so it cannot be relied on and is not the mechanism for §16.3. Where it is
already configured, locking also calls `SteamClient.Parental.LockParentalLock`
(no secret needed) and unlocking calls `UnlockParentalLock(pin)` when the user
stored the PIN.

Steam's parental store is located by shape, never by webpack module id: ids
change with every client build.

### 16.5 Non-goals within restricted mode

* Blocking Steam's own menus — the Store, Settings and Desktop Mode stay
  reachable. Steam's internal restricted mode could do it, but it is undocumented,
  frontend-only, resets on a client restart and carries a hardcoded escape PIN.
* Replacing the Steam Home view with a restricted overlay.
* Hiding other Decky Loader plugins — another plugin's UI is not this one's to
  hide, and Decky Loader has no lock of its own.
* Intercepting the STEAM button inside a running game. Not exposed to plugins.

---

## 14. Implementation Considerations (Technology-Agnostic)

The system requires:

1. NFC polling loop
2. NDEF URI read/write support
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

## 15. Design Constraints

* Must behave deterministically.
* Must never auto-launch over an active game.
* Must not relaunch unless card is reinserted.
* Must require no manual background service installation (v1 goal).
* Must remain minimal and stable.

# Hardware test plan

Everything below runs from the repo root on the dev machine, with the Deck on
the same network. Each step says what to do on the Deck, what to run, and what
counts as a pass.

Capture as you go:

```bash
mkdir -p test-run && cd test-run     # gitignored scratch
```

then redirect each command, e.g. `pnpm deck:status > 02-drive.txt 2>&1`. If a
step fails, keep its file — the log around a failure is the useful part.

**Read this first:** steps 1–3 must pass before anything else is meaningful. A
stale deploy or a leaked mount makes every later result a lie.

---

## 0. Deploy

```bash
pnpm build && ./.vscode/build.sh          # produces out/Decky Links.zip
```

Install the zip through Decky's plugin store ("Install from ZIP"), or run the
VS Code `deploy` task, which copies, extracts **and restarts the loader**. The
restart is not optional: the plugin's uid is fixed when the process spawns, so
a deploy without one leaves the old process running.

```bash
pnpm deck:status > 00-deploy.txt 2>&1
```

**Pass:** `STALE` is not reported, `flags` contains `root`, and the plugin
process user is `root`. If the user is `deck`, the loader did not restart.

Then check the interpreter the plugin actually runs under:

```bash
pnpm logs | grep -A4 "Python runtime" >> 00-deploy.txt
```

**Pass:** the version matches `DECK_PYTHON` in `.vscode/build.sh`, and all three
compiled dependencies report `OK`. They are the only ones that can be built for
the wrong runtime — everything else we vendor is pure Python. A mismatch here
disables the camera and silently stores NFC keys unencrypted, so it is worth
checking on every deploy and after any Decky Loader update.

---

## 1. Clean start — no drive, no reader

Unplug everything first.

```bash
pnpm deck:restart
pnpm logs > 01-clean.txt 2>&1
```

**Pass:**
- `euid=0` in the startup line.
- No repeated reconnect attempts for camera / MQTT / serial / file-watch. Those
  ship disabled, and a retry loop against absent hardware is the C4 regression.
- No `must be superuser to use mount`.

**Panel:** Triggers list shows NFC `ON` and everything else `OFF` — including
Floppy, which is off by default now. The NFC row reads `Not connected` until the
reader is in. If any storage category is on, an old settings file survived; that
is fine, note it, and switch **Floppy ON** before the tests below that use it.

---

## 2. Mount leak — do this before anything else

The one whose failure corrupts every later test: a leaked mount pins the device
node, so the next drive comes up as `/dev/sdb`, then `sdc`.

Plug the floppy drive in and out **five times**, waiting for the panel to
update each time. Insert and eject a disk at least twice in between.

```bash
pnpm deck:status > 02-leak.txt 2>&1
ssh deck@steamdeck.local "cat /proc/mounts | grep decky-links" > 02-mounts.txt 2>&1
```

**Pass:** the drive is still `/dev/sda` in `02-leak.txt`, and `02-mounts.txt` is
**empty** with the disk ejected. Any surviving `/tmp/decky-links-*` line is the
leak, and the device letter drifting is the symptom you already saw.

---

## 3. Drive present before the plugin starts

Never worked until now — `_scan_existing_devices` crashed on its first
removable drive and the whole scan died silently.

With the floppy drive plugged in **and a paired disk already in it**:

```bash
pnpm deck:restart
sleep 15
pnpm logs > 03-startup-scan.txt 2>&1
pnpm deck:status >> 03-startup-scan.txt 2>&1
```

**Pass:** the log has `StorageSource: found existing media` **or**
`media already inserted in /dev/sda`, and no `startup device scan failed`. The
Floppy row shows the game name without touching the disk.

---

## 4. Loading state

New. A floppy can take a minute to mount, and the row used to say `No disk` for
that whole time.

With the drive connected and empty, insert a disk and **watch the Floppy row**.

**Pass:** `No disk` → `💾 Reading disk…` with a spinner → the game name (or
`Empty disk`). The middle state must appear within about a second of the disk
going in, well before the mount finishes.

```bash
pnpm logs > 04-loading.txt 2>&1
```

The log should show `has media — mounting` and then the mount result.

**Fail modes worth distinguishing:** the row jumping straight from `No disk` to
the game name means the disk was already mounted (fine, retry with a disk that
was not); the row stuck on `Reading disk…` means a mount path returned nothing
without an event — capture the log.

---

## 5. Drive categories

**5a.** Switch **Floppy OFF** in the panel, then insert a disk.

```bash
pnpm logs > 05a-floppy-off.txt 2>&1
```

**Pass:** log says `ignoring /dev/sda — floppy drives are switched off`, no
mount is attempted, and the row shows no media. Switch it back on.

**5b.** Plug in a USB stick with **USB Storage OFF** (the default).

```bash
pnpm deck:status > 05b-usb.txt 2>&1
```

**Pass:** the USB Storage row shows the stick as connected but nothing is
mounted — the toggle has to be discoverable, so presence is reported even for a
category that is switched off. `grep decky-links /proc/mounts` stays empty.

---

## 6. NFC end to end

Reader plugged in, disk drive unplugged.

1. Open a game's page, open the panel, press **Pair** on the NFC row.
2. Tap a blank tag.
3. Remove the tag, tap it again.

**Pass:** pair succeeds with a sound; the row shows the game name; the second
tap launches it.

```bash
pnpm logs > 06-nfc.txt 2>&1
```

**6b. Tag too small.** If you have an NTAG213 (144 bytes) as well as a 215,
pair a game with a long name / non-Steam shortcut to it.

**Pass:** either it writes correctly, or it is refused with `Tag too small:
needs N bytes, holds M` — and the tag still reads its **old** value afterwards,
because the check happens before the first page write. The capacity is now read
from the tag rather than assumed, so `holds 144` on a 213 is the correct answer,
not a bug.

---

## 7. Two triggers at once — the point of the whole panel

Reader **and** drive connected. Tag on the reader, disk in the drive, both
paired to *different* games.

**Pass:** both rows show their own game simultaneously. Before the per-source
registry this was impossible — one slot held whichever arrived last.

Now open a **third** game's page and press **Pair on the Floppy row only**.

**Pass:** the disk is rewritten to the third game; **the tag is untouched**.
This is the targeted-pairing gate. If the tag changes too, `pairing_source_id`
is not being honoured.

```bash
pnpm logs > 07-two-triggers.txt 2>&1
```

---

## 8. Launch and quit

`auto_close` must be ON for the second half.

1. Tap a paired tag → game launches.
2. While it runs, remove the tag → game closes.
3. Relaunch, then remove the **floppy** while the game launched from the *tag*
   is running.

**Pass on 3:** nothing happens. Only the medium that launched the game may
close it — that is `_launch_origin`, and C3 moved the decision to the backend.

```bash
pnpm logs > 08-launch-quit.txt 2>&1
```

---

## 9. Unformatted disk

Insert a blank, unformatted floppy.

**Pass:** the row shows `Unformatted disk` within the mount timeout (~20s), the
message fits on one line, and the disk is **not** retried on every subsequent
udev event — the log should show one mount failure, not a stream. Eject and
reinsert: exactly one more attempt.

```bash
pnpm logs > 09-unformatted.txt 2>&1
```

To make one: `pnpm deck:format /dev/sda` writes FAT12 (destructive).

---

## 10. Camera (only if you want the camera trigger)

`pyzbar` was replaced with `zxing-cpp`, which needs no system packages. Plug in
the webcam, switch **Camera ON** in the panel, show a QR code on your phone.

Make one from a paired game's URI, e.g. `steam://rungameid/220`.

```bash
pnpm logs > 10-camera.txt 2>&1
ssh deck@steamdeck.local "ls -l /dev/video*" >> 10-camera.txt 2>&1
```

**Pass:** `CameraSource: ready on /dev/videoN`, and holding the code up
launches the game.

If it says `cannot import zxingcpp` or `cannot import PIL.Image`, the vendored
wheels are built for the wrong interpreter. Check step 0's runtime line — the
plugin runs under Decky Loader's own Python (3.11.7 as of 2026-08-01), *not*
SteamOS's `python3` (3.13.5) — and rebuild with `DECK_PYTHON=<that version>`.

---

## Reporting back

The useful summary is the step numbers that failed plus their capture files.
For anything that fails, `pnpm logs` immediately afterwards is worth more than
a description — the udev action sequence around the failure is usually the
whole answer.

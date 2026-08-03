# Decky Links — Architecture Audit

**Reviewer:** Principal Architect review, uncompromising pass
**Date:** 2026-08-01
**Branch:** `feature/refactor` @ `959c368`
**Scope:** `main.py`, `sources/`, `nfc/`, `cards/`, `src/`, build & test tooling

---

## Executive Summary

The `MediaSource` abstraction is genuinely good — a clean event-sourced boundary that let six wildly different triggers (NFC, block devices, webcam, MQTT, serial, filesystem) land behind one contract without the plugin knowing which is which. That design is sound and worth keeping.

What undermines it is everything around it: the entire plugin runs on a **single asyncio event loop that most sources block synchronously for seconds at a time**, it runs as **root and mounts attacker-supplied filesystems with no hardening**, and although 634 tests exist and a well-built linux/amd64 container toolchain exists, **the two are never connected — there is no CI and no containerized test target**, so nothing gates any of this automatically. This is viable software for one handheld device, but three of its load-bearing assumptions are wrong today, not eventually.

> **Note on test runs.** This plugin targets Linux x86_64 (Steam Deck). The audit host is macOS arm64, so any test run here is not a valid signal and no result below should be read as one. Where I cite test output it is to characterise the *tooling gap*, never the code.

> **Everything above and in the dimension breakdown below describes the code *as
> audited*, at `959c368`.** It is left unedited so the findings stay legible as
> findings. Current state is the Status section immediately below, and every
> finding is marked ✅ fixed / ⚠️ partly / ⬜ open where it appears.

---

## Status

Work is on `fix/audit-critical-findings`. **All three fatal flaws are fixed
and Phases A–E are complete.** Suite: **914 passed, 6 skipped**, in
linux/amd64. Frontend builds. Plugin zip builds.

**None of it has been run on a Steam Deck yet.** The container proves the
logic holds; it cannot prove a tag still launches a game.

| Phase | What it was | State |
|---|---|---|
| Fatal #1 | Blocking I/O on the shared event loop | ✅ `8e5685e` |
| Fatal #2 | Unhardened root mounts of untrusted media | ✅ `ba72cf0` |
| Fatal #3 | Unwired signing subsystem / key material at rest | ✅ `dbb9118` |
| **A** | Stop the bleeding — 10 discrete fixes | ✅ complete |
| **B** | Connect the tests to an environment that can run them | ✅ complete |
| **C** | Unify the trust boundary | ✅ `7189396` |
| **D** | Break up the god object | ⚠️ substantially — see below |
| **E** | Pay down the abstraction leak | ✅ complete |

Suite size tracks the work: 634 at audit → 656 (A+B) → 742 (C) → 872 (D) →
914 (E).

### What is left

Three things, none of them blocking a hardware test:

1. **The router still lives on `Plugin`** (rest of Phase D). The event loop and
   media handlers, ~350 lines, are genuinely coupled to `self.state` and
   `decky.emit` rather than incidentally so. Splitting them needs a decision
   about who owns state transitions, not a file move. **Needs your input.**
2. **The `E4` compatibility shim.** `media_detected` / `media_removed` /
   `source_connection` are emitted alongside their old NFC-shaped names. Retire
   the aliases once a release has shipped with both.
3. **Smaller findings deliberately left open** — listed under "Open findings"
   at the end of the dimension breakdown. Each is real; none is load-bearing.

Also carried forward, from C4: **the panel has no field to display or copy the
auto-minted MQTT secret.** Enabling MQTT mints one, and there is currently no
way to read it out of the UI to give to a publisher.

### What extraction found

Each of these was invisible while the code was a method on a 1,800-line class,
and obvious within minutes of the extracted module having its own tests:

1. **`settings_schema.py` was never packaged.** The decky CLI zips a fixed
   allowlist; a top-level module next to `main.py` is not copied. The plugin
   would have failed to start on a Deck with `ImportError`. `tests/test_packaging.py`
   now parses `main.py`'s imports and fails if any local one is missing from
   `build.sh` — the one failure mode the suite structurally could not see,
   because pytest runs from the repo root.
2. **Non-Steam shortcuts could never be paired.** `steam://rungameid/` carries
   a gameID64 for shortcuts, not an app id, and both endpoints were checked
   against `^[0-9]{1,10}$` — a uint32. Every shortcut was rejected inside
   `start_pairing` with only a log line.
3. **`get_sector_info`/`lock_sector` raised with no reader attached**, calling
   `nfc_source._classify_tag` unguarded — an RPC error in the panel instead of
   the empty result the rest of the function expects.

---

## Top 3 Fatal Flaws / Risks

Ranked by severity × proximity.

### 1. Blocking I/O on the shared event loop — freezes the whole plugin, today

> **Fixed in `8e5685e`.** `NfcSource.poll`, `NfcSource.write_uri` and
> `CameraSource.poll` now run their blocking bodies via `asyncio.to_thread`,
> and `MediaSource.poll`'s contract states the rule so the next source does
> not have to rediscover it. The proxmark backend is covered by the NFC
> offload.

Every source task, the event loop, and every frontend RPC share one asyncio loop. Several `poll()` implementations do synchronous blocking work directly on it:

| Location | Blocking call | Worst-case stall |
|---|---|---|
| [camera_source.py:166](sources/camera_source.py#L166) | `subprocess.run(ffmpeg…, timeout=5)` | **5s** |
| [camera_source.py:215](sources/camera_source.py#L215) | `zxingcpp.read_barcodes()` (CPU-bound decode) | 100s of ms |
| [nfc_source.py:294](sources/nfc_source.py#L294) | `reader.read_uid(timeout=0.2)` — blocking serial | 200ms+ |
| [nfc_source.py:517](sources/nfc_source.py#L517), [:729](sources/nfc_source.py#L729) | `time.sleep(0.05)` × 3 keys in classify/write | 150ms |
| [proxmark_backend.py:170](nfc/proxmark_backend.py#L170) | `subprocess.run(timeout=5.0)` per command | **5s** |

During any of these, all six source tasks stop, the event loop stops draining, and the frontend's 2 Hz RPC poll ([BackgroundManager.tsx:502](src/BackgroundManager.tsx#L502)) hangs. With the camera trigger enabled, the plugin is unresponsive for up to 5 seconds out of every poll interval.

The team clearly knows the correct pattern — [storage_source.py:693](sources/storage_source.py#L693) wraps `mount` in `asyncio.to_thread` precisely because a floppy mount takes 20s. It was applied to exactly one source.

**Fix:** every blocking `poll()` body goes through `asyncio.to_thread`, or `MediaSource` grows a `poll_blocking()` that the manager always offloads.

### 2. Root process mounts untrusted filesystems with no hardening

> **Fixed in `ba72cf0`.** The filesystem is probed with `blkid` and checked
> against `ALLOWED_FILESYSTEMS` before any mount, the type is stated
> explicitly with `-t`, and `ro,nosuid,nodev,noexec` is applied to every mount
> *and* repeated on every remount (a remount does not inherit them, and
> pairing goes through that path). Probing first also turns a 20s failed mount
> on an unformatted floppy into a millisecond rejection.

[plugin.json](plugin.json) declares the `root` flag. [storage_source.py:695](sources/storage_source.py#L695):

```python
["mount", "-o", "ro", devnode, tmpdir]
```

No `nosuid`, no `nodev`, no `noexec`, and **no `-t` filesystem allowlist** — so `mount` auto-probes and will happily hand a crafted disk image to any filesystem driver the kernel has (`ntfs`, `hfsplus`, `udf`, `squashfs`, …). In-kernel filesystem parsers are a well-established local-privilege-escalation surface, and this is a root process mounting media that arrives from outside the trust boundary by definition.

`floppy` is enabled by default ([main.py:143-148](main.py#L143-L148)); a USB floppy emulator is indistinguishable from a real one to udev. [`write_uri`](sources/storage_source.py#L598) also remounts `rw`.

**Fix:** `mount -t <allowlist> -o ro,nosuid,nodev,noexec`. One line, removes the class.

### 3. The trust model rests on one function, and the crypto meant to back it is unwired

> **Resolved in `dbb9118`.** The signing subsystem was deleted — it was
> unreachable code implying a protection the plugin did not have, and the
> spec never asked for it. `KeyManager` (which *is* used) keeps its keys at
> 0600 in a 0700 directory, written atomically; encryption no longer degrades
> silently to plaintext, and enabling it migrates an existing plaintext file
> instead of destroying it. `_validate_uri` remains the sole trust boundary —
> now honestly so, with nothing implying otherwise.

`_validate_uri` ([main.py:867](main.py#L867)) is the *entire* control preventing arbitrary media from launching arbitrary things. It is reasonable work — allowlisted schemes, length cap, app-ID regex — but it is the only layer, and:

- **`SignatureManager` is decorative.** It is constructed, passed into `NfcSource`, assigned at [nfc_source.py:88](sources/nfc_source.py#L88) — and *never referenced again anywhere in the codebase*. No media read path verifies a signature. `verify_signature` is reachable only as a manual frontend RPC. The signing subsystem (262 lines + 15 tests) provides zero runtime protection.
- **Private keys are stored in plaintext.** [signature_manager.py:69](nfc/signature_manager.py#L69) writes PKCS8 with `NoEncryption()` to `signing_keys.json`, and nothing anywhere calls `chmod` — verified, zero occurrences repo-wide.
- **Mifare keys are effectively always plaintext.** [key_manager.py:33](nfc/key_manager.py#L33) encrypts only if `DECKY_LINKS_KEY_ENCRYPTION_KEY` is set. Nothing in this repo ever sets it. Worse, [key_manager.py:105-111](nfc/key_manager.py#L105-L111) *silently falls back to plaintext* if encryption throws, and returns as if it succeeded.
- **The env-var path has a one-way migration trap.** If a user does set the key, `load()` finds a plaintext file, fails to decrypt, and `return`s ([key_manager.py:63-66](nfc/key_manager.py#L63-L66)) — every stored key silently disappears.

**Fix:** either verify signatures on the read path and encrypt key material at rest, or delete the subsystem. Shipping security theatre is worse than shipping neither.

---

## Dimension-by-Dimension Breakdown

### 1. Scalability & Bottlenecks

This is a single-process plugin on one handheld. "Horizontal scale" is not the axis and pretending otherwise would be cargo-culting. The real axis is **cost per poll cycle on a battery-powered device**, and that is where the problems are.

- ✅ **Blocking-loop serialization** — see Fatal Flaw #1. This was the dominant bottleneck. Fixed in `8e5685e`.
- ✅ **MQTT throughput ceiling + unbounded buffer.** `poll()` drains exactly one message per cycle at `poll_interval = 0.1` → hard **10 msg/s** ceiling ([mqtt_source.py:114-127](sources/mqtt_source.py#L114-L127)). The paho callback thread appends without limit to a `deque()` with **no `maxlen`** ([mqtt_source.py:43](sources/mqtt_source.py#L43)). A publisher exceeding 10 msg/s grows that deque without bound — unbounded memory on a device with no OOM headroom. There is no backpressure path back to the broker.
  > **Fixed (A4).** `deque(maxlen=MAX_PENDING)` with a dropped-message counter, because a silent discard is a trigger that vanishes with no explanation. The 10 msg/s ceiling is left as-is — it is the right cadence for "someone tapped a thing", not a defect.
- ✅ **Duplicated push and pull for the same data.** The backend pushes `source_statuses` every ≤2s ([main.py:491](main.py#L491)) *and* the frontend polls `get_source_statuses` every 5s, `get_reader_status` + `get_tag_status` every 500ms ([BackgroundManager.tsx:471-533](src/BackgroundManager.tsx#L471-L533)). Two mechanisms, same state. The poll loop starts at plugin load and **never stops when the panel is closed** — 2 RPC round-trips per second, forever, on battery.
  > **Fixed (E6).** 2 RPC/s → 0.4 RPC/s. The loop still cannot stop when the panel closes — game state has no backend event to push and drives auto-close — but everything the backend already pushes moved to the 5s tick, where it is a dropped-event backstop rather than the primary path. `get_tag_status` was retired outright in E4.
- ⚠️ **Redundant status publishes.** `_publish_statuses()` is called at the end of `_handle_source_event` ([main.py:602](main.py#L602)) *and* again at the end of the same event-loop iteration ([main.py:484](main.py#L484)).
  > **Mitigated, not removed.** Both calls remain, but `_publish_statuses` now diffs against `_last_statuses` and emits nothing when unchanged, so the second is a comparison rather than a message. The duplicate call is still there to delete.
- ⬜ **Post-poll sleep inflates the effective interval.** [manager.py:209](sources/manager.py#L209) sleeps `poll_interval` *after* the poll returns, so real cadence is `interval + work_time`. With a 5s camera capture and a 1s interval, the camera actually samples every 6s. **Open** — see Open findings.

### 2. Coupling & Cohesion

- ⚠️ **`main.py` is a 1,834-line god object.** One `Plugin` class holds the settings manager, state machine, event loop, URI validation, pairing, launching, audio, and ~35 RPC methods spanning NFC key management, Mifare sector locking, ECDSA signing, and printable-card rendering. Cohesion is near zero — sector locking and QR card generation share nothing but a `self`.
  > **1,834 → 1,256 lines (-31%).** Six modules now live in `decky_links/`. What remains is the lifecycle, the RPC surface, and the router — and the router is the part that needs a design decision, not a file move. See "What is left".
- ✅ **Six sources are tracked twice.** `Plugin` keeps named attributes (`self.nfc_source`, `self.storage_source`, …) *and* registers them all with `SourceManager`. `_all_sources()` ([main.py:504](main.py#L504)) exists solely to reconcile the two. Adding a seventh source requires editing **eight** places: `Plugin.__init__`, `_main`, `_all_sources`, `get_source_statuses`, `set_source_setting`'s allowlist, `SourceType` (Python), `SourceType` (TypeScript), and `sourceIcon()`. The abstraction does not yet pay for itself.
  > **Fixed (E1, E3).** The registry is the only record; `nfc_source`/`storage_source` are lookups by type. `sources.source_classes()` collapses the first three of those eight edits into one entry.
- ✅ **NFC leaks straight through the generic layer.** `current_tag_uid/uri/meta`, `get_tag_status`, `get_reader_status`, the `reader_status` event, the `tag_detected`/`tag_removed` event names, and `NFC_SETTING_KEYS` being special-cased inside `SettingsManager.get/set` ([main.py:210-226](main.py#L210-L226)). The code acknowledges this ("NFC-flavoured for historical reasons", [main.py:107](main.py#L107)) — acknowledged debt is still debt.
  > **Mostly fixed (E4).** The three misleading *event* names are now `media_detected`, `media_removed` and `source_connection`, with the old names emitted alongside as a shim. `get_tag_status` is gone. What deliberately stays: `get_reader_status` and `current_tag_*`, which describe the NFC reader specifically and are read by the tag-write path; `NFC_SETTING_KEYS`, which is the settings file's historical top-level shape. Those are NFC-shaped because they *are* NFC — the problem was generic events wearing NFC names.
- ✅ **The plugin reaches into a subclass.** [main.py:1767](main.py#L1767) does `hasattr(source, "drive_kinds_present")` and [main.py:38](main.py#L38) imports `DEFAULT_DRIVE_KINDS` from `storage_source` to compute defaults it should be asking the source for. The `MediaSource` contract doesn't cover drive categories, so the caller duck-types around it.
  > **Fixed (E2).** `sub_devices()` is part of the `MediaSource` contract and returns presence *and* enablement together. `main.py` no longer imports anything from `storage_source`.
- ⚠️ **Implicit shared mutable state across a thread boundary.** `get_source_settings()` ([main.py:245](main.py#L245)) returns the *live* nested dict, which is handed to each source constructor and then mutated in place by `set_source_setting` ([main.py:1818](main.py#L1818)). `MqttSource._on_message` reads `self._settings["secret"]` from paho's I/O thread while the event loop may be writing it. It works today by GIL accident, not by design.
  > **Consequence removed, mechanism kept.** The live dict is still shared — that is *how* toggling a source in the panel takes effect without a restart, so it is load-bearing. What changed is that the dangerous read fails closed: a secret that reads back empty mid-write now rejects the message rather than accepting anything. Also `hmac.compare_digest` instead of `!=`.
- ✅ **The frontend holds two competing models of the same state.** `sharedState.tagUid`/`tagUri` is a single global slot that whichever source fired last clobbers — and `sharedState.activeMedia` is the per-source map added specifically to fix that. Both live in the same object and both are maintained. Related: [BackgroundManager.tsx:296](src/BackgroundManager.tsx#L296) — a `tag_removed` without `source_id` wipes **all** `activeMedia`, so one source's removal clears every other source's row.
  > **Fixed (E5, `d6adef6`).** The global slot turned out to be write-only — no component ever read it. Deleting it fixed the "removal clears every row" bug and a uid-normalisation bug where `uri_detected` used the source type of whatever was presented last.

### 3. Error Handling & Resilience

- ✅ **The backoff doesn't cover the failure path that matters.** [manager.py:200-209](sources/manager.py#L200-L209): an exception in `poll()` is logged, sets `was_connected = False`, then falls through to `await asyncio.sleep(source.poll_interval)` — **not** the exponential backoff. `reconnect_delay` only applies when `start()` returns `False`. A source raising on every poll hot-loops at its poll interval — **10 Hz for MQTT** — logging a full traceback each time, forever.
  > **Fixed (A5).** The exception path now doubles from `RECONNECT_MIN` to `RECONNECT_MAX` like the start path. Two regression tests assert the sleeps are the backoff sequence and not `poll_interval`.
- **Silent failures reported as success.**
  - ✅ `SettingsManager.save()` logs and returns on a permissions failure ([main.py:200-202](main.py#L200-L202)); `set_setting` returns `True` to the frontend regardless ([main.py:1137](main.py#L1137)). The UI shows the toggle as saved when nothing was written. — **Fixed (C2).** `save()` returns a bool that callers propagate, and the write is atomic (temp file + rename), so an interrupted save can no longer truncate `settings.json`.
  - ✅ `KeyManager.save()` falls back to plaintext on encryption failure and reports success ([key_manager.py:102-111](nfc/key_manager.py#L102-L111)). — **Fixed (`dbb9118`).** It raises instead, and enabling encryption migrates an existing plaintext file rather than destroying it.
  - ⬜ `_event_loop` catches bare `Exception`, logs, and **drops the event** ([main.py:487](main.py#L487)). No dead-letter, no counter, no user-visible signal. **Open** — see Open findings.
- ✅ **Zombie process accumulation.** `subprocess.Popen` with no `wait()`/reaping in `_play_sound` ([main.py:1096](main.py#L1096)) and `_launch_uri` ([main.py:1055](main.py#L1055)). One `paplay` zombie per scan, for the life of the plugin.
  > **Fixed (A8).** Both go through `Plugin._spawn`, which reaps finished children and uses `start_new_session=True`.
- ⬜ **Over-eager teardown.** `CameraSource.poll` sets `self._active = False` on a *single* failed frame ([camera_source.py:110](sources/camera_source.py#L110)), forcing a full stop/start cycle. A dropped frame is normal for a webcam. **Open** — see Open findings.
- ⬜ **Unawaited cancellation.** `_unload` cancels `polling_task` but never awaits it ([main.py:433-434](main.py#L433-L434)), so unload can return while the loop is still mid-iteration. **Open** — see Open findings.
- ⬜ **Failures the user never sees.** `terminateSteamApp` retries 6×500ms then gives up with a `console.warn` ([BackgroundManager.tsx:177](src/BackgroundManager.tsx#L177)) — auto-close silently doesn't happen, with no toast. **Open** — see Open findings.
- **Credit where due:** the per-source task isolation, the CONNECTED/DISCONNECTED lifecycle, `_reap_stale_mounts` ([storage_source.py:624](sources/storage_source.py#L624)), and the `_unmountable` set that stops 20s floppy retries are all correct, well-reasoned resilience work. Formal circuit breakers would be over-engineering at this scale — the gap is that the backoff that exists doesn't cover the exception path.

### 4. Security & Data Integrity

**Critical**

- **Hardcoded sudo password committed to a public repo.** ✅ **Fixed in `8a8c9fc`.** `package.json`'s `stop`/`start` piped the Deck's stock password into `sudo -S`, and `scripts/deck.sh` and `.vscode/defsettings.json` carried it as a fallback default. It is the SteamDeckHomebrew template's well-known default rather than a user secret, so history was deliberately left alone; what mattered was that the repo stopped being a place a password lives. The password now comes from `DECK_PASSWORD`, else the gitignored `.vscode/settings.json`, else a prompt at the point of use.
- ✅ **Root mount of untrusted filesystems** — Fatal Flaw #2. Fixed in `ba72cf0`.
- ✅ **MQTT is an unauthenticated remote trigger by default.** [mqtt_source.py:77-84](sources/mqtt_source.py#L77-L84): plain `mqtt.Client()`, no TLS, no broker credentials. `secret` defaults to `""` ([main.py:160](main.py#L160)), and the check is skipped entirely when empty ([mqtt_source.py:147](sources/mqtt_source.py#L147)) — so with the toggle on, **anyone who can publish to the topic can launch games and open arbitrary HTTPS URLs on the device**. The secret is stored plaintext in `settings.json` and compared with `!=`. Opt-in caps the blast radius; one toggle is not much of a gate.
  > **Fixed (C4).** `start()` refuses without a secret, comparison is `hmac.compare_digest`, and TLS + broker credentials are supported. Enabling MQTT mints a secret with `secrets.token_urlsafe(24)`, because the panel has no field to type one into and the toggle would otherwise silently do nothing. **Follow-up: that field, so the secret can be read out and given to a publisher.**

**High**

- ✅ **Two divergent validation paths for the same settings.** `_validate_setting` ([main.py:911](main.py#L911)) enforces ranges and a `/dev/` prefix; `set_source_setting` ([main.py:1790](main.py#L1790)) validates **type only**. Consequences: `broker_port` accepts `-1` or `70000`; serial `port` accepts any string, bypassing the `/dev/` check applied to `device_path`; `watch_dir` accepts `/` or `/proc`, pointing a root process's directory scanner at the filesystem root; `poll_interval` accepts `0.0`.
  > **Fixed (C1).** There were *three* copies, not two — `set_source_setting`'s type-only map was the third and the dangerous one. All three now go through `decky_links/settings_schema.py`, where the rules are data.
- ✅ **`_validate_setting` is duplicated verbatim** in `SettingsManager` ([main.py:228](main.py#L228)) and `Plugin` ([main.py:911](main.py#L911)), with a comment admitting it. These will drift. — **Fixed (C1).**
- ✅ **Path traversal via `save_game_card`.** `appid` flows unvalidated from the frontend RPC ([main.py:1708](main.py#L1708)) → `save_card` → `find_art` → `template.format(home=home, appid=appid)` ([qr.py:140](cards/qr.py#L140)). `_validate_uri` guards `uri`; nothing guards `appid`. A `../../../` value makes a root process read an arbitrary path and render it into a PNG the caller reads back. Low proximity (the frontend is the only caller and passes real app IDs) but a genuine boundary gap — `title` is properly sanitised at [qr.py:252](cards/qr.py#L252), so the pattern is understood.
  > **Fixed (A7).** `card_rpcs.save_card` rejects an `appid` that is not a valid app id before it reaches the path builder.
- ✅ **No file permissions on secrets.** Zero `chmod`/`umask` calls repo-wide. `keys.json` and `signing_keys.json` land at default umask.
  > **Fixed (A6, `dbb9118`).** `keys.json` is written atomically at 0600 inside a 0700 directory. `signing_keys.json` no longer exists — the subsystem that wrote it was deleted.

**Medium**

- ✅ **The HTTPS loopback check is porous.** [main.py:903](main.py#L903) rejects only the literal strings `localhost`, `127.0.0.1`, `::1`. Not blocked: `127.0.0.2`, `[::1]`, `0.0.0.0`, RFC1918 addresses, `169.254.169.254`, or any hostname that resolves to loopback. If the intent is SSRF protection it does not hold; if it is only "don't open odd local pages", the comment should say so.
  > **Fixed (C5).** `decky_links.uri.is_local_host` uses `ipaddress` and covers 127/8, bracketed IPv6, IPv4-mapped IPv6, RFC1918, link-local (incl. 169.254.169.254) and `.local`. It also fixes a port bug: `localhost:8080` never matched, because the check compared against `netloc`. The docstring states the scope — it checks the literal, not what a public name resolves to.
- ⬜ **`simulate_tag` is an unconditional debug RPC** ([main.py:1207](main.py#L1207)) — it forges `tag_detected`/`uri_detected` events with caller-supplied data and is exposed to the frontend in production builds. **Open** — see Open findings. (No frontend caller remains, but the RPC is still exposed.)

### 5. Maintainability & Technical Debt

- ✅ **A containerized *build*, but no containerized *test*, and no CI.** [.vscode/build.sh](.vscode/build.sh) is the strongest piece of engineering in this repo: it builds `py_modules/` inside `docker --platform linux/amd64 python:3.11-slim`, then applies three guards — reject macOS binaries, reject extensions tagged for the wrong CPython, and compare abi3 wheels' *minimum* version against `DECK_PYTHON`. The correct linux/amd64 environment already exists and is already scripted. **It is simply never pointed at `pytest`.** `npm test` instead runs `.venv/bin/pytest` on the host, which for an x86_64-Linux-only plugin is the wrong environment by construction. There is **no `.github/`**, so nothing runs the 634 tests anywhere, ever, automatically. This is the single highest-leverage gap in the project: the hard part is done.
  > **Fixed (Phase B).** `.vscode/test.sh` mirrors `build.sh`'s container; `.github/workflows/test.yml` runs the suite on 3.11 and 3.12, re-runs all three wheel guards, and builds the frontend. The suite has gone 634 → 914 tests since, all of them run in the environment they target.
- ✅ **`.venv` is stale.** Its shebangs point at `/Users/kmturley/Sites/decky-links-comparison/decky-links-refactor/`, a path that no longer exists, so `npm test` fails with `bad interpreter`. Minor on its own — it's gitignored and host-local — but it means the one wired-up test command is dead, which is likely why the gap went unnoticed.
  > **Fixed (B4).** `npm test` runs the container. `setup_test_env.sh` is now a signpost to it rather than a second answer. The `.venv` directory itself is gitignored and host-local — delete it by hand if you want it gone.
- ℹ️ **Test results on this host are not a signal.** I ran the suite on macOS arm64 and saw failures traceable to missing `cryptography` and to `py_modules/` holding a Linux-built Pillow. That is the expected and correct outcome of running an x86_64-Linux plugin's tests on Apple Silicon — **not a project defect**, and I am not reporting it as one. The `py_modules/` vendoring behind it is likewise deliberate: it is gitignored (only `.keep` is tracked), regenerated per build, and forced by the decky CLI zipping a fixed path allowlist that excludes `sources/`, `nfc/`, `cards/`, and `assets/`. [main.py:12-13](main.py#L12-L13) puts the checked-out tree ahead of it on `sys.path` specifically so the vendored copies can never shadow files under edit. All of this is documented at length in `build.sh`.
- ⚠️ **`main.py` at 1,834 lines / one class** is the single largest evolution hurdle. Testing `_validate_uri` requires standing up a `Plugin`.
  > **1,834 → 1,256.** `_validate_uri`'s rules are `decky_links/uri.py`, a pure module with 103 tests and no `Plugin` anywhere near them. The router is what remains — see "What is left".
- ✅ **Generated-text artifacts left in source:** [index.tsx:25](src/index.tsx#L25) reads `// (the rest of the file remains unchanged)` and [BackgroundManager.tsx:469](src/BackgroundManager.tsx#L469) reads `// polling loop omitted for brevity` — directly above the polling loop. Both are placeholder text that shipped. — **Fixed (A9).**
- ✅ **Agent memory is stale** — `MEMORY.md` records "Phase 2 (StorageSource) is next"; the repo is post-Phase-5. — **Fixed.**

---

## Open findings

Everything below is real and deliberately not fixed. None is load-bearing, and
none blocks a hardware test.

| # | Finding | Why it was left |
|---|---|---|
| O1 | `_event_loop` drops an event on a bare `Exception` with no dead-letter, counter, or user-visible signal | Needs a decision on what the user should see. A toast per dropped event would be worse than the silence; a counter in the panel is probably right, and that is a UI change, not a fix. |
| O2 | `CameraSource.poll` tears the source down on a *single* failed frame | A dropped frame is normal for a webcam, and the restart costs a `ffmpeg` spawn. Wants a consecutive-failure threshold — small, but it changes camera behaviour and the camera is the one source with no test hardware here. |
| O3 | `_unload` cancels `polling_task` without awaiting it | Unload can return while the loop is mid-iteration. In practice the loop is almost always parked on `queue.get`, so this has no observed symptom — but it is one `await` and should just be done. |
| O4 | `terminateSteamApp` gives up after 6×500ms with only a `console.warn` | Auto-close silently not happening is a genuinely confusing failure. Wants a toast, which means deciding what it says. |
| O5 | The post-poll sleep makes real cadence `interval + work_time` | Only visibly wrong for the camera (5s capture on a 1s interval samples every 6s). Fixing it properly means scheduling from poll *start*, which changes the timing of every source at once. |
| O6 | `simulate_tag` is an unconditional debug RPC that forges media events | No frontend caller remains. Should be gated behind a debug flag or removed — it is a working forgery path exposed in production builds. |
| O7 | `_publish_statuses()` is called twice per source event | The second call is now a no-op diff rather than a message. Deleting the duplicate is trivial; it just has not been. |
| O8 | The panel has no field to display or copy the auto-minted MQTT secret | Enabling MQTT mints a secret that currently cannot be read out of the UI to give to a publisher. Carried forward from C4. |
| O9 | The `E4` alias shim emits both old and new event names | Deliberate. Retire the aliases once a release has shipped with both. |

---

## Actionable Refactoring Roadmap

### Phase A — Stop the bleeding ✅ complete

| # | Action | State |
|---|---|---|
| A1 | Remove the hardcoded sudo password | ✅ `8a8c9fc` — `DECK_PASSWORD`, else gitignored `.vscode/settings.json`, else a prompt. History deliberately left alone: it is the template's well-known default, not a user secret. |
| A2 | Harden the mount | ✅ `ba72cf0` — `-t` against an allowlist, `ro,nosuid,nodev,noexec`, reapplied on every remount |
| A3 | Move every blocking `poll()` body into `asyncio.to_thread` | ✅ `8e5685e` |
| A4 | `deque(maxlen=100)` for the MQTT buffer; log and drop on overflow | ✅ `MAX_PENDING = 100` plus a dropped counter |
| A5 | Apply the backoff to the exception path in `_run_source` | ✅ doubles to `RECONNECT_MAX`; two regression tests pin the sequence |
| A6 | `chmod 0o600` on key files | ✅ `dbb9118` — atomic 0600 write in a 0700 directory; `signing_keys.json` no longer exists |
| A7 | Validate `appid` before it reaches `find_art` | ✅ rejected in `card_rpcs.save_card` |
| A8 | Reap `Popen` children | ✅ both go through `Plugin._spawn` — reaps, plus `start_new_session=True` |
| A9 | Delete the two placeholder comments | ✅ |
| A10 | Only our own mounts should be remounted | ✅ `_our_mounts` is consulted first; a system mountpoint is left alone |

### Phase B — Connect the tests to the environment that can run them ✅ complete

The insight: `build.sh` already solved the hard problem. Reuse it rather than inventing a second answer.

- ~~**B1.**~~ **Done** — `.vscode/test.sh` mirrors `build.sh`'s container invocation (same `--platform linux/amd64`, same `python:${DECK_PYTHON}-slim`). `pnpm test` runs it; `pnpm test:native` skips the container on a Linux x86_64 host. Pytest arguments pass through.
- ~~**B2.**~~ **Done** — `.github/workflows/test.yml` runs the suite on `ubuntu-latest` (native x86_64, no emulation) across Python 3.11 and 3.12, plus a frontend `pnpm build` job.
- ~~**B3.**~~ **Done** — the workflow's `wheels` job runs all three of `build.sh`'s guards on every PR: no macOS binaries, extensions match the target interpreter, and abi3 wheels do not require a newer Python than the Deck has.
- ~~**B4.**~~ **Done** — `setup_test_env.sh` is now a signpost to the container; `npm test` no longer points at the stale `.venv`. (The `.venv` directory itself is gitignored and local — delete it by hand if you want it gone.)
- ~~**B5.**~~ **Done** — 41 regression tests added across Phases A and B.

Two latent failures surfaced the moment the suite could run in the right place, both predating this branch: `test_file_watch_source.py` and `test_plugin.py` each had a sync test driving the loop via `asyncio.get_event_loop().run_until_complete()`, which only creates a loop when the main thread has never had one set — so they passed in isolation and failed in the full run. Both are now `async` tests.

### Phase C — Unify the trust boundary ✅ complete

- ~~**C1.**~~ **Done, `7189396`** — rules live in `settings_schema.py` as data; `set_setting`, `set_source_setting` and the on-disk loader all go through `validate()`. There were *three* copies, not two — `set_source_setting`'s type-only map was the third and the dangerous one.
- ~~**C2.**~~ **Done, `7189396`** — plus an atomic temp-file-and-rename write, so an interrupted save cannot truncate `settings.json` and reset every preference.
- ~~**C3.** Decide on signing.~~ **Done, `dbb9118`** — deleted. `KeyManager`'s at-rest storage was fixed in the same commit.
- ~~**C4.**~~ **Done, `7189396`** — all three, plus: enabling MQTT mints a secret, because the panel has no field to type one into and the toggle would otherwise silently do nothing. **Follow-up: a panel field to display and copy that secret.**
- ~~**C5.**~~ **Done, `7189396`** — `_is_local_host` now covers 127/8, bracketed IPv6, IPv4-mapped IPv6, RFC1918, link-local (incl. 169.254.169.254) and `.local`; it also fixes the port bug, where `localhost:8080` never matched because the check compared against `netloc`. The docstring states the scope: it checks the literal, not what a public name resolves to.

### Phase D — Break up the god object ⚠️ D1–D3 done, D4 open

Everything landed in a `decky_links/` package rather than the `plugin/` + `rpc/`
split sketched below. The reason is a packaging constraint, not taste: the decky
CLI zips a fixed path allowlist, so a top-level module next to `main.py` is
never copied to the device. `build.sh` vendors `decky_links/` explicitly, and
`tests/test_packaging.py` fails if an import is added without adding it there.
Two shallow packages would have meant two vendoring entries and two chances to
forget one.

| # | Action | State |
|---|---|---|
| D1 | Extract URI validation — pure function, zero dependencies, immediate test win | ✅ `decky_links/uri.py`, 103 tests, no `Plugin` involved |
| D2 | Extract settings (builds on C1) | ✅ `decky_links/settings.py` + `settings_schema.py` |
| D3 | Extract the RPC groups — mechanical, low risk | ✅ `card_rpcs.py`, `nfc_rpcs.py`, `media_registry.py` |
| D4 | Extract the state machine and router last; this is where the real coupling lives | ⬜ **Open.** ~350 lines still on `Plugin`. |

**D4 is the one item that needs your input.** The event loop and media handlers
are coupled to `self.state` and `decky.emit` rather than incidentally sharing a
`self` — moving them to a `Router` means deciding whether the router owns state
transitions or asks the plugin to make them, and whether it emits directly or
returns events for the plugin to emit. That is a design decision, and doing it
badly would give you two objects that both half-own the state machine, which is
worse than one that owns it outright.

`main.py`: 1,834 → 1,256 lines (-31%).

### Phase E — Pay down the abstraction leak ✅ complete

- ~~**E1.**~~ **Done** — `nfc_source`/`storage_source` are lookups by type; `SourceManager.replace()` added because substituting a source now goes through the registry.
- ~~**E2.**~~ **Done** — `sub_devices()` returns presence *and* enablement, so the plugin no longer recomputes the latter from the source's own settings plus an imported copy of its defaults.
- ~~**E3.**~~ **Done** — `sources.source_classes()` plus `build_all()`. Constructor extras are matched against each class's signature, so NFC keeps its key manager without every other source growing an unused parameter.
- ~~**E4.**~~ **Done** — `tag_detected` → `media_detected`, `tag_removed` → `media_removed`, `reader_status` → `source_connection`, bringing them into line with `media_loading`, which already used the current naming. Both names are emitted: the two halves of the plugin ship in one zip so a *deploy* cannot skew them, but the frontend bundle lives in the Steam UI process and outlives a `plugin_loader` restart — restarting the backend against a bundle the UI has not reloaded is the normal development loop, and without the alias that window is one where no medium is ever detected. A test asserts no site emits a legacy name directly, since the shim only works if everything goes through `main.emit`. **The aliases are still to be retired (O9).**
  `get_tag_status` went with it — orphaned since E5 removed the frontend's copy of the single-slot model, along with the 100ms cache that existed only because the frontend polled it twice a second. What deliberately keeps its NFC name: `get_reader_status` and `current_tag_*`, which describe the reader and the tag on it, and are read by the tag-write path.
- ~~**E5.**~~ **Done, `d6adef6`** — the global slot turned out to be write-only: no component ever read it. Removing it fixed the "removal clears every row" bug and a uid-normalisation bug where `uri_detected` used the source type of whatever was presented last.
- ~~**E6.**~~ **Done, `d6adef6`** — the loop cannot stop when the panel closes (game state has no backend event to push, and drives auto-close), but the RPC polls that duplicate backend pushes moved to the 5s tick. 2 RPC/s → 0.4 RPC/s. Only the local `Router.MainRunningApp` read stays at 2 Hz.

---

## What's Actually Good

Worth stating plainly, so the refactor doesn't destroy it:

- The `MediaSource` / `SourceEvent` / `MediaEvent` abstraction is the right shape and absorbed six very different triggers cleanly.
- Per-source media tracking (`_active_media`) and launch attribution (`_launch_origin`) correctly solve a genuinely subtle problem — only the medium that started a game may stop it.
- The comment density explaining *why* (the `_pending_launch_origin` ordering fix, `_reap_stale_mounts`, `_unmountable`, the `LOADING` event) is exceptional and is what made this audit fast.
- ~~`SignatureManager` fails closed rather than degrading to a forgeable HMAC — the right call, documented at [signature_manager.py:33-42](nfc/signature_manager.py#L33-L42).~~ *Deleted in `dbb9118`. The instinct was right and the code was well written; it just never verified anything on the read path, so it implied a protection the plugin did not have. That judgement — fail closed rather than degrade — carried over into `KeyManager`, which does now refuse to silently fall back to plaintext.*
- **The build toolchain is genuinely excellent.** [.vscode/build.sh](.vscode/build.sh) pins the interpreter to what Decky Loader actually carries (not SteamOS's `python3`), records how that was measured and when, cross-builds wheels in a linux/amd64 container, and then *verifies its own output* three ways — including an abi3 minimum-version check written after that exact gap shipped a broken release. That is a mature, scar-tissue-informed build. Phase B is mostly about pointing it at the test suite too.
- ~~634 tests exist and the container that can run them already exists. Connecting the two is a days-long job that changes this project's trajectory.~~ *Done, and it did. Every finding fixed since Phase B was verified in the environment the plugin actually targets, and three latent bugs surfaced that no macOS test run could have shown.*

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

---

## Top 3 Fatal Flaws / Risks

Ranked by severity × proximity.

### 1. Blocking I/O on the shared event loop — freezes the whole plugin, today

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

[plugin.json](plugin.json) declares the `root` flag. [storage_source.py:695](sources/storage_source.py#L695):

```python
["mount", "-o", "ro", devnode, tmpdir]
```

No `nosuid`, no `nodev`, no `noexec`, and **no `-t` filesystem allowlist** — so `mount` auto-probes and will happily hand a crafted disk image to any filesystem driver the kernel has (`ntfs`, `hfsplus`, `udf`, `squashfs`, …). In-kernel filesystem parsers are a well-established local-privilege-escalation surface, and this is a root process mounting media that arrives from outside the trust boundary by definition.

`floppy` is enabled by default ([main.py:143-148](main.py#L143-L148)); a USB floppy emulator is indistinguishable from a real one to udev. [`write_uri`](sources/storage_source.py#L598) also remounts `rw`.

**Fix:** `mount -t <allowlist> -o ro,nosuid,nodev,noexec`. One line, removes the class.

### 3. The trust model rests on one function, and the crypto meant to back it is unwired

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

- **Blocking-loop serialization** — see Fatal Flaw #1. This is the dominant bottleneck.
- **MQTT throughput ceiling + unbounded buffer.** `poll()` drains exactly one message per cycle at `poll_interval = 0.1` → hard **10 msg/s** ceiling ([mqtt_source.py:114-127](sources/mqtt_source.py#L114-L127)). The paho callback thread appends without limit to a `deque()` with **no `maxlen`** ([mqtt_source.py:43](sources/mqtt_source.py#L43)). A publisher exceeding 10 msg/s grows that deque without bound — unbounded memory on a device with no OOM headroom. There is no backpressure path back to the broker.
- **Duplicated push and pull for the same data.** The backend pushes `source_statuses` every ≤2s ([main.py:491](main.py#L491)) *and* the frontend polls `get_source_statuses` every 5s, `get_reader_status` + `get_tag_status` every 500ms ([BackgroundManager.tsx:471-533](src/BackgroundManager.tsx#L471-L533)). Two mechanisms, same state. The poll loop starts at plugin load and **never stops when the panel is closed** — 2 RPC round-trips per second, forever, on battery.
- **Redundant status publishes.** `_publish_statuses()` is called at the end of `_handle_source_event` ([main.py:602](main.py#L602)) *and* again at the end of the same event-loop iteration ([main.py:484](main.py#L484)).
- **Post-poll sleep inflates the effective interval.** [manager.py:209](sources/manager.py#L209) sleeps `poll_interval` *after* the poll returns, so real cadence is `interval + work_time`. With a 5s camera capture and a 1s interval, the camera actually samples every 6s.

### 2. Coupling & Cohesion

- **`main.py` is a 1,834-line god object.** One `Plugin` class holds the settings manager, state machine, event loop, URI validation, pairing, launching, audio, and ~35 RPC methods spanning NFC key management, Mifare sector locking, ECDSA signing, and printable-card rendering. Cohesion is near zero — sector locking and QR card generation share nothing but a `self`.
- **Six sources are tracked twice.** `Plugin` keeps named attributes (`self.nfc_source`, `self.storage_source`, …) *and* registers them all with `SourceManager`. `_all_sources()` ([main.py:504](main.py#L504)) exists solely to reconcile the two. Adding a seventh source requires editing **eight** places: `Plugin.__init__`, `_main`, `_all_sources`, `get_source_statuses`, `set_source_setting`'s allowlist, `SourceType` (Python), `SourceType` (TypeScript), and `sourceIcon()`. The abstraction does not yet pay for itself.
- **NFC leaks straight through the generic layer.** `current_tag_uid/uri/meta`, `get_tag_status`, `get_reader_status`, the `reader_status` event, the `tag_detected`/`tag_removed` event names, and `NFC_SETTING_KEYS` being special-cased inside `SettingsManager.get/set` ([main.py:210-226](main.py#L210-L226)). The code acknowledges this ("NFC-flavoured for historical reasons", [main.py:107](main.py#L107)) — acknowledged debt is still debt.
- **The plugin reaches into a subclass.** [main.py:1767](main.py#L1767) does `hasattr(source, "drive_kinds_present")` and [main.py:38](main.py#L38) imports `DEFAULT_DRIVE_KINDS` from `storage_source` to compute defaults it should be asking the source for. The `MediaSource` contract doesn't cover drive categories, so the caller duck-types around it.
- **Implicit shared mutable state across a thread boundary.** `get_source_settings()` ([main.py:245](main.py#L245)) returns the *live* nested dict, which is handed to each source constructor and then mutated in place by `set_source_setting` ([main.py:1818](main.py#L1818)). `MqttSource._on_message` reads `self._settings["secret"]` from paho's I/O thread while the event loop may be writing it. It works today by GIL accident, not by design.
- **The frontend holds two competing models of the same state.** `sharedState.tagUid`/`tagUri` is a single global slot that whichever source fired last clobbers — and `sharedState.activeMedia` is the per-source map added specifically to fix that. Both live in the same object and both are maintained. Related: [BackgroundManager.tsx:296](src/BackgroundManager.tsx#L296) — a `tag_removed` without `source_id` wipes **all** `activeMedia`, so one source's removal clears every other source's row.

### 3. Error Handling & Resilience

- **The backoff doesn't cover the failure path that matters.** [manager.py:200-209](sources/manager.py#L200-L209): an exception in `poll()` is logged, sets `was_connected = False`, then falls through to `await asyncio.sleep(source.poll_interval)` — **not** the exponential backoff. `reconnect_delay` only applies when `start()` returns `False`. A source raising on every poll hot-loops at its poll interval — **10 Hz for MQTT** — logging a full traceback each time, forever.
- **Silent failures reported as success.**
  - `SettingsManager.save()` logs and returns on a permissions failure ([main.py:200-202](main.py#L200-L202)); `set_setting` returns `True` to the frontend regardless ([main.py:1137](main.py#L1137)). The UI shows the toggle as saved when nothing was written.
  - `KeyManager.save()` falls back to plaintext on encryption failure and reports success ([key_manager.py:102-111](nfc/key_manager.py#L102-L111)).
  - `_event_loop` catches bare `Exception`, logs, and **drops the event** ([main.py:487](main.py#L487)). No dead-letter, no counter, no user-visible signal.
- **Zombie process accumulation.** `subprocess.Popen` with no `wait()`/reaping in `_play_sound` ([main.py:1096](main.py#L1096)) and `_launch_uri` ([main.py:1055](main.py#L1055)). One `paplay` zombie per scan, for the life of the plugin.
- **Over-eager teardown.** `CameraSource.poll` sets `self._active = False` on a *single* failed frame ([camera_source.py:110](sources/camera_source.py#L110)), forcing a full stop/start cycle. A dropped frame is normal for a webcam.
- **Unawaited cancellation.** `_unload` cancels `polling_task` but never awaits it ([main.py:433-434](main.py#L433-L434)), so unload can return while the loop is still mid-iteration.
- **Failures the user never sees.** `terminateSteamApp` retries 6×500ms then gives up with a `console.warn` ([BackgroundManager.tsx:177](src/BackgroundManager.tsx#L177)) — auto-close silently doesn't happen, with no toast.
- **Credit where due:** the per-source task isolation, the CONNECTED/DISCONNECTED lifecycle, `_reap_stale_mounts` ([storage_source.py:624](sources/storage_source.py#L624)), and the `_unmountable` set that stops 20s floppy retries are all correct, well-reasoned resilience work. Formal circuit breakers would be over-engineering at this scale — the gap is that the backoff that exists doesn't cover the exception path.

### 4. Security & Data Integrity

**Critical**

- **Hardcoded sudo password committed to a public repo.** [package.json:14-15](package.json#L14-L15) `stop` and `start` contain `echo 'ssap' | sudo -S …`, present since the initial commit `650f2b9` and tracked on a public GitHub remote. Notably `.vscode/settings.json` holds the same value in `deckpass` and **is** correctly gitignored — so the mechanism to keep this out of git already exists and `package.json` simply bypassed it. Move these scripts to a gitignored local file or read from the environment, then rotate and purge from history.
- **Root mount of untrusted filesystems** — Fatal Flaw #2.
- **MQTT is an unauthenticated remote trigger by default.** [mqtt_source.py:77-84](sources/mqtt_source.py#L77-L84): plain `mqtt.Client()`, no TLS, no broker credentials. `secret` defaults to `""` ([main.py:160](main.py#L160)), and the check is skipped entirely when empty ([mqtt_source.py:147](sources/mqtt_source.py#L147)) — so with the toggle on, **anyone who can publish to the topic can launch games and open arbitrary HTTPS URLs on the device**. The secret is stored plaintext in `settings.json` and compared with `!=`. Opt-in caps the blast radius; one toggle is not much of a gate.

**High**

- **Two divergent validation paths for the same settings.** `_validate_setting` ([main.py:911](main.py#L911)) enforces ranges and a `/dev/` prefix; `set_source_setting` ([main.py:1790](main.py#L1790)) validates **type only**. Consequences: `broker_port` accepts `-1` or `70000`; serial `port` accepts any string, bypassing the `/dev/` check applied to `device_path`; `watch_dir` accepts `/` or `/proc`, pointing a root process's directory scanner at the filesystem root; `poll_interval` accepts `0.0`.
- **`_validate_setting` is duplicated verbatim** in `SettingsManager` ([main.py:228](main.py#L228)) and `Plugin` ([main.py:911](main.py#L911)), with a comment admitting it. These will drift.
- **Path traversal via `save_game_card`.** `appid` flows unvalidated from the frontend RPC ([main.py:1708](main.py#L1708)) → `save_card` → `find_art` → `template.format(home=home, appid=appid)` ([qr.py:140](cards/qr.py#L140)). `_validate_uri` guards `uri`; nothing guards `appid`. A `../../../` value makes a root process read an arbitrary path and render it into a PNG the caller reads back. Low proximity (the frontend is the only caller and passes real app IDs) but a genuine boundary gap — `title` is properly sanitised at [qr.py:252](cards/qr.py#L252), so the pattern is understood.
- **No file permissions on secrets.** Zero `chmod`/`umask` calls repo-wide. `keys.json` and `signing_keys.json` land at default umask.

**Medium**

- **The HTTPS loopback check is porous.** [main.py:903](main.py#L903) rejects only the literal strings `localhost`, `127.0.0.1`, `::1`. Not blocked: `127.0.0.2`, `[::1]`, `0.0.0.0`, RFC1918 addresses, `169.254.169.254`, or any hostname that resolves to loopback. If the intent is SSRF protection it does not hold; if it is only "don't open odd local pages", the comment should say so.
- **`simulate_tag` is an unconditional debug RPC** ([main.py:1207](main.py#L1207)) — it forges `tag_detected`/`uri_detected` events with caller-supplied data and is exposed to the frontend in production builds.

### 5. Maintainability & Technical Debt

- **A containerized *build*, but no containerized *test*, and no CI.** [.vscode/build.sh](.vscode/build.sh) is the strongest piece of engineering in this repo: it builds `py_modules/` inside `docker --platform linux/amd64 python:3.11-slim`, then applies three guards — reject macOS binaries, reject extensions tagged for the wrong CPython, and compare abi3 wheels' *minimum* version against `DECK_PYTHON`. The correct linux/amd64 environment already exists and is already scripted. **It is simply never pointed at `pytest`.** `npm test` instead runs `.venv/bin/pytest` on the host, which for an x86_64-Linux-only plugin is the wrong environment by construction. There is **no `.github/`**, so nothing runs the 634 tests anywhere, ever, automatically. This is the single highest-leverage gap in the project: the hard part is done.
- **`.venv` is stale.** Its shebangs point at `/Users/kmturley/Sites/decky-links-comparison/decky-links-refactor/`, a path that no longer exists, so `npm test` fails with `bad interpreter`. Minor on its own — it's gitignored and host-local — but it means the one wired-up test command is dead, which is likely why the gap went unnoticed.
- **Test results on this host are not a signal.** I ran the suite on macOS arm64 and saw failures traceable to missing `cryptography` and to `py_modules/` holding a Linux-built Pillow. That is the expected and correct outcome of running an x86_64-Linux plugin's tests on Apple Silicon — **not a project defect**, and I am not reporting it as one. The `py_modules/` vendoring behind it is likewise deliberate: it is gitignored (only `.keep` is tracked), regenerated per build, and forced by the decky CLI zipping a fixed path allowlist that excludes `sources/`, `nfc/`, `cards/`, and `assets/`. [main.py:12-13](main.py#L12-L13) puts the checked-out tree ahead of it on `sys.path` specifically so the vendored copies can never shadow files under edit. All of this is documented at length in `build.sh`.
- **`main.py` at 1,834 lines / one class** is the single largest evolution hurdle. Testing `_validate_uri` requires standing up a `Plugin`.
- **Generated-text artifacts left in source:** [index.tsx:25](src/index.tsx#L25) reads `// (the rest of the file remains unchanged)` and [BackgroundManager.tsx:469](src/BackgroundManager.tsx#L469) reads `// polling loop omitted for brevity` — directly above the polling loop. Both are placeholder text that shipped.
- **Agent memory is stale** — `MEMORY.md` records "Phase 2 (StorageSource) is next"; the repo is post-Phase-5.

---

## Actionable Refactoring Roadmap

### Phase A — Stop the bleeding (days, ordered by risk retired per hour)

| # | Action | Files |
|---|---|---|
| A1 | Remove the hardcoded sudo password; rotate the Deck password; purge from git history | `package.json` |
| A2 | Harden the mount: `mount -t vfat,exfat,ext4,iso9660 -o ro,nosuid,nodev,noexec` | `sources/storage_source.py:695` |
| A3 | Move every blocking `poll()` body into `asyncio.to_thread` — camera first (5s), then NFC, then proxmark | `sources/camera_source.py`, `sources/nfc_source.py`, `nfc/proxmark_backend.py` |
| A4 | `deque(maxlen=100)` for the MQTT buffer; log and drop on overflow | `sources/mqtt_source.py:43` |
| A5 | Apply the backoff to the exception path in `_run_source`, not just to `start()` failure | `sources/manager.py:200-209` |
| A6 | `chmod 0o600` on `keys.json` and `signing_keys.json` | `nfc/key_manager.py`, `nfc/signature_manager.py` |
| A7 | Validate `appid` as `^[0-9]{1,10}$` before it reaches `find_art` | `main.py:1708`, `cards/qr.py:136` |
| A8 | Reap `Popen` children (`start_new_session=True` + a reaper, or `asyncio.create_subprocess_exec`) | `main.py:1055`, `main.py:1096` |
| A9 | Delete the two placeholder comments | `src/index.tsx:25`, `src/BackgroundManager.tsx:469` |

### Phase B — Connect the tests to the environment that can run them (days, not weeks)

The insight: `build.sh` already solved the hard problem. Reuse it rather than inventing a second answer.

- **B1.** Add `.vscode/test.sh` mirroring `build.sh`'s container invocation — same `--platform linux/amd64`, same `python:${DECK_PYTHON}-slim` image — installing `requirements.txt` + `tests/requirements.txt` and running `pytest`. Repoint `npm test` at it. This makes the suite runnable from an arm64 Mac for the first time.
- **B2.** Add `.github/workflows/test.yml` on `ubuntu-latest` (native x86_64, so no emulation cost) running the same command. Gate PRs on it. Since no developer host can validly run these tests directly, CI is not a nice-to-have here — it is the only place the suite can ever be trusted.
- **B3.** Extend the workflow to run `build.sh`'s three wheel guards on every PR, so a wrong-ABI dependency is caught before it reaches a Deck rather than after — the failure mode `build.sh`'s own comments record having cost a release.
- **B4.** Fix or delete `.venv` and `setup_test_env.sh`; both now describe a host-native workflow that this project cannot actually support.
- **B5.** Regression tests for every Phase-A fix, especially the mount flags and the settings-validation unification.

### Phase C — Unify the trust boundary (1–2 weeks)

- **C1.** Collapse the two `_validate_setting` implementations into a single `settings/schema.py` with `(type, validator, range)` per key. Route both `set_setting` and `set_source_setting` through it. Kills the divergence *and* the drift risk.
- **C2.** Make save failures propagate: `SettingsManager.save()` returns `bool`; `set_setting` returns it.
- **C3.** Decide on signing. Either verify on the read path in `NfcSource` (and encrypt keys at rest, with a real migration for the plaintext→encrypted transition), **or delete `signature_manager.py`, `signature_record.py`, and their RPCs**. Choose one; the current state is the worst of both.
- **C4.** MQTT: require a non-empty secret before `start()` succeeds, add TLS and broker-credential settings, use `hmac.compare_digest`.
- **C5.** Document what `_validate_uri`'s HTTPS branch is actually for, then make it match — either full private-range/DNS-rebinding blocking, or an honest comment that it only stops the obvious cases.

### Phase D — Break up the god object (2–4 weeks)

Target module layout, extracted from `main.py` in this order (each extraction is independently shippable and testable):

```
main.py          → Plugin: lifecycle + RPC surface only (~300 lines)
plugin/state.py  → PluginState machine + transitions
plugin/router.py → event loop, _handle_media_*, launch decisions
plugin/uri.py    → _validate_uri  (pure function, trivially testable)
settings/        → SettingsManager + unified schema (from C1)
rpc/nfc.py       → tag keys, sector locking
rpc/cards.py     → QR preview, card saving
rpc/sources.py   → statuses, per-source settings
```

- **D1.** Extract `plugin/uri.py` first — pure function, zero dependencies, immediate test win.
- **D2.** Extract settings (builds on C1).
- **D3.** Extract the RPC groups — mechanical, low risk.
- **D4.** Extract the state machine and router last; this is where the real coupling lives.

### Phase E — Pay down the abstraction leak (ongoing)

- **E1.** Delete the six named source attributes on `Plugin`; make `SourceManager` the single registry and `_all_sources()` unnecessary.
- **E2.** Promote `drive_kinds_present()` into the `MediaSource` contract (default `{}`) so `main.py` stops `hasattr`-probing and stops importing `DEFAULT_DRIVE_KINDS`.
- **E3.** Add source self-registration (a `SOURCE_REGISTRY` dict) so a new source is one file plus one entry, not eight edits.
- **E4.** Rename the NFC-flavoured events (`tag_detected` → `media_detected`, etc.) behind a compatibility shim, then retire the shim.
- **E5.** Frontend: delete `sharedState.tagUid`/`tagUri`; derive them from `activeMedia` so there is one model. Fixes the "removal clears every row" bug at [BackgroundManager.tsx:296](src/BackgroundManager.tsx#L296) by construction.
- **E6.** Have the frontend poll loop stop when the panel closes, and drop the polls that duplicate the `source_statuses` push.

---

## What's Actually Good

Worth stating plainly, so the refactor doesn't destroy it:

- The `MediaSource` / `SourceEvent` / `MediaEvent` abstraction is the right shape and absorbed six very different triggers cleanly.
- Per-source media tracking (`_active_media`) and launch attribution (`_launch_origin`) correctly solve a genuinely subtle problem — only the medium that started a game may stop it.
- The comment density explaining *why* (the `_pending_launch_origin` ordering fix, `_reap_stale_mounts`, `_unmountable`, the `LOADING` event) is exceptional and is what made this audit fast.
- `SignatureManager` fails closed rather than degrading to a forgeable HMAC — the right call, documented at [signature_manager.py:33-42](nfc/signature_manager.py#L33-L42).
- **The build toolchain is genuinely excellent.** [.vscode/build.sh](.vscode/build.sh) pins the interpreter to what Decky Loader actually carries (not SteamOS's `python3`), records how that was measured and when, cross-builds wheels in a linux/amd64 container, and then *verifies its own output* three ways — including an abi3 minimum-version check written after that exact gap shipped a broken release. That is a mature, scar-tissue-informed build. Phase B is mostly about pointing it at the test suite too.
- 634 tests exist and the container that can run them already exists. Connecting the two is a days-long job that changes this project's trajectory.

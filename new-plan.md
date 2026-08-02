# Decky Links — New Vision Implementation Plan (v2)

> [!NOTE]
> Revised based on feedback. Key decisions locked in:
> - **Shared event queue** architecture (per-source async tasks)
> - **USB webcams** for QR (no built-in Deck cameras)
> - **Breaking changes OK** — no existing users to worry about
> - **One phase at a time** — test each before proceeding
> - **Dynamic argument handover** — deferred until after refactor; will prototype multiple approaches
> - **Amiibo/Skylanders** — deferred

---

## 1. Current State Summary

| Area | What Exists |
|---|---|
| **Hardware** | NFC only — PN532 (UART), ACR122U, Proxmark, nfcpy |
| **Tags** | Mifare Classic, NTAG21x, Ultralight, ISO-14443B, ISO-15693, FeliCa, DESFire |
| **Payload** | NDEF URI records (`steam://run/*`, `steam://rungameid/*`, `https://`) |
| **State Machine** | `IDLE → READY → CARD_PRESENT → GAME_RUNNING` |
| **Launch** | `SteamClient.Apps.RunGame` / `ExecuteSteamURL` / `xdg-open` |
| **Termination** | `TerminateApp` with polling; fallback to Steam menu |
| **UI** | Decky sidebar panel + game-page pair button |
| **Tests** | ~9 Python test files |

---

## 2. Target Architecture

### Shared Event Queue

Each media source runs its own `asyncio.Task`, independently polling at its own interval. Events are pushed into a shared `asyncio.Queue` consumed by the plugin's main loop.

```
┌──────────────────────────────────────────────────────────┐
│                     Plugin (main.py)                      │
│                                                           │
│   ┌─────────────┐    ┌──────────────┐    ┌────────────┐  │
│   │ State Machine│◄───│ Event Queue  │───►│ Frontend   │  │
│   │ Launch Logic │    │ (asyncio.Q)  │    │ Events     │  │
│   │ Pairing      │    └──────┬───────┘    └────────────┘  │
│   └─────────────┘           │                             │
│                    ┌────────┴────────┐                    │
│                    │  SourceManager   │                    │
│                    └────────┬────────┘                    │
└─────────────────────────────┼────────────────────────────┘
                              │
           ┌──────────┬───────┼───────┬──────────┐
           ▼          ▼       ▼       ▼          ▼
       NfcSource  StorageSrc CamSrc MqttSrc  FileWatchSrc
       (Task 1)  (Task 2)  (Task 3) (Task 4) (Task 5)
```

### `MediaSource` Base Class (Python)

Two separate event types flow through the shared queue:

- **`SourceEvent`** — hardware/connection lifecycle (reader plugged in, webcam disconnected, MQTT broker unreachable)
- **`MediaEvent`** — media interactions (tag tapped, floppy inserted, QR scanned)

This mirrors the existing split between `reader_status` and `tag_detected`/`tag_removed` in the current NFC code, but generalized.

```python
from enum import Enum

# ── Enums ──

class SourceType(Enum):
    NFC = "nfc"
    STORAGE = "storage"
    CAMERA = "camera"
    MQTT = "mqtt"
    FILE_WATCH = "file_watch"

class SourceEventKind(Enum):
    CONNECTED = "connected"         # source hardware detected / broker reachable
    DISCONNECTED = "disconnected"   # source hardware lost / broker unreachable

class MediaEventKind(Enum):
    LOAD = "load"       # media inserted / tag tapped / QR scanned
    UNLOAD = "unload"   # media ejected / tag removed / QR left frame

# ── Events ──

@dataclass
class SourceEvent:
    """Hardware / connection lifecycle event."""
    kind: SourceEventKind
    source_type: SourceType
    source_id: str          # unique instance ID
    detail: dict            # source-specific info (device path, firmware, etc.)

@dataclass
class MediaEvent:
    """Media interaction event."""
    kind: MediaEventKind
    source_type: SourceType
    source_id: str          # unique instance ID
    media_id: str           # UID / path / content identifier
    uri: str | None         # extracted URI (validated)
    payload: dict           # source-specific extras

# Union type for the shared queue
PluginEvent = SourceEvent | MediaEvent

# ── Base Class ──

class MediaSource(ABC):
    source_type: SourceType
    
    @abstractmethod
    async def start(self) -> bool: ...
    
    @abstractmethod
    async def stop(self) -> None: ...
    
    @abstractmethod
    def is_active(self) -> bool: ...
    
    @abstractmethod
    async def poll(self) -> PluginEvent | None: ...
```

### Frontend Enums (TypeScript)

```typescript
export enum SourceType {
  NFC = "nfc",
  STORAGE = "storage",
  CAMERA = "camera",
  MQTT = "mqtt",
  FILE_WATCH = "file_watch",
}

export enum SourceEventKind {
  CONNECTED = "connected",
  DISCONNECTED = "disconnected",
}

export enum MediaEventKind {
  LOAD = "load",
  UNLOAD = "unload",
}
```

### `SourceManager`

```python
class SourceManager:
    """Orchestrates all media sources, each in its own asyncio.Task."""
    
    def __init__(self, event_queue: asyncio.Queue[PluginEvent]):
        self.sources: list[MediaSource] = []
        self.queue = event_queue
        self.tasks: list[asyncio.Task] = []
    
    def register(self, source: MediaSource): ...
    
    async def start_all(self): 
        """Start each source as an independent asyncio.Task."""
        ...
    
    async def stop_all(self): ...
    
    async def _run_source(self, source: MediaSource):
        """Poll loop for a single source; puts SourceEvents and MediaEvents on shared queue."""
        while True:
            event = await source.poll()  # returns SourceEvent | MediaEvent | None
            if event:
                await self.queue.put(event)
            await asyncio.sleep(source.poll_interval)
```

The plugin's main consumer loop dispatches based on event type:

```python
async def _event_loop(self):
    while True:
        event = await self.event_queue.get()
        if isinstance(event, SourceEvent):
            await self._handle_source_event(event)
        elif isinstance(event, MediaEvent):
            await self._handle_media_event(event)
```

---

## 3. Implementation Phases

### Phase 1: Architecture Refactor
> **Goal:** Extract NFC into the `MediaSource` pattern. Existing behavior preserved. All tests pass.

| Step | What | Files | Notes |
|---|---|---|---|
| 1.1 | Create `sources/` module with `base.py` (MediaEvent, MediaSource ABC), `manager.py` (SourceManager) | `sources/__init__.py`, `sources/base.py`, `sources/manager.py` | New module |
| 1.2 | Create `NfcSource` — extract NFC polling, NDEF read/write, tag classification from `main.py._nfc_loop` | `sources/nfc_source.py` | Wraps existing `nfc/` module. Most code is a move, not a rewrite. |
| 1.3 | Refactor `main.py` — replace `_nfc_loop` with `SourceManager` + event queue consumer loop | `main.py` | Plugin's `_main()` creates SourceManager, registers NfcSource, consumes queue |
| 1.4 | Add `source_type` to all emitted frontend events | `main.py`, `src/BackgroundManager.tsx`, `src/shared.ts` | Frontend can display which source triggered |
| 1.5 | Restructure settings — top-level settings + per-source config | `main.py`, `src/shared.ts` | `{"auto_launch": true, "auto_close": false, "sources": {"nfc": {"device_path": "...", ...}}}` |
| 1.6 | Update all Python tests | `tests/` | Must pass against refactored code |
| 1.7 | Update frontend to work with new settings shape | `src/index.tsx`, `src/shared.ts` | Settings UI still shows NFC config under "Sources" |

**Deliverable:** Plugin works identically to today, but internals use the new architecture.

---

### Phase 2: Storage Media Support (💾 Floppy / USB / Optical / MicroSD)
> **Goal:** Insert USB storage → detect → read payload → launch. Remove → pause OR close (per user setting).

| Step | What | Details |
|---|---|---|
| 2.1 | Create `StorageSource` | `sources/storage_source.py` — uses `pyudev` for Linux block device events |
| 2.2 | Device detection | Listen for USB storage attach/detach. Identify floppy (`/dev/fd*`), USB keys (`/dev/sd*`), optical (`/dev/sr*`). |
| 2.3 | Mount handling | Check if auto-mounted by SteamOS; if not, read-only mount to temp dir. Unmount on eject. |
| 2.4 | Payload file reading | Look for `decky-links.json` at filesystem root. Parse and validate URI. |
| 2.5 | Eject → pause OR close | `pyudev` removal → `MediaEvent(kind="unload")` → plugin checks `auto_close` setting: if enabled, terminate game; if disabled, open Steam menu (pause). |
| 2.6 | Register in SourceManager | Add to settings, auto-enable when storage source is configured |
| 2.7 | Frontend: multi-source status | Update status dashboard to show storage source state alongside NFC |
| 2.8 | Tests | Mocked `pyudev` events, payload parsing, mount/unmount |

#### Payload File: `decky-links.json`

```jsonc
{
  "version": 1,
  "uri": "steam://rungameid/400",
  "title": "DOOM",                         // display name
  "icon": "icon.png"                       // relative path on the media
}
```

> [!NOTE]
> For now this is a visible file. In the future we could support multiple game entries per device, or hidden files, or a more "cartridge-like" format. Keep it simple for v1 — a single JSON file with URI + optional metadata.

> [!IMPORTANT]
> **Dynamic argument handover** (passing a ROM to a launcher) is **deferred** to a later phase. We will prototype these approaches when ready:
> - `steam://run/<appid>//<arguments>/` — argument-aware Steam URI
> - SteamClient JS API VDF manipulation — modify shortcut launch args at runtime
> - Symlink swap — `/home/deck/decky-links/current_game` points to media's executable
> - File-based — write argument to a well-known file the launcher reads
>
> For now, storage media uses a direct `uri` field only (same as NFC tags).

---

### Phase 3: QR Code Support (📷 USB Webcam)
> **Goal:** USB webcam connected → detect QR codes → extract URI → launch.

| Step | What | Details |
|---|---|---|
| 3.1 | Create `CameraSource` | `sources/camera_source.py` — captures frames from USB webcam (`/dev/video*`) |
| 3.2 | QR decode library | Use lightweight `pyzbar` + `Pillow` (~2MB total) instead of OpenCV (~50MB). If `pyzbar` proves insufficient, escalate to `opencv-python-headless`. |
| 3.3 | Polling strategy | **Low-frequency continuous poll** when camera is connected: capture a frame every ~1-2 seconds, decode QR. This avoids needing a manual "Scan" button while keeping CPU usage minimal. If resource testing shows this is too heavy, fall back to on-demand scanning via a button. |
| 3.4 | Detection flow | QR decoded → extract URI string → validate against allowlist → `MediaEvent(kind="load")` → launch. QR leaving frame → `MediaEvent(kind="unload")` after debounce. |
| 3.5 | Camera lifecycle | Only activate when a `/dev/video*` device is present. Gracefully handle disconnection. |
| 3.6 | Frontend: camera status | Show camera connected state in multi-source dashboard |
| 3.7 | Tests | Mocked frame capture, QR decode, URI extraction |

> [!NOTE]
> Scoped to **QR codes only** (not barcodes). QR codes natively encode URI strings, so no lookup table is needed. Standard barcodes (UPC/EAN) are out of scope.

---

### Phase 4: Virtual Triggers (MQTT / Serial / File Watcher)
> **Goal:** Software-defined triggers for power users and home automation integration.

| Step | What | Details |
|---|---|---|
| 4.1 | `MqttSource` | Subscribe to configurable MQTT topic. Payload = JSON with `uri` field. Requires `paho-mqtt`. |
| 4.2 | `SerialSource` | Listen on serial port for line-delimited URI messages. |
| 4.3 | `FileWatchSource` | Watch a directory for `.json` files. Content follows same payload schema. |
| 4.4 | Security | **All disabled by default.** MQTT supports optional shared-secret validation. File watcher restricted to a specific directory. |
| 4.5 | Settings UI | Enable/disable toggles per virtual source, with configuration fields. |
| 4.6 | Tests | Per-source unit tests |

> [!WARNING]
> Virtual triggers introduce security surface. All are **opt-in only** and require explicit user configuration.

---

### Phase 5: UI & Artwork Polish
> **Goal:** Polished multi-source experience with artwork support.

| Step | What | Details |
|---|---|---|
| 5.1 | Multi-source dashboard | List all active sources with state, last event, and source-specific icons (NFC chip, floppy disk, camera, MQTT, etc.) |
| 5.2 | Per-source pairing | NFC: write tag. Storage: create `decky-links.json`. QR: generate printable QR code image. |
| 5.3 | Per-source behavior toggles | Auto-launch and auto-close configurable per source type |
| 5.4 | Artwork research & implementation | Investigate `SteamClient` APIs for runtime shortcut artwork changes. Implement if feasible. |
| 5.5 | Dynamic argument handover | Prototype the deferred approaches (see Phase 2 note) and implement the best one |

---

## 4. Phase Execution Order

```mermaid
graph LR
    P1["Phase 1\nArchitecture Refactor"] --> T1["🧪 Test & Validate"]
    T1 --> P2["Phase 2\nStorage Media"]
    P2 --> T2["🧪 Test & Validate"]
    T2 --> P3["Phase 3\nQR/Camera"]
    P3 --> T3["🧪 Test & Validate"]
    T3 --> P4["Phase 4\nVirtual Triggers"]
    P4 --> T4["🧪 Test & Validate"]
    T4 --> P5["Phase 5\nUI & Artwork"]
    P5 --> T5["🧪 Final Validation"]
    
    style P1 fill:#4a9eff,color:#fff
    style P2 fill:#4a9eff,color:#fff
    style P3 fill:#4a9eff,color:#fff
    style P4 fill:#4a9eff,color:#fff
    style P5 fill:#4a9eff,color:#fff
    style T1 fill:#2d8a4e,color:#fff
    style T2 fill:#2d8a4e,color:#fff
    style T3 fill:#2d8a4e,color:#fff
    style T4 fill:#2d8a4e,color:#fff
    style T5 fill:#2d8a4e,color:#fff
```

**Strictly sequential.** Each phase gets tested and validated before the next begins.

---

## 5. Dependencies

| Dependency | Phase | Size | Notes |
|---|---|---|---|
| `pyudev` | 2 | ~100KB | Linux device monitoring. Well-maintained. |
| `pyzbar` | 3 | ~2MB | QR/barcode decoding. Lightweight alternative to OpenCV. |
| `Pillow` | 3 | ~5MB | Image capture/processing for camera frames. |
| `paho-mqtt` | 4 | ~200KB | MQTT client. Mature. |
| `pyserial` | — | ✅ | Already a dependency (PN532 UART). |

> [!TIP]
> If `pyzbar` + `Pillow` proves too heavy or insufficient, alternative: `qreader` (pure Python, ~500KB) or direct `libzbar` FFI.

---

## 6. Decisions Log

| Decision | Resolution |
|---|---|
| Polling architecture | **Shared event queue** — per-source async tasks |
| Breaking changes | **Allowed** — no existing users |
| Phase ordering | **Sequential** — test each before next |
| Steam Deck cameras | **None exist** — USB webcams only |
| QR vs barcodes | **QR only** — native URI encoding |
| Camera scanning mode | **Low-frequency continuous poll** (test first, fallback to on-demand) |
| Amiibo/Skylanders | **Deferred** |
| Dynamic argument handover | **Deferred** — prototype after refactor |
| Virtual triggers default state | **Disabled** — opt-in only |
| QR dependency | **pyzbar + Pillow** (small) over OpenCV (large) |
| Storage payload file | **Visible `decky-links.json`** — simple for now, evolve later |
| Eject behavior | **Pause OR close** per user's `auto_close` setting |
| Fixed value types | **Enums** over raw strings — `MediaEventKind`, `SourceType` on both Python and TypeScript |

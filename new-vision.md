# Product Requirements Document: Decky Links

---

## 1. Product Vision
**"Physicalizing the Digital Library"** Decky Links aims to become the universal hardware-to-software bridge for SteamOS, transforming the digital gaming experience into a tactile, physical ritual. By treating diverse hardware triggers—NFC tags, floppy disks, USB keys, and QR codes—as smart "cartridges," Decky Links allows users to navigate and launch their modern library using the physical objects they love.

---

## 2. Strategic Goals
1.  **Reintroduce the Ritual:** Restore the mechanical satisfaction of inserting, tapping, or scanning media to trigger gameplay.
2.  **Hardware Agnostic:** Support a vast landscape of consumer and hobbyist hardware readers to ensure any physical object can become a "link."
3.  **On-Media Truth:** Prioritize storing launch data directly on the physical media to ensure portability across devices without relying on local databases.
4.  **Seamless System Integration:** Deliver a native-feeling experience that respects SteamOS states (artwork, playtime, and system flows) without requiring client restarts.

---

## 3. Targeted Use Cases

### 💾 The "Retro-Modern" Floppy Drive
A user connects an external USB floppy drive. Inserting a disk labeled "Doom" automatically triggers the launch of the modern Steam version (or an emulated version) of Doom. Ejecting the disk prompts the game to close or pause.

### 🃏 The "Physical Shelf" (NFC Cards)
A user creates a physical binder of custom-printed cards with NFC stickers. Tapping a card against a hidden reader on the back of the Deck instantly navigates to that game's library page to show achievements and friend activity.

### 🧸 The "Toy-to-Life" Resurrection
A user repurposes old Amiibo or Skylanders figures. Placing a "Mario" figure on a reader launches a specific platformer or an emulated classic through a pre-configured launcher.

### 🍱 The "Fixed Player, Dynamic Data" (Emulation)
A user has one "DOSBox" or "RetroArch" shortcut in Steam. Inserting different physical media (each containing a different ROM/Game) passes that specific game to the fixed launcher, allowing the external media to act as the "Software" and the local app to act as the "Console."

---

## 4. Functional Requirements

### 4.1 Media Detection & Management
* **Universal Monitoring:** The system must listen for "Load" (Insert/Tap/Scan) and "Unload" (Eject/Remove) events across all supported hardware classes.
* **Deterministic Behavior:** Every physical action must result in a predictable software response.
* **Conflict Prevention:** The system must detect if a game is already running and block secondary launches to prevent system instability.
* **Relaunch Protection:** Manually exiting a game must not trigger an immediate re-launch loop while the media is still present.

### 4.2 Content & Launching
* **Deep Link Support:** Must support native Steam `AppIDs`, Non-Steam `GameIDs` (Heroic, Lutris, Flatpak), and standard `HTTPS` URLs.
* **Dynamic Argument Handover:** The system must be able to pass a game path or ROM location from the physical media to a fixed "Launcher" app (e.g., passing a ROM on a floppy to a local DOSBox install).
* **Seamless Artwork Handling:** Even when using a single "Bridge" or "Slot" launcher for external games, the system should ideally display the correct game artwork within the SteamOS UI.

### 4.3 User Interface (Decky Menu)
* **Status Dashboard:** A clear view showing currently connected hardware, detected media, and the "Loaded" URI.
* **One-Touch Pairing:** A "Pair Current Game" button that allows users to instantly write the identity of the currently running game to a physical tag or disk.
* **Behavior Toggles:** * **Auto-Launch:** Toggle between "Instant Play" and "Navigate to Details."
    * **Auto-Close:** Toggle whether removing media terminates the associated game.

---

## 5. Hardware Standard (Target Coverage)

| Category | Supported Triggers |
| :--- | :--- |
| **NFC** | PN532 (USB/UART), ACR122U, NTAG213/215, Amiibo, Mifare, FeliCa |
| **Storage** | USB Floppy Drives, USB-C Keys, External Optical Drives (CD/DVD), MicroSD |
| **Vision** | Webcams (QR codes), Dedicated Hardware Scanners |
| **Virtual** | MQTT, Simple Serial Protocols, Local File Watchers |

---

## 6. Non-Functional Requirements
* **Performance:** Media detection and URI extraction must happen in <500ms to maintain the "physical" feel.
* **Zero-Index Dependency:** The plugin should not require a full scan of the user's library; it simply executes what the media tells it to.
* **Security:** Dynamic data passed from external media (paths/arguments) must be strictly validated to prevent unauthorized system command execution.
* **Persistence:** User settings and hardware configurations must persist across SteamOS updates.

---

## 7. Success Criteria
* Successfully triggering a game launch from at least **three** different hardware classes (NFC, Storage, QR).
* Zero required Steam restarts when swapping "cartridges" for emulated or non-Steam games.
* A "Zero-Config" setup where a user can plug in a standard reader and begin pairing immediately.

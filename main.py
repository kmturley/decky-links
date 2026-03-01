import os
import sys
import asyncio
import time
import json
import traceback
import subprocess

# Add vendored modules to path
import decky
py_modules_path = os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules")
if py_modules_path not in sys.path:
    sys.path.insert(0, py_modules_path)

import serial
from adafruit_pn532.uart import PN532_UART
import ndef

class SettingsManager:
    def __init__(self, path):
        self.path = path
        self.settings = {
            "device_path": self._get_default_device_path(),
            "baudrate": 115200,
            "polling_interval": 0.5,
            "auto_launch": True
        }
        self.load()

    def _get_default_device_path(self):
        if sys.platform == "darwin":
            return "/dev/cu.usbserial-1440"
        return "/dev/ttyUSB0"

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r") as f:
                    self.settings.update(json.load(f))
        except Exception as e:
            decky.logger.error(f"Failed to load settings: {e}")

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.settings, f, indent=4)
        except Exception as e:
            decky.logger.error(f"Failed to save settings: {e}")

    def get(self, key):
        return self.settings.get(key)

    def set(self, key, value):
        self.settings[key] = value
        self.save()

class Plugin:
    async def _main(self):
        decky.logger.info("Decky Links starting...")
        self.settings = SettingsManager(os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json"))
        self.reader = None
        self.uart = None
        self.is_pairing = False
        self.pairing_uri = None
        self.running_game_id = None
        
        # Start the NFC polling task
        self.polling_task = asyncio.create_task(self._nfc_loop())

    async def _unload(self):
        decky.logger.info("Decky Links unloading...")
        if hasattr(self, 'polling_task'):
            self.polling_task.cancel()
        if self.uart:
            self.uart.close()

    async def _nfc_loop(self):
        last_uid_hex = None
        missing_count = 0
        DEBOUNCE_THRESHOLD = 3 # Number of consecutive None reads to confirm removal
        
        while True:
            try:
                if not self.reader:
                    await self._init_reader()
                    if not self.reader:
                        await asyncio.sleep(5)
                        continue

                # Poll for tag
                uid = self.reader.read_passive_target(timeout=0.2)
                
                if uid:
                    missing_count = 0 # Reset debounce on any successful read
                    uid_hex = uid.hex().upper()
                    is_new_tag = (uid_hex != last_uid_hex)
                    
                    if is_new_tag:
                        last_uid_hex = uid_hex
                        decky.logger.info(f"Tag detected: {uid_hex} (Game Active: {self.running_game_id})")
                        await decky.emit("tag_detected", {"uid": uid_hex})
                    
                    # Logic Branches:
                    if self.is_pairing:
                        # 1. Pairing Mode: Handle immediately if any tag is current
                        decky.logger.info(f"Pairing mode active. Writing to tag {uid_hex}")
                        await self._handle_pairing(uid)
                    elif is_new_tag:
                        # 2. Tag just arrived and NOT pairing
                        await self._handle_scan(uid)
                else:
                    if last_uid_hex:
                        missing_count += 1
                        if missing_count >= DEBOUNCE_THRESHOLD:
                            # Confirm removal
                            decky.logger.info(f"Tag removed: {last_uid_hex} (after {missing_count} misses)")
                            await decky.emit("tag_removed", {})
                            
                            # SPEC 6.3: Removal during game
                            # Only trigger if NOT pairing to avoid menu opening during busy chip
                            if self.running_game_id and not self.is_pairing:
                                decky.logger.info(f"Removal detected during game {self.running_game_id}. Emitting event.")
                                await decky.emit("card_removed_during_game", {"appid": self.running_game_id})
                                
                            last_uid_hex = None
                            missing_count = 0
                
            except Exception as e:
                decky.logger.error(f"NFC loop error: {e}")
                decky.logger.error(traceback.format_exc())
                self.reader = None # Trigger re-init
                if self.uart:
                    try: self.uart.close()
                    except: pass
                    self.uart = None
            
            await asyncio.sleep(self.settings.get("polling_interval"))

    async def _init_reader(self):
        path = self.settings.get("device_path")
        baud = self.settings.get("baudrate")
        
        if not os.path.exists(path):
            return

        try:
            decky.logger.info(f"Attempting to connect to PN532 on {path} at {baud}")
            self.uart = serial.Serial(path, baudrate=baud, timeout=0.1)
            self.reader = PN532_UART(self.uart, debug=False)
            
            version = self.reader.firmware_version
            if version:
                decky.logger.info(f"Connected to PN532: {version}")
                self.reader.SAM_configuration()
                await decky.emit("reader_status", {"connected": True, "path": path})
            else:
                decky.logger.error("Failed to get PN532 firmware version")
                self.uart.close()
                self.reader = None
                self.uart = None
        except Exception as e:
            decky.logger.error(f"Init reader failed: {e}")
            self.reader = None

    async def _handle_scan(self, uid):
        # Play scan sound
        self._play_sound("scan.flac")
        
        # Read NDEF
        uri = self._read_ndef_uri()
        if uri:
            decky.logger.info(f"URI found on tag {uid.hex()}: {uri}")
            await decky.emit("uri_detected", {"uri": uri, "uid": uid.hex()})
            
            # Auto-launch logic handled via frontend event listener
            # but we keep a record in the logs
            if self.settings.get("auto_launch"):
                if self.running_game_id:
                    decky.logger.info(f"Launch Blocked: Game {self.running_game_id} already running.")
                else:
                    decky.logger.info(f"Auto-launch event emitted for: {uri}")
                    # We still call _launch_uri as a fallback or for non-steam links
                    await self._launch_uri(uri)
        else:
            decky.logger.info(f"Scan complete: No URI found on tag {uid.hex()}")
            await decky.emit("uri_detected", {"uri": "None", "uid": uid.hex()})

    def _read_ndef_uri(self):
        try:
            # For Mifare Classic, we MUST authenticate first
            uid = self.reader.read_passive_target(timeout=0.1)
            if not uid:
                return None
                
            # Try to authenticate Sector 1 (Block 4) with default keys
            authenticated = False
            keys = [b'\xFF\xFF\xFF\xFF\xFF\xFF', b'\xD3\xF7\xD3\xF7\xD3\xF7', b'\xA0\xA1\xA2\xA3\xA4\xA5']
            for key in keys:
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
                time.sleep(0.05)
            
            if not authenticated:
                decky.logger.info("Auth failed for block 4 - attempting raw read fallback")

            data = bytearray()
            # Try to read 4 blocks (4x16 = 64 bytes)
            for i in range(4, 8):
                block = self.reader.mifare_classic_read_block(i)
                if block:
                    data.extend(block)
                else:
                    break
            
            if not data:
                return None

            # NDEF messages in Type 2 tags usually start with a TLV (Type-Length-Value)
            # Type 0x03 is NDEF Message.
            if len(data) > 2 and data[0] == 0x03:
                length = data[1]
                ndef_data = data[2:2+length]
                decoder = ndef.message_decoder(ndef_data)
                for record in decoder:
                    if isinstance(record, ndef.UriRecord):
                        return record.uri
            
            # Fallback for simple URI strings
            try:
                decoded = data.decode('utf-8', errors='ignore').strip('\x00')
                if "://" in decoded:
                    # Find first occurrence of :// and try to isolate it
                    import re
                    match = re.search(r'[a-zA-Z0-9]+://[^\s\x00]+', decoded)
                    if match:
                        return match.group(0)
            except:
                pass
                
        except Exception as e:
            decky.logger.error(f"Error reading NDEF: {e}")
        return None

    async def _handle_pairing(self, uid):
        if not self.pairing_uri:
            decky.logger.warning("Pairing triggered but no URI set!")
            self.is_pairing = False
            return
            
        decky.logger.info(f"Pairing process started for tag {uid.hex()} -> {self.pairing_uri}")
        
        try:
            # Pass the UID we already have to the write function
            success, error_msg = self._write_ndef_uri(uid, self.pairing_uri)
            
            if success:
                self._play_sound("success.flac")
            else:
                self._play_sound("error.flac")
                
            await decky.emit("pairing_result", {"success": success, "uid": uid.hex(), "error": error_msg})
        except Exception as e:
            decky.logger.error(f"Critical error in pairing handler: {e}")
            await decky.emit("pairing_result", {"success": False, "uid": uid.hex(), "error": str(e)})
        finally:
            self.is_pairing = False
            self.pairing_uri = None
            decky.logger.info("Pairing mode exited.")

    def _write_ndef_uri(self, uid, uri):
        try:
            # Sector 1 (Block 4) is where we want to write our NDEF message.
            authenticated = False
            # Common Mifare default keys
            keys = [b'\xFF\xFF\xFF\xFF\xFF\xFF', b'\xD3\xF7\xD3\xF7\xD3\xF7', b'\xA0\xA1\xA2\xA3\xA4\xA5']
            
            for key in keys:
                # Try to authenticate block 4 (Sector 1)
                if self.reader.mifare_classic_authenticate_block(uid, 4, 0x60, key):
                    authenticated = True
                    break
                time.sleep(0.05)
            
            if not authenticated:
                msg = "Authentication failed: Default keys rejected."
                decky.logger.error(f"{msg} (UID: {uid.hex()})")
                return False, msg

            # Create NDEF record
            record = ndef.UriRecord(uri)
            message = b"".join(ndef.message_encoder([record]))
            
            # Wrap in NDEF TLV (Type-Length-Value)
            # [0x03] [Length] [Message] [0xFE (Terminator)]
            tlv = bytearray([0x03, len(message)]) + message + b"\xFE"
            
            # Pad to block size (16 bytes)
            while len(tlv) % 16 != 0:
                tlv.append(0x00)
            
            # Write blocks starting from block 4
            for i in range(0, len(tlv), 16):
                block_num = 4 + (i // 16)
                block_data = tlv[i:i+16]
                decky.logger.info(f"Writing NDEF block {block_num}: {block_data.hex()}")
                if not self.reader.mifare_classic_write_block(block_num, block_data):
                    msg = f"Write failed at block {block_num}"
                    decky.logger.error(msg)
                    return False, msg
            
            decky.logger.info("NDEF Write Successful")
            return True, None
        except Exception as e:
            decky.logger.error(f"Error writing NDEF: {e}")
            decky.logger.error(traceback.format_exc())
            return False, str(e)

    async def _launch_uri(self, uri):
        decky.logger.info(f"Launching URI: {uri}")
        try:
            if uri.startswith("steam://rungameid/"):
                app_id = uri.replace("steam://rungameid/", "").split("/")[0]
                decky.logger.info(f"Backend initiating Steam launch for AppID: {app_id}")
                # Using 'steam -applaunch' is a direct backend command for SteamOS
                subprocess.Popen(["steam", "-applaunch", app_id], shell=False)
            else:
                # Using xdg-open is the most reliable way to trigger protocol handlers (like steam://) in Game Mode
                subprocess.Popen(["xdg-open", uri], shell=False)
        except Exception as e:
            decky.logger.error(f"Launch failed: {e}")

    def _play_sound(self, filename):
        try:
            sound_path = os.path.join(decky.DECKY_PLUGIN_DIR, "assets", "sounds", filename)
            if os.path.exists(sound_path):
                # Use paplay for SteamOS (pulseaudio)
                subprocess.Popen(["paplay", sound_path])
        except Exception as e:
            decky.logger.error(f"Failed to play sound {filename}: {e}")

    # --- Callable methods called from JS ---

    async def get_settings(self):
        return self.settings.settings

    async def set_setting(self, key, value):
        self.settings.set(key, value)
        if key in ["device_path", "baudrate"]:
            self.reader = None # Trigger re-init
        return True

    async def start_pairing(self, uri):
        decky.logger.info(f"UI requested pairing for URI: {uri}")
        self.is_pairing = True
        self.pairing_uri = uri
        return True

    async def cancel_pairing(self):
        self.is_pairing = False
        self.pairing_uri = None
        return True

    async def get_reader_status(self):
        return {
            "connected": self.reader is not None,
            "path": self.settings.get("device_path")
        }
    
    async def set_running_game(self, appid):
        # Frontend calls this when game state changes
        self.running_game_id = appid
        decky.logger.info(f"Backend notified of running game: {appid}")
        return True

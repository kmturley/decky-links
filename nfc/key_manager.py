"""Key management module for Mifare Classic authentication keys."""

import json
import os
from typing import Dict, List, Optional

try:
    from cryptography.fernet import Fernet
    ENCRYPTION_AVAILABLE = True
except ImportError:
    ENCRYPTION_AVAILABLE = False


class KeyManager:
    """Manages custom authentication keys for Mifare Classic tags with encryption."""

    def __init__(self, path: Optional[str] = None, logger=None):
        # Format: {uid_hex: [key_a_hex, key_b_hex]}
        self.tag_keys: Dict[str, List[str]] = {}
        self.path = path
        self.logger = logger
        # Set by _init_cipher when encryption was asked for and could not be
        # set up. Distinct from "no encryption requested", which is fine.
        self._cipher_broken = False
        self._cipher = self._init_cipher()
        # True when the on-disk file was plaintext but we can now encrypt, so
        # the next save upgrades it in place.
        self._needs_reencrypt = False
        if path:
            self.load()
            if self._needs_reencrypt:
                self.save()

    def _init_cipher(self):
        """Cipher from ``DECKY_LINKS_KEY_ENCRYPTION_KEY``, or None.

        Encryption here is opt-in and deliberately not automatic. Generating a
        key ourselves would mean storing it next to the ciphertext on the same
        disk, which is obfuscation dressed as encryption — anything able to
        read one file can read both. The honest protection for these keys is
        the file mode, which :meth:`save` now always applies.

        The env var stays for users who supply a key from somewhere the device
        does not keep, where it does buy something real.
        """
        if not ENCRYPTION_AVAILABLE:
            return None

        key_env = os.environ.get("DECKY_LINKS_KEY_ENCRYPTION_KEY")
        if not key_env:
            return None

        try:
            return Fernet(key_env.encode())
        except Exception as e:
            # Do not fall through to writing plaintext. The user asked for
            # encryption; silently not doing it is the one outcome they cannot
            # detect. save() refuses to write at all in this state.
            self._cipher_broken = True
            if self.logger:
                self.logger.error(
                    f"DECKY_LINKS_KEY_ENCRYPTION_KEY is not a valid Fernet key "
                    f"({e}). Refusing to store keys unencrypted — fix the key or "
                    f"unset the variable to store them plaintext deliberately."
                )
            return None

    def load(self) -> None:
        """Load keys from file if it exists.

        Handles both storage forms in either direction. Turning encryption on
        used to lose every stored key: the file was plaintext, the decrypt
        failed, and the handler returned — silently, leaving an empty manager
        that then overwrote the file on the next save. Now a plaintext file
        found while a cipher is available is read as plaintext and flagged for
        re-encryption, so enabling the feature upgrades the file instead of
        destroying it.
        """
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except OSError as e:
            if self.logger:
                self.logger.error(f"Failed to read keys file {self.path}: {e}")
            return

        if not data:
            return

        parsed = None

        if self._cipher:
            try:
                parsed = json.loads(self._cipher.decrypt(data).decode("utf-8"))
            except Exception:
                # Not encrypted, or not with this key. Try plaintext before
                # giving up — that is the migration case.
                parsed = self._parse_plaintext(data)
                if parsed is None:
                    if self.logger:
                        self.logger.error(
                            f"Keys file {self.path} could not be decrypted with "
                            f"DECKY_LINKS_KEY_ENCRYPTION_KEY, and is not plaintext "
                            f"JSON either. Leaving it untouched — a wrong key must "
                            f"not cause the stored keys to be overwritten."
                        )
                    return
                self._needs_reencrypt = True
                if self.logger:
                    self.logger.info(
                        f"Keys file {self.path} is plaintext and encryption is "
                        f"available; re-encrypting in place."
                    )
        else:
            parsed = self._parse_plaintext(data)
            if parsed is None:
                if self.logger:
                    self.logger.error(
                        f"Keys file {self.path} is not readable as JSON. It may be "
                        f"encrypted — set DECKY_LINKS_KEY_ENCRYPTION_KEY to read it. "
                        f"Leaving it untouched."
                    )
                return

        if isinstance(parsed, dict):
            self.tag_keys = parsed

    @staticmethod
    def _parse_plaintext(data: bytes):
        """Parse ``data`` as plaintext JSON, or return None."""
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def save(self) -> None:
        """Write the keys to disk, owner-readable only.

        Two behaviours worth stating, because both were wrong before:

        The file mode is not optional. These keys unlock the user's tags, and
        the plugin runs as root, so anything it writes is world-readable at the
        default umask. 0600 on the file and 0700 on its directory is the actual
        protection here — encryption is opt-in and, when the key would live on
        the same disk, mostly theatre.

        Encryption never silently degrades. Falling back to a plaintext write
        when encrypt() failed reported success while doing the one thing the
        user had asked it not to do, and left no way to tell from the outside.

        Raises on failure so callers can report it rather than assuming a write
        that never happened.
        """
        if not self.path:
            return

        if self._cipher_broken:
            raise RuntimeError(
                "refusing to write keys: encryption was requested but the key is "
                "invalid"
            )

        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                # Not fatal — the file mode below is what actually guards the
                # contents, and the directory may be owned by someone else.
                pass

        payload = json.dumps(self.tag_keys, indent=2).encode("utf-8")
        if self._cipher:
            payload = self._cipher.encrypt(payload)

        # Write via a private temp file and rename, so a crash mid-write cannot
        # truncate the existing keys, and so the contents are never briefly
        # readable at the default umask.
        tmp_path = f"{self.path}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                os.unlink(tmp_path)
                raise
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
            self._needs_reencrypt = False
        except OSError as e:
            if self.logger:
                self.logger.error(f"Failed to write keys file {self.path}: {e}")
            raise

    def set_key(self, uid: str, key_a: str, key_b: str) -> None:
        """Store custom keys for a tag UID.
        
        Args:
            uid: Tag UID in hex format (uppercase)
            key_a: Key A in hex format (12 chars = 6 bytes)
            key_b: Key B in hex format (12 chars = 6 bytes)
        
        Raises:
            ValueError: If keys are invalid format
        """
        if not self._validate_key(key_a):
            raise ValueError(f"Invalid key_a format: {key_a}")
        if not self._validate_key(key_b):
            raise ValueError(f"Invalid key_b format: {key_b}")
        
        self.tag_keys[uid] = [key_a, key_b]
        self.save()

    def get_keys(self, uid: str) -> Optional[List[str]]:
        """Get stored keys for a tag UID.
        
        Returns:
            [key_a, key_b] if found, None otherwise
        """
        return self.tag_keys.get(uid)

    def delete_key(self, uid: str) -> None:
        """Delete stored keys for a tag UID.
        
        Raises:
            KeyError: If UID not found
        """
        del self.tag_keys[uid]
        self.save()

    def list_keys(self) -> List[str]:
        """Return list of tag UIDs with stored keys."""
        return list(self.tag_keys.keys())

    def from_dict(self, data: Dict) -> None:
        """Load keys from dictionary (for settings persistence)."""
        if isinstance(data, dict):
            self.tag_keys = data

    def to_dict(self) -> Dict:
        """Export keys as dictionary (for settings persistence)."""
        return dict(self.tag_keys)

    @staticmethod
    def _validate_key(key: str) -> bool:
        """Validate key format (12 hex chars = 6 bytes).
        
        Note: While all-zeros and all-FFs keys are weak, they are valid
        Mifare Classic keys and are used in tests. We validate format only.
        """
        if not isinstance(key, str):
            return False
        if len(key) != 12:
            return False
        try:
            bytes.fromhex(key)
            return True
        except ValueError:
            return False

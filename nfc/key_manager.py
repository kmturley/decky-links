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
        self._cipher = self._init_cipher()
        if path:
            self.load()

    def _init_cipher(self):
        """Initialize encryption cipher from environment or return None if unavailable."""
        if not ENCRYPTION_AVAILABLE:
            if self.logger:
                self.logger.warning("cryptography library not installed; keys will be stored unencrypted")
            return None
        
        key_env = os.environ.get("DECKY_LINKS_KEY_ENCRYPTION_KEY")
        if key_env:
            try:
                return Fernet(key_env.encode())
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Invalid encryption key in environment: {e}")
                return None
        
        # No key provided - warn user
        if self.logger:
            self.logger.warning(
                "DECKY_LINKS_KEY_ENCRYPTION_KEY not set; keys will be stored unencrypted. "
                "Set this environment variable to enable encryption."
            )
        return None

    def load(self) -> None:
        """Load keys from file if it exists."""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as f:
                data = f.read()
            
            # Try to decrypt if cipher is available
            if self._cipher and data:
                try:
                    decrypted = self._cipher.decrypt(data)
                    parsed = json.loads(decrypted.decode('utf-8'))
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Failed to decrypt keys file: {e}")
                    return
            else:
                # Fall back to plaintext (for backward compatibility)
                try:
                    with open(self.path, "r") as f:
                        parsed = json.load(f)
                except json.JSONDecodeError:
                    # Try reading as binary and decrypting
                    if self.logger:
                        self.logger.error(f"Keys file is corrupted and cannot be read")
                    return
            
            if isinstance(parsed, dict):
                self.tag_keys = parsed
        except IOError as e:
            if self.logger:
                self.logger.error(f"Failed to read keys file {self.path}: {e}")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Unexpected error loading keys: {e}")

    def save(self) -> None:
        """Save keys to file with encryption if available."""
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            
            json_data = json.dumps(self.tag_keys, indent=2).encode('utf-8')
            
            # Encrypt if cipher is available
            if self._cipher:
                try:
                    encrypted_data = self._cipher.encrypt(json_data)
                    with open(self.path, "wb") as f:
                        f.write(encrypted_data)
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"Failed to encrypt keys: {e}")
                    # Fall back to plaintext
                    with open(self.path, "w") as f:
                        json.dump(self.tag_keys, f, indent=2)
            else:
                # Store plaintext if encryption unavailable
                with open(self.path, "w") as f:
                    json.dump(self.tag_keys, f, indent=2)
        except IOError as e:
            if self.logger:
                self.logger.error(f"Failed to write keys file {self.path}: {e}")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Unexpected error saving keys: {e}")

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

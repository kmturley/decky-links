"""Key management module for Mifare Classic authentication keys."""

import json
import os
from typing import Dict, List, Optional


class KeyManager:
    """Manages custom authentication keys for Mifare Classic tags."""

    def __init__(self, path: Optional[str] = None):
        # Format: {uid_hex: [key_a_hex, key_b_hex]}
        self.tag_keys: Dict[str, List[str]] = {}
        self.path = path
        if path:
            self.load()

    def load(self) -> None:
        """Load keys from file if it exists."""
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.tag_keys = data
        except Exception:
            pass

    def save(self) -> None:
        """Save keys to file."""
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.tag_keys, f, indent=2)
        except Exception:
            pass

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
        """Validate key format (12 hex chars = 6 bytes)."""
        if not isinstance(key, str):
            return False
        if len(key) != 12:
            return False
        try:
            bytes.fromhex(key)
            return True
        except ValueError:
            return False

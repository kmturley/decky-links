"""NDEF signature manager for signing and verifying records.

This module provides digital signature support for NDEF records using
ECDSA (Elliptic Curve Digital Signature Algorithm) with SHA-256.
"""

import os
import json
from typing import Optional, Tuple

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.backends import default_backend
    CRYPTO_AVAILABLE = True
except Exception:
    hashes = None
    serialization = None
    ec = None
    default_backend = None
    CRYPTO_AVAILABLE = False


class SignatureManager:
    """Manages signing keys and NDEF record signatures."""

    def __init__(self, keys_path: Optional[str] = None):
        self.keys_path = keys_path
        self.signing_keys = {}
        self.crypto_available = CRYPTO_AVAILABLE
        if keys_path:
            self.load()

    def _require_crypto(self):
        if not self.crypto_available:
            raise RuntimeError(
                "cryptography is not installed; signing features are disabled"
            )

    def generate_key_pair(self, key_id: str) -> Tuple[str, str]:
        """Generate new ECDSA key pair.
        
        Args:
            key_id: Identifier for the key pair
            
        Returns:
            Tuple of (public_key_pem, private_key_pem)
        """
        self._require_crypto()
        private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        public_key = private_key.public_key()
        
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')
        
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode('utf-8')
        
        self.signing_keys[key_id] = {
            'private_key': private_pem,
            'public_key': public_pem
        }
        
        if self.keys_path:
            self.save()
        
        return public_pem, private_pem

    def import_key_pair(self, key_id: str, public_key_pem: str, private_key_pem: Optional[str] = None):
        """Import existing key pair.
        
        Args:
            key_id: Identifier for the key pair
            public_key_pem: Public key in PEM format
            private_key_pem: Optional private key in PEM format
        """
        self._require_crypto()
        # Validate keys by loading them
        serialization.load_pem_public_key(public_key_pem.encode('utf-8'), default_backend())
        if private_key_pem:
            serialization.load_pem_private_key(
                private_key_pem.encode('utf-8'),
                password=None,
                backend=default_backend()
            )
        
        self.signing_keys[key_id] = {
            'public_key': public_key_pem,
            'private_key': private_key_pem
        }
        
        if self.keys_path:
            self.save()

    def delete_key(self, key_id: str):
        """Delete a key pair.
        
        Args:
            key_id: Identifier for the key pair
        """
        if key_id in self.signing_keys:
            del self.signing_keys[key_id]
            if self.keys_path:
                self.save()

    def list_keys(self) -> list:
        """List all stored key IDs."""
        return list(self.signing_keys.keys())

    def get_public_key(self, key_id: str) -> Optional[str]:
        """Get public key PEM for a key ID."""
        if key_id in self.signing_keys:
            return self.signing_keys[key_id]['public_key']
        return None

    def sign_data(self, key_id: str, data: bytes) -> bytes:
        """Sign data using private key.
        
        Args:
            key_id: Identifier for the key pair
            data: Data to sign
            
        Returns:
            Signature bytes
            
        Raises:
            KeyError: If key_id not found
            ValueError: If private key not available
        """
        self._require_crypto()
        if key_id not in self.signing_keys:
            raise KeyError(f"Key ID {key_id} not found")
        
        private_key_pem = self.signing_keys[key_id].get('private_key')
        if not private_key_pem:
            raise ValueError(f"Private key not available for {key_id}")
        
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode('utf-8'),
            password=None,
            backend=default_backend()
        )
        
        signature = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
        return signature

    def verify_signature(self, key_id: str, data: bytes, signature: bytes) -> bool:
        """Verify signature using public key.
        
        Args:
            key_id: Identifier for the key pair
            data: Original data
            signature: Signature to verify
            
        Returns:
            True if signature is valid, False otherwise
        """
        self._require_crypto()
        if key_id not in self.signing_keys:
            return False
        
        public_key_pem = self.signing_keys[key_id]['public_key']
        public_key = serialization.load_pem_public_key(
            public_key_pem.encode('utf-8'),
            default_backend()
        )
        
        try:
            public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
            return True
        except Exception:
            return False

    def load(self):
        """Load keys from file."""
        if not self.keys_path or not os.path.exists(self.keys_path):
            return
        
        try:
            with open(self.keys_path, 'r') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.signing_keys = data
        except Exception:
            pass

    def save(self):
        """Save keys to file."""
        if not self.keys_path:
            return
        
        try:
            os.makedirs(os.path.dirname(self.keys_path), exist_ok=True)
            with open(self.keys_path, 'w') as f:
                json.dump(self.signing_keys, f, indent=2)
        except Exception:
            pass

    def to_dict(self) -> dict:
        """Export keys as dict."""
        return dict(self.signing_keys)

    @classmethod
    def from_dict(cls, data: dict, keys_path: Optional[str] = None):
        """Create instance from dict."""
        manager = cls(keys_path)
        if isinstance(data, dict):
            manager.signing_keys = data
        return manager

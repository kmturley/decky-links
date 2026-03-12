"""NDEF signature record handler.

This module provides utilities for creating and parsing NDEF signature records
according to the NFC Forum Signature RTD specification.
"""

import struct
from typing import Optional, Tuple


class SignatureRecord:
    """NDEF Signature Record (NFC Forum Signature RTD)."""

    def __init__(self, signature: bytes, key_id: str, algorithm: str = "ECDSA-SHA256"):
        self.signature = signature
        self.key_id = key_id
        self.algorithm = algorithm

    def to_ndef_payload(self) -> bytes:
        """Convert to NDEF record payload.
        
        Format:
        - 1 byte: version (0x01)
        - 1 byte: algorithm ID (0x02 for ECDSA-SHA256)
        - 2 bytes: key ID length
        - N bytes: key ID (UTF-8)
        - 2 bytes: signature length
        - M bytes: signature
        """
        key_id_bytes = self.key_id.encode('utf-8')
        
        # Algorithm mapping
        algo_map = {
            "ECDSA-SHA256": 0x02,
            "RSA-SHA256": 0x01,
        }
        algo_id = algo_map.get(self.algorithm, 0x02)
        
        payload = bytearray()
        payload.append(0x01)  # Version
        payload.append(algo_id)  # Algorithm
        payload.extend(struct.pack('>H', len(key_id_bytes)))  # Key ID length
        payload.extend(key_id_bytes)  # Key ID
        payload.extend(struct.pack('>H', len(self.signature)))  # Signature length
        payload.extend(self.signature)  # Signature
        
        return bytes(payload)

    @classmethod
    def from_ndef_payload(cls, payload: bytes) -> Optional['SignatureRecord']:
        """Parse NDEF signature record payload.
        
        Args:
            payload: Raw NDEF payload bytes
            
        Returns:
            SignatureRecord instance or None if invalid
        """
        if len(payload) < 6:
            return None
        
        try:
            version = payload[0]
            if version != 0x01:
                return None
            
            algo_id = payload[1]
            algo_map = {0x01: "RSA-SHA256", 0x02: "ECDSA-SHA256"}
            algorithm = algo_map.get(algo_id, "ECDSA-SHA256")
            
            key_id_len = struct.unpack('>H', payload[2:4])[0]
            key_id = payload[4:4+key_id_len].decode('utf-8')
            
            sig_len = struct.unpack('>H', payload[4+key_id_len:6+key_id_len])[0]
            signature = payload[6+key_id_len:6+key_id_len+sig_len]
            
            return cls(signature, key_id, algorithm)
        except Exception:
            return None

    def to_ndef_record(self) -> bytes:
        """Create complete NDEF record with signature payload.
        
        Returns NDEF record bytes with TNF=0x02 (MIME type) and
        type="application/vnd.nfc.signature".
        """
        payload = self.to_ndef_payload()
        record_type = b"application/vnd.nfc.signature"
        
        # NDEF record header
        # MB=1, ME=1, CF=0, SR=1, IL=0, TNF=0x02
        header = 0xD2  # 11010010
        
        record = bytearray()
        record.append(header)
        record.append(len(record_type))
        record.append(len(payload))
        record.extend(record_type)
        record.extend(payload)
        
        return bytes(record)


def create_signed_ndef_message(uri_record: bytes, signature_record: bytes) -> bytes:
    """Create NDEF message with URI and signature records.
    
    Args:
        uri_record: URI record bytes
        signature_record: Signature record bytes
        
    Returns:
        Complete NDEF message with both records
    """
    # Modify URI record header to clear ME bit
    uri_modified = bytearray(uri_record)
    uri_modified[0] = uri_modified[0] & 0xBF  # Clear ME bit
    
    # Modify signature record header to set ME bit and clear MB bit
    sig_modified = bytearray(signature_record)
    sig_modified[0] = (sig_modified[0] & 0xBF) | 0x40  # Clear MB, set ME
    
    return bytes(uri_modified) + bytes(sig_modified)


def extract_uri_from_signed_message(message: bytes) -> Tuple[Optional[bytes], Optional[bytes]]:
    """Extract URI and signature records from signed NDEF message.
    
    Args:
        message: Complete NDEF message bytes
        
    Returns:
        Tuple of (uri_record, signature_record) or (None, None) if invalid
    """
    if len(message) < 3:
        return None, None
    
    try:
        # Parse first record (URI)
        header1 = message[0]
        type_len1 = message[1]
        payload_len1 = message[2]
        
        record1_len = 3 + type_len1 + payload_len1
        uri_record = message[:record1_len]
        
        # Parse second record (signature)
        if len(message) > record1_len:
            sig_record = message[record1_len:]
            return uri_record, sig_record
        
        return uri_record, None
    except Exception:
        return None, None

"""Tests for signature manager and NDEF signature records."""

import pytest
import os
import tempfile
from nfc.signature_manager import SignatureManager
from nfc.signature_record import SignatureRecord, create_signed_ndef_message, extract_uri_from_signed_message


class TestSignatureManager:
    """Tests for SignatureManager."""

    def test_generate_key_pair(self):
        """Should generate new ECDSA key pair."""
        manager = SignatureManager()
        
        public_key, private_key = manager.generate_key_pair("test_key")
        
        assert public_key.startswith("-----BEGIN PUBLIC KEY-----")
        assert private_key.startswith("-----BEGIN PRIVATE KEY-----")
        assert "test_key" in manager.list_keys()

    def test_import_key_pair(self):
        """Should import existing key pair."""
        manager = SignatureManager()
        
        # Generate a key to get valid PEM format
        public_key, private_key = manager.generate_key_pair("temp")
        
        # Import as new key
        manager.import_key_pair("imported", public_key, private_key)
        
        assert "imported" in manager.list_keys()
        assert manager.get_public_key("imported") == public_key

    def test_import_public_key_only(self):
        """Should import public key without private key."""
        manager = SignatureManager()
        
        public_key, _ = manager.generate_key_pair("temp")
        manager.import_key_pair("public_only", public_key, None)
        
        assert "public_only" in manager.list_keys()
        assert manager.get_public_key("public_only") == public_key

    def test_delete_key(self):
        """Should delete key pair."""
        manager = SignatureManager()
        
        manager.generate_key_pair("to_delete")
        assert "to_delete" in manager.list_keys()
        
        manager.delete_key("to_delete")
        assert "to_delete" not in manager.list_keys()

    def test_sign_data(self):
        """Should sign data with private key."""
        manager = SignatureManager()
        
        manager.generate_key_pair("signer")
        data = b"test data to sign"
        
        signature = manager.sign_data("signer", data)
        
        assert isinstance(signature, bytes)
        assert len(signature) > 0

    def test_sign_data_missing_key(self):
        """Should raise KeyError for missing key."""
        manager = SignatureManager()
        
        with pytest.raises(KeyError):
            manager.sign_data("nonexistent", b"data")

    def test_sign_data_no_private_key(self):
        """Should raise ValueError when private key not available."""
        manager = SignatureManager()
        
        public_key, _ = manager.generate_key_pair("temp")
        manager.import_key_pair("public_only", public_key, None)
        
        with pytest.raises(ValueError):
            manager.sign_data("public_only", b"data")

    def test_verify_signature_valid(self):
        """Should verify valid signature."""
        manager = SignatureManager()
        
        manager.generate_key_pair("verifier")
        data = b"test data"
        signature = manager.sign_data("verifier", data)
        
        valid = manager.verify_signature("verifier", data, signature)
        
        assert valid is True

    def test_verify_signature_invalid(self):
        """Should reject invalid signature."""
        manager = SignatureManager()
        
        manager.generate_key_pair("verifier")
        data = b"test data"
        wrong_signature = b"invalid signature bytes"
        
        valid = manager.verify_signature("verifier", data, wrong_signature)
        
        assert valid is False

    def test_verify_signature_tampered_data(self):
        """Should reject signature when data is tampered."""
        manager = SignatureManager()
        
        manager.generate_key_pair("verifier")
        data = b"original data"
        signature = manager.sign_data("verifier", data)
        
        tampered_data = b"tampered data"
        valid = manager.verify_signature("verifier", tampered_data, signature)
        
        assert valid is False

    def test_verify_signature_missing_key(self):
        """Should return False for missing key."""
        manager = SignatureManager()
        
        valid = manager.verify_signature("nonexistent", b"data", b"sig")
        
        assert valid is False

    def test_persistence(self):
        """Should persist keys to file."""
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
            path = f.name
        
        try:
            manager1 = SignatureManager(path)
            manager1.generate_key_pair("persistent")
            
            manager2 = SignatureManager(path)
            
            assert "persistent" in manager2.list_keys()
        finally:
            os.unlink(path)

    def test_to_dict(self):
        """Should export keys as dict."""
        manager = SignatureManager()
        manager.generate_key_pair("export_test")
        
        data = manager.to_dict()
        
        assert isinstance(data, dict)
        assert "export_test" in data

    def test_from_dict(self):
        """Should create instance from dict."""
        manager1 = SignatureManager()
        manager1.generate_key_pair("import_test")
        data = manager1.to_dict()
        
        manager2 = SignatureManager.from_dict(data)
        
        assert "import_test" in manager2.list_keys()


class TestSignatureRecord:
    """Tests for SignatureRecord."""

    def test_create_signature_record(self):
        """Should create signature record."""
        signature = b"test_signature_bytes"
        key_id = "test_key"
        
        record = SignatureRecord(signature, key_id)
        
        assert record.signature == signature
        assert record.key_id == key_id
        assert record.algorithm == "ECDSA-SHA256"

    def test_to_ndef_payload(self):
        """Should convert to NDEF payload."""
        signature = b"sig"
        key_id = "key1"
        
        record = SignatureRecord(signature, key_id)
        payload = record.to_ndef_payload()
        
        assert payload[0] == 0x01  # Version
        assert payload[1] == 0x02  # ECDSA-SHA256
        assert len(payload) > 6

    def test_from_ndef_payload(self):
        """Should parse NDEF payload."""
        signature = b"test_sig"
        key_id = "key1"
        
        record1 = SignatureRecord(signature, key_id)
        payload = record1.to_ndef_payload()
        
        record2 = SignatureRecord.from_ndef_payload(payload)
        
        assert record2 is not None
        assert record2.signature == signature
        assert record2.key_id == key_id
        assert record2.algorithm == "ECDSA-SHA256"

    def test_from_ndef_payload_invalid(self):
        """Should return None for invalid payload."""
        invalid_payload = b"\x00\x00"
        
        record = SignatureRecord.from_ndef_payload(invalid_payload)
        
        assert record is None

    def test_to_ndef_record(self):
        """Should create complete NDEF record."""
        signature = b"sig"
        key_id = "key1"
        
        record = SignatureRecord(signature, key_id)
        ndef_record = record.to_ndef_record()
        
        assert ndef_record[0] == 0xD2  # Header
        assert len(ndef_record) > 3

    def test_roundtrip(self):
        """Should survive roundtrip conversion."""
        signature = b"original_signature"
        key_id = "original_key"
        
        record1 = SignatureRecord(signature, key_id)
        payload = record1.to_ndef_payload()
        record2 = SignatureRecord.from_ndef_payload(payload)
        
        assert record2.signature == record1.signature
        assert record2.key_id == record1.key_id
        assert record2.algorithm == record1.algorithm


class TestSignedNDEFMessage:
    """Tests for signed NDEF message creation and parsing."""

    def test_create_signed_message(self):
        """Should create signed NDEF message."""
        uri_record = b"\xD1\x01\x0C\x55\x03example.com"
        sig_record = b"\xD2\x1E\x10application/vnd.nfc.signature\x01\x02\x00\x04key1\x00\x03sig"
        
        message = create_signed_ndef_message(uri_record, sig_record)
        
        assert len(message) > len(uri_record) + len(sig_record) - 2
        assert message[0] & 0x40 == 0  # URI record ME bit cleared

    def test_extract_uri_from_signed_message(self):
        """Should extract URI and signature records."""
        uri_record = b"\xD1\x01\x0C\x55\x03example.com"
        sig_record = b"\xD2\x1E\x10application/vnd.nfc.signature\x01\x02\x00\x04key1\x00\x03sig"
        message = create_signed_ndef_message(uri_record, sig_record)
        
        extracted_uri, extracted_sig = extract_uri_from_signed_message(message)
        
        assert extracted_uri is not None
        assert extracted_sig is not None

    def test_extract_from_invalid_message(self):
        """Should return None for invalid message."""
        invalid_message = b"\x00\x00"
        
        uri, sig = extract_uri_from_signed_message(invalid_message)
        
        assert uri is None
        assert sig is None


class TestSignatureIntegration:
    """Integration tests for full signing workflow."""

    def test_full_signing_workflow(self):
        """Should sign and verify URI end-to-end."""
        manager = SignatureManager()
        manager.generate_key_pair("integration_test")
        
        # Create URI data
        uri_data = b"steam://run/12345"
        
        # Sign
        signature = manager.sign_data("integration_test", uri_data)
        
        # Create signature record
        sig_record = SignatureRecord(signature, "integration_test")
        
        # Verify
        valid = manager.verify_signature("integration_test", uri_data, sig_record.signature)
        
        assert valid is True

    def test_signature_with_different_keys(self):
        """Should reject signature from different key."""
        manager = SignatureManager()
        manager.generate_key_pair("key1")
        manager.generate_key_pair("key2")
        
        data = b"test data"
        signature = manager.sign_data("key1", data)
        
        # Try to verify with different key
        valid = manager.verify_signature("key2", data, signature)
        
        assert valid is False

    def test_signature_record_roundtrip(self):
        """Should survive full NDEF record roundtrip."""
        manager = SignatureManager()
        manager.generate_key_pair("roundtrip")
        
        data = b"test data"
        signature = manager.sign_data("roundtrip", data)
        
        # Create and serialize record
        record1 = SignatureRecord(signature, "roundtrip")
        payload = record1.to_ndef_payload()
        
        # Deserialize and verify
        record2 = SignatureRecord.from_ndef_payload(payload)
        valid = manager.verify_signature("roundtrip", data, record2.signature)
        
        assert valid is True

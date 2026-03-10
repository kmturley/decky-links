"""Tests for key management module."""

import json
import os
import tempfile
import pytest
from nfc.key_manager import KeyManager


class TestKeyManager:
    """Tests for KeyManager class."""

    def test_set_and_get_key(self):
        """Should store and retrieve keys for a tag."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        key_a = "FFFFFFFFFFFF"
        key_b = "D3F7D3F7D3F7"
        
        km.set_key(uid, key_a, key_b)
        
        assert km.get_keys(uid) == [key_a, key_b]

    def test_set_key_invalid_key_a(self):
        """Should reject invalid key A."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        
        # Too short
        with pytest.raises(ValueError):
            km.set_key(uid, "FFFF", "FFFFFFFFFFFF")
        
        # Invalid hex
        with pytest.raises(ValueError):
            km.set_key(uid, "GGGGGGGGGGGG", "FFFFFFFFFFFF")

    def test_set_key_invalid_key_b(self):
        """Should reject invalid key B."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        
        with pytest.raises(ValueError):
            km.set_key(uid, "FFFFFFFFFFFF", "INVALID")

    def test_get_nonexistent_key(self):
        """Should return None for nonexistent UID."""
        km = KeyManager()
        
        result = km.get_keys("NONEXISTENT")
        
        assert result is None

    def test_delete_key(self):
        """Should delete stored keys."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        km.set_key(uid, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        
        km.delete_key(uid)
        
        assert km.get_keys(uid) is None

    def test_delete_nonexistent_key(self):
        """Should raise KeyError when deleting nonexistent key."""
        km = KeyManager()
        
        with pytest.raises(KeyError):
            km.delete_key("NONEXISTENT")

    def test_list_keys(self):
        """Should return list of UIDs with stored keys."""
        km = KeyManager()
        uid1 = "DEADBEEFCAFE"
        uid2 = "CAFEBEEFDEAD"
        
        km.set_key(uid1, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        km.set_key(uid2, "A0A1A2A3A4A5", "FFFFFFFFFFFF")
        
        keys = km.list_keys()
        
        assert len(keys) == 2
        assert uid1 in keys
        assert uid2 in keys

    def test_persistence_to_dict(self):
        """Should export keys as dictionary."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        km.set_key(uid, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        
        data = km.to_dict()
        
        assert isinstance(data, dict)
        assert data[uid] == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]

    def test_persistence_from_dict(self):
        """Should load keys from dictionary."""
        km = KeyManager()
        data = {
            "DEADBEEFCAFE": ["FFFFFFFFFFFF", "D3F7D3F7D3F7"],
            "CAFEBEEFDEAD": ["A0A1A2A3A4A5", "FFFFFFFFFFFF"],
        }
        
        km.from_dict(data)
        
        assert km.get_keys("DEADBEEFCAFE") == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]
        assert km.get_keys("CAFEBEEFDEAD") == ["A0A1A2A3A4A5", "FFFFFFFFFFFF"]

    def test_validate_key_correct_format(self):
        """Should validate correct key format."""
        assert KeyManager._validate_key("FFFFFFFFFFFF") is True
        assert KeyManager._validate_key("D3F7D3F7D3F7") is True
        assert KeyManager._validate_key("A0A1A2A3A4A5") is True

    def test_validate_key_incorrect_length(self):
        """Should reject keys with incorrect length."""
        assert KeyManager._validate_key("FFFF") is False
        assert KeyManager._validate_key("FFFFFFFFFFFFFFFFFF") is False

    def test_validate_key_invalid_hex(self):
        """Should reject invalid hex characters."""
        assert KeyManager._validate_key("GGGGGGGGGGGG") is False
        assert KeyManager._validate_key("ZZZZZZZZZZZZ") is False

    def test_validate_key_not_string(self):
        """Should reject non-string keys."""
        assert KeyManager._validate_key(123456) is False
        assert KeyManager._validate_key(None) is False
        assert KeyManager._validate_key([]) is False

    def test_multiple_keys_per_uid(self):
        """Should allow updating keys for same UID."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        
        km.set_key(uid, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        assert km.get_keys(uid) == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]
        
        km.set_key(uid, "A0A1A2A3A4A5", "FFFFFFFFFFFF")
        assert km.get_keys(uid) == ["A0A1A2A3A4A5", "FFFFFFFFFFFF"]

    def test_empty_key_manager(self):
        """Should handle empty key manager."""
        km = KeyManager()
        
        assert km.list_keys() == []
        assert km.to_dict() == {}

    def test_file_persistence_save(self):
        """Should save keys to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "keys.json")
            km = KeyManager(path)
            uid = "DEADBEEFCAFE"
            
            km.set_key(uid, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
            
            assert os.path.exists(path)
            with open(path) as f:
                data = json.load(f)
                assert data[uid] == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]

    def test_file_persistence_load(self):
        """Should load keys from file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "keys.json")
            
            # Create file with initial data
            data = {"DEADBEEFCAFE": ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]}
            with open(path, "w") as f:
                json.dump(data, f)
            
            # Load in new instance
            km = KeyManager(path)
            
            assert km.get_keys("DEADBEEFCAFE") == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]

    def test_file_persistence_delete(self):
        """Should persist deletions to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "keys.json")
            km = KeyManager(path)
            uid = "DEADBEEFCAFE"
            
            km.set_key(uid, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
            km.delete_key(uid)
            
            # Verify file was updated
            with open(path) as f:
                data = json.load(f)
                assert uid not in data

    def test_no_path_no_persistence(self):
        """Should work without file path (in-memory only)."""
        km = KeyManager()
        uid = "DEADBEEFCAFE"
        
        km.set_key(uid, "FFFFFFFFFFFF", "D3F7D3F7D3F7")
        
        assert km.get_keys(uid) == ["FFFFFFFFFFFF", "D3F7D3F7D3F7"]

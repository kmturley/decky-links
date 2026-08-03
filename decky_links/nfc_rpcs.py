"""Mifare Classic key and sector RPCs.

Storing per-tag authentication keys and locking sectors are NFC-specific
operations that happened to live on Plugin because every RPC did. They need a
key manager, a reader and a logger — not the state machine, the media
registry, the settings or the other five sources.

Plain functions taking what they need. ``decky`` is passed in because it
exists only inside the plugin loader's process; ``nfc_source`` may be None
when no reader is configured, which every function here handles.
"""

from typing import Optional


def _classify(nfc_source, uid_bytes):
    """Tag metadata, or {} when there is no reader to ask.

    Guarded because both callers below went straight to _classify_tag and
    would raise AttributeError with no reader configured — an RPC error in the
    panel rather than the empty result the rest of the function expects.
    """
    if nfc_source is None:
        return {}
    return nfc_source._classify_tag(uid_bytes)


async def set_tag_key(decky, key_manager, uid: str, key_a: str, key_b: str):
    """Store custom Mifare Classic authentication keys for a tag UID.

    Args:
        uid: Tag UID as hex string (e.g. "04A1B2C3D4E5F6")
        key_a: Key A as 12-char hex string (6 bytes)
        key_b: Key B as 12-char hex string (6 bytes)

    Returns:
        True if keys were stored successfully, False otherwise.
    """
    # Validate UID format
    if not isinstance(uid, str) or not uid:
        decky.logger.warning("Invalid UID: must be non-empty string")
        return False
    
    try:
        bytes.fromhex(uid)  # Validate hex format
    except ValueError:
        decky.logger.warning(f"Invalid UID format (not hex): {uid}")
        return False
    
    try:
        key_manager.set_key(uid.upper(), key_a, key_b)
        decky.logger.info(f"Stored custom keys for tag {uid.upper()}")
        return True
    except ValueError as e:
        decky.logger.warning(f"Invalid key format: {e}")
        return False
    except Exception as e:
        decky.logger.error(f"Failed to store keys: {e}")
        return False

async def get_tag_key(decky, key_manager, uid: str):
    """Retrieve stored Mifare Classic authentication keys for a tag UID.

    Args:
        uid: Tag UID as hex string

    Returns:
        Dict with 'key_a' and 'key_b' if found, empty dict otherwise.
    """
    try:
        keys = key_manager.get_keys(uid)
        if keys:
            return {"key_a": keys[0], "key_b": keys[1]}
        return {}
    except Exception as e:
        decky.logger.error(f"Failed to retrieve keys: {e}")
        return {}

async def list_tag_keys(decky, key_manager):
    """List all stored tag UIDs with custom keys.

    Returns:
        List of tag UIDs that have custom keys stored.
    """
    try:
        return key_manager.list_keys()
    except Exception as e:
        decky.logger.error(f"Failed to list keys: {e}")
        return []

async def get_sector_info(decky, key_manager, nfc_source, uid: Optional[str] = None,
                          current_uid: Optional[str] = None):
    """Get sector lock status for current or specified tag.
    
    Args:
        uid: Optional tag UID hex string. If None, uses current tag.
        
    Returns:
        List of sector info dicts, or empty list on error.
    """
    try:
        # Use current tag if no UID specified
        if uid:
            uid_bytes = bytes.fromhex(uid)
        elif current_uid:
            uid_bytes = bytes.fromhex(current_uid)
        else:
            decky.logger.warning("No tag present for sector info")
            return []
        
        # Get tag metadata to determine type
        meta = _classify(nfc_source, uid_bytes)
        if meta.get("type") != "mifare-classic":
            decky.logger.warning(f"Sector info only supported for Mifare Classic, got {meta.get('type')}")
            return []

        # Create handler and get sector info
        from nfc.tag_handlers import MifareClassicHandler
        handler = MifareClassicHandler(uid_bytes, key_manager)

        reader = nfc_source.reader if nfc_source else None
        if not reader:
            decky.logger.error("No reader available for sector info")
            return []

        return handler.get_sector_info(reader)
    except Exception as e:
        decky.logger.error(f"Failed to get sector info: {e}")
        return []

async def lock_sector(decky, key_manager, nfc_source, uid: str, sector: int, key_a: str, key_b: str):
    """Lock a sector on a Mifare Classic tag.
    
    Args:
        uid: Tag UID hex string
        sector: Sector number (0-15 for 1K, 0-39 for 4K)
        key_a: Key A hex string (12 chars = 6 bytes)
        key_b: Key B hex string (12 chars = 6 bytes)
        
    Returns:
        True if successful, False otherwise.
    """
    try:
        # Validate inputs
        if not uid or not isinstance(uid, str):
            decky.logger.warning("Invalid UID for sector lock")
            return False
        
        if len(key_a) != 12 or len(key_b) != 12:
            decky.logger.warning("Keys must be 12 hex characters")
            return False
        
        # Convert hex strings to bytes
        try:
            uid_bytes = bytes.fromhex(uid)
            key_a_bytes = bytes.fromhex(key_a)
            key_b_bytes = bytes.fromhex(key_b)
        except ValueError as e:
            decky.logger.warning(f"Invalid hex format: {e}")
            return False
        
        # Verify tag type and get capacity
        meta = _classify(nfc_source, uid_bytes)
        if meta.get("type") != "mifare-classic":
            decky.logger.warning(f"Sector locking only supported for Mifare Classic")
            return False

        capacity = meta.get("capacity_bytes", 0)
        max_sectors = 40 if capacity > 2048 else 16

        if sector < 0 or sector >= max_sectors:
            decky.logger.warning(f"Invalid sector {sector} for {capacity}-byte tag (max {max_sectors - 1})")
            return False

        reader = nfc_source.reader if nfc_source else None
        if not reader:
            decky.logger.error("No reader available for sector lock")
            return False

        # Create handler and lock sector
        from nfc.tag_handlers import MifareClassicHandler
        handler = MifareClassicHandler(uid_bytes, key_manager)

        success, error = handler.lock_sector(reader, sector, key_a_bytes, key_b_bytes)
        
        if not success:
            decky.logger.error(f"Failed to lock sector {sector}: {error}")
        else:
            decky.logger.info(f"Successfully locked sector {sector} on tag {uid}")
        
        return success
    except Exception as e:
        decky.logger.error(f"Failed to lock sector: {e}")
        return False

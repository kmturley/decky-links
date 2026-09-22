"""Tests for NFC tag detection: transport, identification and the poll loop.

Grouped by the failure each behaviour was introduced to prevent, because the
point of most of these is not that the code does something but that it stops
doing something it used to.
"""

import time
import pytest
from unittest.mock import MagicMock

from nfc_core import ndef_codec, tag_identity
from nfc_core.reader import (
    PN532UARTReader,
    ReaderProtocolError,
    ReaderTransportError,
    TargetInfo,
    classify_exception,
)
from nfc_core.tag_handlers import MifareClassicHandler
from sources.base import MediaEventKind
from sources.nfc_source import NfcSource


# ── Fakes ───────────────────────────────────────────────────────────────


class FakePN532:
    """A PN532 driver stand-in that records the commands it is given."""

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.raise_on = {}
        self.firmware_version = (0x32, 1, 6, 7)

    def call_function(self, command, response_length=0, params=b"", timeout=1):
        self.calls.append((command, list(params), timeout))
        if command in self.raise_on:
            raise self.raise_on[command]
        value = self.responses.get(command)
        if callable(value):
            return value(list(params))
        return value


class FakeUart:
    def __init__(self):
        self.written = []
        self.input_reset = 0
        self.output_reset = 0

    def write(self, data):
        self.written.append(bytes(data))

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.input_reset += 1

    def reset_output_buffer(self):
        self.output_reset += 1

    def close(self):
        pass


def make_reader(driver=None, uart=None):
    """A PN532UARTReader wired to fakes, bypassing the serial open."""
    reader = PN532UARTReader("/dev/null", 115200, logger=MagicMock())
    reader._reader = driver if driver is not None else FakePN532()
    reader.uart = uart if uart is not None else FakeUart()
    return reader


def autopoll_response(uid, sak=0x00, atqa=0x0044, target_type=0x10):
    """Build an InAutoPoll response carrying one ISO 14443-3A target."""
    data = [0x01, (atqa >> 8) & 0xFF, atqa & 0xFF, sak, len(uid)] + list(uid)
    return [0x01, target_type, len(data)] + data


def make_source(reader=None, **settings):
    base = {"device_path": "/dev/ttyUSB0", "polling_interval": 0.5}
    base.update(settings)
    source = NfcSource(base, logger=MagicMock())
    source._reader = reader
    return source


# ── Transport: the PN532 no longer gets left mid-command ─────────────────


class TestPassiveActivationRetries:
    """The chip's MaxRetries default is 0xFF: retry activation forever.

    That is what left InListPassiveTarget running after the host had given up
    waiting, so the next command collided with a response to a command the
    host had already forgotten — the root cause of the reader appearing to
    drop in and out.
    """

    def test_connect_bounds_passive_activation(self):
        driver = FakePN532()
        reader = make_reader(driver)
        reader._configure_retries()

        rf_calls = [c for c in driver.calls if c[0] == 0x32]
        assert rf_calls, "RFConfiguration was never sent"
        cfg_item, retry_atr, retry_psl, retry_activation = rf_calls[0][1]
        assert cfg_item == 0x05, "CfgItem 0x05 is MaxRetries"
        assert retry_activation != 0xFF, "0xFF means retry forever — the bug"
        assert retry_activation <= 0x03

    def test_a_failure_to_configure_does_not_prevent_use(self):
        """Less reliable is better than refusing to connect at all."""
        driver = FakePN532()
        driver.raise_on[0x32] = RuntimeError("clone firmware")
        reader = make_reader(driver)
        reader._configure_retries()
        assert reader.is_connected()


class TestRecovery:
    """A desync is recovered from, not escalated into a disconnect."""

    def test_recover_sends_an_ack_frame_and_drains(self):
        uart = FakeUart()
        reader = make_reader(uart=uart)

        reader.recover()

        assert uart.written == [b"\x00\x00\xff\x00\xff\x00"], (
            "the host ACK frame is what aborts a command in flight"
        )
        assert uart.input_reset == 1
        assert uart.output_reset == 1

    def test_recover_without_a_port_is_a_no_op(self):
        reader = make_reader()
        reader.uart = None
        reader.recover()   # must not raise

    def test_transport_errors_are_separated_from_protocol_errors(self):
        assert isinstance(classify_exception(OSError("unplugged")), ReaderTransportError)
        # serial.SerialException subclasses OSError, so it lands here too.
        assert isinstance(
            classify_exception(RuntimeError("Did not receive expected ACK from PN532!")),
            ReaderProtocolError,
        )
        assert isinstance(
            classify_exception(RuntimeError("Response checksum did not match")),
            ReaderProtocolError,
        )
        # The driver subscripts a None response after a timeout.
        assert isinstance(
            classify_exception(TypeError("'NoneType' object is not subscriptable")),
            ReaderProtocolError,
        )


class TestDetection:
    """InAutoPoll: bounded, self-terminating, and multi-protocol."""

    def test_detects_a_type_a_tag_with_its_activation_data(self):
        driver = FakePN532()
        uid = b"\x04\x11\x22\x33\x44\x55\x66"
        driver.responses[0x60] = autopoll_response(uid, sak=0x00, atqa=0x0044)
        reader = make_reader(driver)

        target = reader.read_target(timeout=0.2)

        assert target.uid == uid
        assert target.sak == 0x00
        assert target.atqa == 0x0044
        assert target.protocol == tag_identity.PROTO_106A

    def test_polls_for_type_b_felica_and_jewel_as_well_as_type_a(self):
        driver = FakePN532()
        driver.responses[0x60] = [0x00]
        reader = make_reader(driver)

        reader.read_target(timeout=0.2)

        _, params, _ = driver.calls[0]
        requested = set(params[2:])
        assert 0x00 in requested, "ISO 14443-3A"
        assert 0x03 in requested, "ISO 14443-3B"
        assert 0x11 in requested and 0x12 in requested, "FeliCa 212/424"
        assert 0x04 in requested, "Innovision Jewel"

    def test_autopoll_is_bounded(self):
        """PollNr and Period must both be finite or the chip hangs again."""
        driver = FakePN532()
        driver.responses[0x60] = [0x00]
        reader = make_reader(driver)

        reader.read_target(timeout=0.2)

        _, params, _ = driver.calls[0]
        poll_nr, period = params[0], params[1]
        assert poll_nr != 0xFF, "0xFF is endless polling"
        assert 1 <= period <= 0x0F

    def test_no_tag_reports_absence_cleanly(self):
        driver = FakePN532()
        driver.responses[0x60] = [0x00]
        reader = make_reader(driver)
        assert reader.read_target(timeout=0.2) is None

    def test_a_missing_response_is_an_error_not_an_absent_tag(self):
        """Reporting absence wrongly is what closes the user's game."""
        driver = FakePN532()
        driver.responses[0x60] = None
        reader = make_reader(driver)

        with pytest.raises(ReaderProtocolError):
            reader.read_target(timeout=0.2)

    def test_a_desync_triggers_recovery(self):
        driver = FakePN532()
        driver.responses[0x60] = None
        uart = FakeUart()
        reader = make_reader(driver, uart)

        with pytest.raises(ReaderProtocolError):
            reader.read_target(timeout=0.2)

        assert uart.written, "recover() should have aborted the command"

    def test_an_implausible_uid_length_is_rejected(self):
        """A corrupt frame read as identity used to become a brand new tag."""
        driver = FakePN532()
        driver.responses[0x60] = [0x01, 0x10, 6, 0x01, 0x00, 0x44, 0x00, 99, 0x04]
        reader = make_reader(driver)

        with pytest.raises(ReaderProtocolError):
            reader.read_target(timeout=0.2)

    def test_type_b_target_yields_its_pupi(self):
        driver = FakePN532()
        atqb = [0x50, 0xDE, 0xAD, 0xBE, 0xEF] + [0x00] * 7
        data = [0x01] + atqb + [0x00]
        driver.responses[0x60] = [0x01, 0x03, len(data)] + data
        reader = make_reader(driver)

        target = reader.read_target(timeout=0.2)

        assert target.uid == b"\xDE\xAD\xBE\xEF"
        assert target.protocol == tag_identity.PROTO_106B

    def test_felica_target_yields_its_idm(self):
        driver = FakePN532()
        idm = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]
        data = [0x01, 0x12, 0x01] + idm + [0x00] * 8
        driver.responses[0x60] = [0x01, 0x11, len(data)] + data
        reader = make_reader(driver)

        target = reader.read_target(timeout=0.2)

        assert target.uid == bytes(idm)
        assert target.protocol == tag_identity.PROTO_212F

    def test_falls_back_when_the_driver_has_no_low_level_api(self):
        """A stubbed or third-party driver still works, just without extras."""
        driver = MagicMock(spec=["read_passive_target"])
        driver.read_passive_target.return_value = b"\x01\x02\x03\x04"
        reader = make_reader(driver)

        target = reader.read_target(timeout=0.2)

        assert target.uid == b"\x01\x02\x03\x04"
        assert target.sak is None
        assert reader.supports_target_info is False

    def test_iso14443b_probe_uses_the_right_parameter_name(self):
        """It used to pass baud_rate= to a method whose parameter is card_baud,
        so every call raised TypeError into a bare except and Type B never
        worked at all."""
        driver = FakePN532()
        atqb = [0x50, 0x11, 0x22, 0x33, 0x44] + [0x00] * 7
        data = [0x01] + atqb + [0x00]
        driver.responses[0x4A] = [0x01] + data
        reader = make_reader(driver)

        assert reader.read_uid_iso14443b(timeout=0.1) == b"\x11\x22\x33\x44"
        assert driver.calls[0][1][1] == 0x03, "baud rate 3 is 106 kbps Type B"


class TestGetVersion:
    def test_returns_the_eight_version_bytes(self):
        driver = FakePN532()
        version = [0x00, 0x04, 0x04, 0x02, 0x01, 0x00, 0x11, 0x03]
        driver.responses[0x40] = [0x00] + version
        reader = make_reader(driver)

        assert reader.get_version() == bytes(version)

    def test_a_nak_returns_none_rather_than_raising(self):
        driver = FakePN532()
        driver.responses[0x40] = [0x01]
        reader = make_reader(driver)

        assert reader.get_version() is None


# ── Identification from ATQA / SAK ───────────────────────────────────────


class TestSakClassification:
    @pytest.mark.parametrize("sak,expected", [
        (0x00, tag_identity.TYPE_ULTRALIGHT),
        (0x08, tag_identity.TYPE_CLASSIC),
        (0x09, tag_identity.TYPE_CLASSIC_MINI),
        (0x18, tag_identity.TYPE_CLASSIC),
        (0x10, tag_identity.TYPE_PLUS),
        (0x20, tag_identity.TYPE_ISO14443_4),
        (0x28, tag_identity.TYPE_CLASSIC),
        (0x38, tag_identity.TYPE_CLASSIC),
    ])
    def test_known_saks(self, sak, expected):
        assert tag_identity.classify_sak(sak)["type"] == expected

    def test_desfire_needs_the_atqa_too(self):
        assert tag_identity.classify_sak(0x20)["type"] == tag_identity.TYPE_ISO14443_4
        assert tag_identity.classify_sak(0x20, 0x0344)["type"] == tag_identity.TYPE_DESFIRE

    def test_unknown_sak_falls_back_to_the_iso_defined_bits(self):
        """Parts that did not exist when the table was written still classify."""
        result = tag_identity.classify_sak(0x60)   # ISO 14443-4 bit set
        assert result["type"] == tag_identity.TYPE_ISO14443_4

    def test_cascade_bit_means_the_uid_is_incomplete(self):
        assert tag_identity.classify_sak(0x04)["type"] == tag_identity.TYPE_UNKNOWN

    def test_only_storage_families_are_ndef_capable(self):
        assert tag_identity.TYPE_NTAG in tag_identity.NDEF_CAPABLE
        assert tag_identity.TYPE_CLASSIC in tag_identity.NDEF_CAPABLE
        assert tag_identity.TYPE_DESFIRE not in tag_identity.NDEF_CAPABLE
        assert tag_identity.TYPE_FELICA not in tag_identity.NDEF_CAPABLE


class TestVersionParsing:
    @pytest.mark.parametrize("storage,model,size", [
        (0x0F, "NTAG213", 144),
        (0x11, "NTAG215", 504),
        (0x13, "NTAG216", 888),
    ])
    def test_ntag_models(self, storage, model, size):
        version = bytes([0x00, 0x04, 0x04, 0x02, 0x01, 0x00, storage, 0x03])
        parsed = tag_identity.parse_version(version)
        assert parsed["type"] == tag_identity.TYPE_NTAG
        assert parsed["label"] == model
        assert parsed["user_bytes"] == size
        assert parsed["user_pages"] == size // 4

    def test_ultralight_ev1(self):
        version = bytes([0x00, 0x04, 0x03, 0x01, 0x01, 0x00, 0x0B, 0x03])
        parsed = tag_identity.parse_version(version)
        assert parsed["type"] == tag_identity.TYPE_ULTRALIGHT
        assert parsed["user_bytes"] == 48

    def test_truncated_or_foreign_versions_return_none(self):
        assert tag_identity.parse_version(None) is None
        assert tag_identity.parse_version(b"\x00\x04") is None
        # Not an NXP part.
        assert tag_identity.parse_version(bytes([0, 0x99, 4, 2, 1, 0, 0x11, 3])) is None


class TestRandomUid:
    def test_a_uid_beginning_08_is_random(self):
        assert tag_identity.is_random_uid(b"\x08\x11\x22\x33") is True

    def test_a_normal_uid_is_not(self):
        assert tag_identity.is_random_uid(b"\x04\x11\x22\x33") is False
        assert tag_identity.is_random_uid(b"\x08\x11\x22\x33\x44\x55\x66") is False


# ── NDEF TLV framing ─────────────────────────────────────────────────────


class TestTlvParsing:
    def test_a_plain_message(self):
        data = b"\x03\x06\xD1\x01\x02\x55\x00\x65\xFE"
        assert ndef_codec.find_ndef_message(data) == b"\xD1\x01\x02\x55\x00\x65"

    def test_a_lock_control_tlv_before_the_message_is_skipped(self):
        """Normal on NTAG216 and on anything formatted by a tool that
        describes its lock bytes. The old parser assumed the message began at
        byte 0 and gave up on these tags entirely."""
        data = b"\x01\x03\xA0\x10\x44" + b"\x03\x06\xD1\x01\x02\x55\x00\x65" + b"\xFE"
        assert ndef_codec.find_ndef_message(data) == b"\xD1\x01\x02\x55\x00\x65"

    def test_a_memory_control_tlv_is_skipped(self):
        data = b"\x02\x03\x01\x02\x03" + b"\x03\x04\xD1\x01\x00\x55" + b"\xFE"
        assert ndef_codec.find_ndef_message(data) == b"\xD1\x01\x00\x55"

    def test_null_tlvs_are_skipped(self):
        data = b"\x00\x00\x00" + b"\x03\x04\xD1\x01\x00\x55" + b"\xFE"
        assert ndef_codec.find_ndef_message(data) == b"\xD1\x01\x00\x55"

    def test_the_three_byte_length_form(self):
        """Messages over 254 bytes. The old encoder never emitted this and the
        old parser never read it, so a long URI was written with a truncated
        length byte and read back as garbage."""
        message = b"\xD1\x01\xFF\x55" + b"x" * 300
        data = b"\x03\xFF" + len(message).to_bytes(2, "big") + message + b"\xFE"
        assert ndef_codec.find_ndef_message(data) == message

    def test_a_terminator_byte_inside_the_payload_does_not_end_the_read(self):
        """0xFE is a legal byte in UTF-8 payload. Scanning for it truncated
        any URI that happened to contain one."""
        message = b"\xD1\x01\x05\x55\x00\xFE\xFE\xFE"
        data = b"\x03" + bytes([len(message)]) + message + b"\xFE"
        assert ndef_codec.find_ndef_message(data) == message
        assert ndef_codec.is_terminated(data) is True

    def test_is_terminated_is_false_until_the_run_is_complete(self):
        message = b"\xD1\x01\x02\x55\x00\x65"
        full = b"\x03" + bytes([len(message)]) + message + b"\xFE"
        assert ndef_codec.is_terminated(full[:4]) is False
        assert ndef_codec.is_terminated(full) is True

    def test_truncated_data_does_not_raise(self):
        assert ndef_codec.find_ndef_message(b"\x03") is None
        assert ndef_codec.find_ndef_message(b"") is None
        assert ndef_codec.is_terminated(b"\x03\x40\x01") is False


class TestTlvEncoding:
    def test_short_messages_use_one_length_byte(self):
        tlv = ndef_codec.encode_tlv(b"\x01\x02\x03")
        assert tlv == bytearray(b"\x03\x03\x01\x02\x03\xFE")

    def test_long_messages_use_the_three_byte_form(self):
        message = b"x" * 300
        tlv = ndef_codec.encode_tlv(message)
        assert tlv[0] == 0x03
        assert tlv[1] == 0xFF
        assert int.from_bytes(tlv[2:4], "big") == 300
        assert tlv[-1] == 0xFE

    def test_round_trip(self):
        for length in (1, 10, 254, 255, 300, 800):
            message = bytes(range(256)) * 4
            message = message[:length]
            assert ndef_codec.find_ndef_message(ndef_codec.encode_tlv(message)) == message


class TestUriDecoding:
    def test_the_prefix_byte_is_expanded(self):
        """The old fallback ignored it, so a tag written with the standard
        abbreviation came back with its scheme eaten."""
        assert ndef_codec.decode_uri_payload(b"\x04example.com") == "https://example.com"
        assert ndef_codec.decode_uri_payload(b"\x03example.com") == "http://example.com"
        assert ndef_codec.decode_uri_payload(b"\x00steam://rungameid/400") == "steam://rungameid/400"

    def test_fallback_finds_a_uri_record_in_raw_bytes(self):
        data = b"\x00\x00\xD1\x01\x0C\x55\x00steam://x\x00"
        assert ndef_codec.extract_uri_fallback(data) == "steam://x"

    def test_fallback_finds_a_bare_uri(self):
        data = b"\xFF\xFF\xFFhttps://example.com/a\x00\x00"
        assert ndef_codec.extract_uri_fallback(data) == "https://example.com/a"

    def test_size_estimation_accounts_for_prefix_compression(self):
        # "https://" is abbreviated to the single prefix byte 0x04, so the
        # record is flags + type length + payload length + type (4 bytes) plus
        # a payload of the prefix byte and the one remaining character.
        assert ndef_codec.uri_record_size("https://e") == 6
        # Plus the TLV tag, its length byte and the terminator.
        assert ndef_codec.uri_storage_size("https://e") == 9
        # An unabbreviated scheme stores every character.
        assert ndef_codec.uri_record_size("steam://x") == 4 + 1 + len("steam://x")

    def test_size_estimation_switches_to_the_long_form(self):
        short = ndef_codec.uri_storage_size("https://" + "a" * 200)
        long = ndef_codec.uri_storage_size("https://" + "a" * 300)
        assert short < long
        assert ndef_codec.uri_record_size("https://" + "a" * 300) == 1 + 1 + 4 + 1 + 301


# ── MIFARE Classic authentication ────────────────────────────────────────


class TestClassicAuthentication:
    """Reads used to issue no authentication at all, so a real Classic tag
    always looked blank; and the key loop never re-selected the tag, so only
    the first key in the list could ever succeed."""

    def test_read_authenticates_before_reading(self):
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD", blocks=[4, 5, 6])
        reader = MagicMock(spec=[
            "mifare_classic_authenticate_block", "mifare_classic_read_block",
            "select_target",
        ])
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\x00" * 16

        handler.read_ndef(reader)

        reader.mifare_classic_authenticate_block.assert_called()

    def test_read_reauthenticates_at_each_sector_boundary(self):
        """Authentication covers one sector. Without renewal the read stopped
        at block 6."""
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD", blocks=[4, 5, 6, 8, 9, 10])
        reader = MagicMock(spec=[
            "mifare_classic_authenticate_block", "mifare_classic_read_block",
            "select_target",
        ])
        reader.mifare_classic_authenticate_block.return_value = True
        reader.mifare_classic_read_block.return_value = b"\x00" * 16

        handler.read_ndef(reader)

        authed_blocks = [
            c.args[1] for c in reader.mifare_classic_authenticate_block.call_args_list
        ]
        assert 4 in authed_blocks, "sector 1"
        assert 8 in authed_blocks, "sector 2"

    def test_the_tag_is_reselected_between_key_attempts(self):
        """A failed authentication deselects the tag, so the next key was
        being tried against a card that could not answer. NDEF-formatted
        Classic tags use the *second* key in the list, which is why they could
        never be read."""
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD")
        reader = MagicMock(spec=[
            "mifare_classic_authenticate_block", "select_target",
        ])
        ndef_key = b"\xD3\xF7\xD3\xF7\xD3\xF7"
        reader.mifare_classic_authenticate_block.side_effect = (
            lambda uid, block, key_type, key: key == ndef_key
        )

        assert handler.authenticate_sector(reader, 1) is True
        reader.select_target.assert_called()

    def test_an_exception_does_not_abandon_the_remaining_keys(self):
        """The old loop broke out of the key list on the first exception, so a
        single timeout meant only one key was ever tried."""
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD")
        reader = MagicMock(spec=[
            "mifare_classic_authenticate_block", "select_target",
        ])
        attempts = []

        def auth(uid, block, key_type, key):
            attempts.append(key)
            if len(attempts) == 1:
                raise TypeError("'NoneType' object is not subscriptable")
            return key == b"\xA0\xA1\xA2\xA3\xA4\xA5"

        reader.mifare_classic_authenticate_block.side_effect = auth

        assert handler.authenticate_sector(reader, 1) is True
        assert len(attempts) >= 3

    def test_custom_keys_from_the_key_manager_are_tried_first(self):
        """They were stored on disk and then never used by the read or write
        path, which used a hardcoded list instead."""
        key_manager = MagicMock()
        key_manager.get_keys.return_value = ["A1A2A3A4A5A6", "B1B2B3B4B5B6"]
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD", key_manager=key_manager)

        keys = handler._get_keys_to_try()

        assert keys[0] == bytes.fromhex("A1A2A3A4A5A6")
        assert keys[1] == bytes.fromhex("B1B2B3B4B5B6")
        assert b"\xFF\xFF\xFF\xFF\xFF\xFF" in keys, "defaults still tried after"

    def test_a_malformed_stored_key_is_skipped_not_fatal(self):
        key_manager = MagicMock()
        key_manager.get_keys.return_value = ["not-hex", "A1A2A3A4A5A6"]
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD", key_manager=key_manager)

        keys = handler._get_keys_to_try()

        assert bytes.fromhex("A1A2A3A4A5A6") in keys

    def test_write_refuses_a_sector_it_cannot_authenticate(self):
        handler = MifareClassicHandler(b"\xAA\xBB\xCC\xDD", blocks=[4, 5, 6])
        reader = MagicMock(spec=[
            "mifare_classic_authenticate_block", "mifare_classic_write_block",
            "select_target",
        ])
        reader.mifare_classic_authenticate_block.return_value = False

        ok, err = handler.write_ndef(reader, b"\x00" * 16)

        assert ok is False
        assert "authentication failed" in err.lower()
        reader.mifare_classic_write_block.assert_not_called()

    def test_sector_of_handles_the_4k_layout(self):
        assert MifareClassicHandler.sector_of(4) == 1
        assert MifareClassicHandler.sector_of(62) == 15
        assert MifareClassicHandler.sector_of(128) == 32
        assert MifareClassicHandler.sector_of(144) == 33


# ── Poll loop ────────────────────────────────────────────────────────────


class TestPollArrivalAndRemoval:
    def test_a_new_tag_produces_a_load_event(self):
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid"])
        reader.supports_target_info = True
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader)

        event = source._poll_locked()

        assert event.kind == MediaEventKind.LOAD
        assert event.media_id == "01020304"

    def test_the_same_tag_does_not_re_fire(self):
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid"])
        reader.supports_target_info = True
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader)

        source._poll_locked()
        assert source._poll_locked() is None

    def test_removal_needs_both_repeated_misses_and_elapsed_time(self):
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid"])
        reader.supports_target_info = True
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader, polling_interval=0.1, removal_grace_seconds=0.2)
        source._poll_locked()

        reader.read_target.return_value = None
        assert source._poll_locked() is None, "one miss is never enough"
        assert source._poll_locked() is None, "grace period has not elapsed"

        source._absent_since = time.monotonic() - 1.0
        event = source._poll_locked()

        assert event.kind == MediaEventKind.UNLOAD
        assert event.media_id == "01020304"

    def test_removal_grace_is_never_shorter_than_two_poll_intervals(self):
        """A single dropped poll must not be able to quit the user's game,
        however the interval is configured."""
        source = make_source(None, polling_interval=2.0, removal_grace_seconds=0.2)
        assert source.removal_grace == 4.0

    def test_a_sighting_cancels_a_removal_in_progress(self):
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid"])
        reader.supports_target_info = True
        target = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        reader.read_target.return_value = target
        source = make_source(reader, polling_interval=0.1, removal_grace_seconds=0.2)
        source._poll_locked()

        reader.read_target.return_value = None
        source._poll_locked()
        assert source._absent_since is not None

        reader.read_target.return_value = target
        source._poll_locked()
        assert source._absent_since is None
        assert source._missing_count == 0


class TestProtocolErrorsAreNotRemovals:
    """Three bad frames in a row used to look exactly like a lifted tag, and
    a lifted tag quits the running game."""

    def _reader_that_errors(self):
        reader = MagicMock(spec=[
            "read_target", "supports_target_info", "read_uid", "recover", "close",
        ])
        reader.supports_target_info = True
        return reader

    def test_a_protocol_error_produces_no_event(self):
        reader = self._reader_that_errors()
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader)
        source._poll_locked()

        reader.read_target.side_effect = ReaderProtocolError("checksum")
        assert source._poll_locked() is None

    def test_a_protocol_error_does_not_advance_the_removal_clock(self):
        reader = self._reader_that_errors()
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader, polling_interval=0.1, removal_grace_seconds=0.1)
        source._poll_locked()

        reader.read_target.side_effect = ReaderProtocolError("checksum")
        for _ in range(4):
            assert source._poll_locked() is None

        assert source._missing_count == 0
        assert source._absent_since is None
        assert source._last_uid_hex == "01020304", "the tag is still considered present"

    def test_a_protocol_error_triggers_recovery_not_a_teardown(self):
        reader = self._reader_that_errors()
        reader.read_target.side_effect = ReaderProtocolError("checksum")
        source = make_source(reader)

        source._poll_locked()

        reader.recover.assert_called_once()
        reader.close.assert_not_called()
        assert source._reader is reader

    def test_repeated_protocol_errors_eventually_reconnect(self):
        reader = self._reader_that_errors()
        reader.read_target.side_effect = ReaderProtocolError("checksum")
        source = make_source(reader)

        for _ in range(NfcSource.MAX_PROTOCOL_ERRORS):
            source._poll_locked()

        assert source._reader is None, "a persistently broken reader is dropped"
        reader.close.assert_called_once()

    def test_a_clean_poll_resets_the_error_count(self):
        reader = self._reader_that_errors()
        reader.read_target.side_effect = ReaderProtocolError("checksum")
        source = make_source(reader)
        source._poll_locked()
        source._poll_locked()
        assert source._protocol_errors == 2

        reader.read_target.side_effect = None
        reader.read_target.return_value = None
        source._poll_locked()
        assert source._protocol_errors == 0

    def test_a_transport_error_drops_the_reader_immediately(self):
        reader = self._reader_that_errors()
        reader.read_target.side_effect = ReaderTransportError("unplugged")
        source = make_source(reader)

        source._poll_locked()

        assert source._reader is None
        reader.close.assert_called_once()


class TestTagSwap:
    def test_swapping_tags_reports_the_outgoing_one_as_removed(self):
        """Without this the registry keeps attributing a running game to a tag
        that is no longer on the reader, and lifting the new one does
        nothing."""
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid"])
        reader.supports_target_info = True
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader)
        source._poll_locked()

        reader.read_target.return_value = TargetInfo(uid=b"\xAA\xBB\xCC\xDD", sak=0x08)
        events = source._poll_locked()

        assert isinstance(events, list)
        assert [e.kind for e in events] == [MediaEventKind.UNLOAD, MediaEventKind.LOAD]
        assert events[0].media_id == "01020304"
        assert events[1].media_id == "AABBCCDD"


class TestUnreadableVersusBlank:
    def _reader(self):
        reader = MagicMock(spec=[
            "read_target", "supports_target_info", "read_uid", "recover",
            "select_target", "get_version", "ntag2xx_read_block",
        ])
        reader.supports_target_info = True
        reader.get_version.return_value = None
        return reader

    def test_an_unreadable_tag_is_not_reported_as_blank(self):
        """Both produce no URI, but only one of them is the user's problem to
        fix, and a tag we failed to read must never be offered for pairing as
        though it were empty."""
        reader = self._reader()
        target = TargetInfo(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00)
        reader.read_target.return_value = target
        reader.select_target.return_value = target
        reader.ntag2xx_read_block.return_value = None   # nothing readable
        source = make_source(reader)

        event = source._poll_locked()

        assert event.uri is None
        assert event.payload.get("unreadable") is True

    def test_a_genuinely_blank_tag_is_not_reported_unreadable(self):
        reader = self._reader()
        target = TargetInfo(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00)
        reader.read_target.return_value = target
        reader.select_target.return_value = target
        # A valid capability container, then an empty NDEF message.
        reader.ntag2xx_read_block.side_effect = (
            lambda page: bytes([0xE1, 0x10, 0x12, 0x00]) if page == 3
            else b"\x03\x00\xFE\x00"
        )
        source = make_source(reader)

        event = source._poll_locked()

        assert event.uri is None
        assert not event.payload.get("unreadable")

    def test_a_non_storage_tag_says_why_it_cannot_be_used(self):
        reader = self._reader()
        reader.read_target.return_value = TargetInfo(
            uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x20, atqa=0x0344
        )
        source = make_source(reader)

        event = source._poll_locked()

        assert event.payload["unreadable"] is True
        assert "DESFire" in event.payload["error"]

    def test_the_content_read_is_retried_before_giving_up(self):
        """One dropped frame used to be cached permanently as "blank tag"."""
        reader = self._reader()
        target = TargetInfo(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00)
        reader.read_target.return_value = target
        reader.select_target.return_value = target
        reader.ntag2xx_read_block.return_value = None
        source = make_source(reader)

        source._poll_locked()

        assert reader.select_target.call_count >= NfcSource.CONTENT_READ_ATTEMPTS

    def test_a_random_uid_tag_is_flagged_as_unpairable(self):
        reader = self._reader()
        reader.read_target.return_value = TargetInfo(uid=b"\x08\x11\x22\x33", sak=0x20)
        source = make_source(reader)

        event = source._poll_locked()

        assert event.payload["unstable_id"] is True
        assert "random" in event.payload["error"].lower()


class TestTargetRelease:
    def test_the_tag_is_released_after_its_content_is_read(self):
        """A target left selected stays in the ISO 14443-3 ACTIVE state and
        does not answer the next anticollision, so a card resting on the
        reader goes intermittently invisible."""
        reader = MagicMock(spec=[
            "read_target", "supports_target_info", "read_uid", "select_target",
            "get_version", "ntag2xx_read_block", "release_target",
        ])
        reader.supports_target_info = True
        reader.get_version.return_value = None
        target = TargetInfo(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00)
        reader.read_target.return_value = target
        reader.select_target.return_value = target
        reader.ntag2xx_read_block.return_value = b"\x03\x00\xFE\x00"
        source = make_source(reader)

        source._poll_locked()

        reader.release_target.assert_called()

    def test_a_backend_without_release_is_fine(self):
        reader = MagicMock(spec=[
            "read_target", "supports_target_info", "read_uid", "select_target",
            "get_version", "ntag2xx_read_block",
        ])
        reader.supports_target_info = True
        reader.get_version.return_value = None
        target = TargetInfo(uid=b"\x04\x11\x22\x33\x44\x55\x66", sak=0x00)
        reader.read_target.return_value = target
        reader.select_target.return_value = target
        reader.ntag2xx_read_block.return_value = b"\x03\x00\xFE\x00"
        source = make_source(reader)

        assert source._poll_locked().kind == MediaEventKind.LOAD


class TestManagerFansOutMultipleEvents:
    @pytest.mark.asyncio
    async def test_a_poll_returning_two_events_queues_both(self):
        import asyncio
        from sources.manager import SourceManager
        from sources.base import SourceType

        queue = asyncio.Queue()
        manager = SourceManager(queue, logger=MagicMock())

        source = MagicMock()
        source.source_type = SourceType.NFC
        source.source_id = "nfc:test"
        source.is_enabled.return_value = True
        source.is_active.return_value = True
        source.poll_interval = 10.0

        unload = MagicMock()
        load = MagicMock()

        async def poll_once():
            source.poll = _never
            return [unload, load]

        async def _never():
            await asyncio.sleep(3600)

        source.poll = poll_once

        task = asyncio.create_task(manager._run_source(source))
        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert first is unload
        assert second is load


class TestSerialPortExclusivity:
    """Two backend processes of this plugin sharing one PN532 is the failure
    that looks most convincingly like broken hardware: both write command
    frames and race to read the replies, so each sees missing ACKs, frames
    starting mid-stream and checksum errors, while the USB link is perfectly
    stable. It happens because unload cannot cancel the poll thread, so a
    reloaded plugin leaves its previous backend alive holding the port."""

    def _port(self, tmp_path, name="ttyFAKE"):
        import os
        path = tmp_path / name
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
        return path, fd

    def test_a_second_opener_is_refused(self, tmp_path):
        import os
        path, first_fd = self._port(tmp_path)

        first = make_reader()
        first.uart = os.fdopen(first_fd, "r+b", buffering=0)
        assert first._lock_port() is True

        second = make_reader()
        second_fd = os.open(str(path), os.O_RDWR)
        second.uart = os.fdopen(second_fd, "r+b", buffering=0)
        try:
            assert second._lock_port() is False, (
                "a second instance must not be able to open the same port"
            )
            # And it must say so in terms that point at the real cause.
            msg = " ".join(
                str(c) for c in second.logger.error.call_args_list
            )
            assert "already in use" in msg
            assert "lsof" in msg
        finally:
            first.uart.close()
            try:
                second.uart.close()
            except Exception:
                pass

    def test_the_lock_is_released_when_the_holder_closes(self, tmp_path):
        import os
        path, first_fd = self._port(tmp_path)

        first = make_reader()
        first.uart = os.fdopen(first_fd, "r+b", buffering=0)
        assert first._lock_port() is True
        first.uart.close()          # as a dying process would

        second = make_reader()
        second_fd = os.open(str(path), os.O_RDWR)
        second.uart = os.fdopen(second_fd, "r+b", buffering=0)
        try:
            assert second._lock_port() is True, (
                "a genuinely dead instance must not leave the port locked"
            )
        finally:
            second.uart.close()

    def test_a_port_without_a_real_fd_is_not_refused(self):
        """Stubs and non-tty transports have nothing to lock; refusing to
        connect over that would be worse than not locking."""
        reader = make_reader()
        reader.uart = MagicMock()          # fileno() returns a Mock, not an int
        assert reader._lock_port() is True


class TestStopReleasesThePollThread:
    def test_stop_sets_the_flag_before_touching_the_reader(self):
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid", "close"])
        source = make_source(reader)
        import asyncio
        asyncio.get_event_loop().run_until_complete(source.stop())
        assert source._stopping is True

    def test_a_poll_in_flight_gives_up_once_stopping(self):
        """The thread cannot be cancelled, so it has to notice for itself."""
        reader = MagicMock(spec=["read_target", "supports_target_info", "read_uid"])
        reader.supports_target_info = True
        reader.read_target.return_value = TargetInfo(uid=b"\x01\x02\x03\x04", sak=0x08)
        source = make_source(reader)

        source._stopping = True

        assert source._poll_locked() is None
        reader.read_target.assert_not_called()


class TestAutopollTimeoutAndFallback:
    """Both of these produced the same observable failure on real hardware:
    a reader that reconnects every 2.4 seconds forever while still reading
    tags correctly in the gaps — which looks like dying hardware, not a
    software bug."""

    def test_the_sweep_is_given_time_to_finish(self):
        """With a tag present the chip answers on the first target type in
        milliseconds. With none, it works through Type A, Type B, two FeliCa
        rates and Jewel before it can honestly report nothing — so a ceiling
        tuned to the tag-present case fails every single no-tag poll."""
        driver = FakePN532()
        driver.responses[0x60] = [0x00]
        reader = make_reader(driver)

        reader.read_target(timeout=0.2)

        _, _, timeout = driver.calls[0]
        assert timeout >= 1.0, (
            "an InAutoPoll sweep across five target types needs more than a "
            "third of a second when nothing is on the reader"
        )

    def test_a_module_that_cannot_autopoll_falls_back(self):
        """Raising forever was a loop with no exit: the caller's error budget
        drops the reader, reconnect clears the flag, and the same unsupported
        command fails again on the very next poll."""
        driver = FakePN532()
        driver.responses[0x60] = None          # never answers InAutoPoll
        driver.responses[0x4A] = [0x01, 0x01, 0x00, 0x44, 0x00, 0x07,
                                  0x04, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66]
        reader = make_reader(driver)

        # The first failures still surface, so a passing desync is not
        # mistaken for an unsupported command.
        for _ in range(2):
            with pytest.raises(ReaderProtocolError):
                reader.read_target(timeout=0.2)
        assert reader._autopoll_unsupported is False

        # The next one demotes rather than raising, and answers from the
        # legacy path in the same call.
        target = reader.read_target(timeout=0.2)

        assert reader._autopoll_unsupported is True
        assert target is not None
        assert target.uid == b"\x04\x11\x22\x33\x44\x55\x66"
        assert target.sak == 0x00

    def test_once_demoted_it_stops_trying_autopoll(self):
        driver = FakePN532()
        driver.responses[0x60] = None
        driver.responses[0x4A] = [0x00]
        reader = make_reader(driver)

        for _ in range(2):
            with pytest.raises(ReaderProtocolError):
                reader.read_target(timeout=0.2)
        reader.read_target(timeout=0.2)
        driver.calls.clear()

        reader.read_target(timeout=0.2)

        assert all(cmd != 0x60 for cmd, _, _ in driver.calls)

    def test_a_success_resets_the_failure_count(self):
        """An occasional desync must never accumulate into a demotion."""
        driver = FakePN532()
        uid = b"\x04\x11\x22\x33\x44\x55\x66"

        driver.responses[0x60] = None
        reader = make_reader(driver)
        with pytest.raises(ReaderProtocolError):
            reader.read_target(timeout=0.2)
        assert reader._autopoll_failures == 1

        driver.responses[0x60] = autopoll_response(uid)
        reader.read_target(timeout=0.2)
        assert reader._autopoll_failures == 0
        assert reader._autopoll_unsupported is False

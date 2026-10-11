import struct

from switch_mcp.controller import adapter_class
from switch_mcp.controller.adapter_class import (
    GAMEPAD_CLASS, format_class, is_gamepad, pack_mgmt_command, parse_mgmt_reply, parse_read_class_reply,
)


def test_gamepad_is_recognised_whatever_the_service_bits():
    assert is_gamepad(GAMEPAD_CLASS)
    assert is_gamepad(0x000508)
    assert not is_gamepad(0x6C0104)  # a desktop computer
    assert not is_gamepad(None)
    assert format_class(GAMEPAD_CLASS) == "0x002508" and format_class(None) == "unknown"


def test_read_class_reply_is_decoded():
    # Captured from a real adapter: Command Complete for Read Class of Device.
    assert parse_read_class_reply(bytes.fromhex("040e0701230c0004016c")) == 0x6C0104
    assert parse_read_class_reply(bytes.fromhex("040e0701230c0c04016c")) is None  # error status
    assert parse_read_class_reply(bytes.fromhex("040e0701240c0004016c")) is None  # another command
    assert parse_read_class_reply(b"\x04\x0e") is None


def test_mgmt_command_and_reply():
    assert pack_mgmt_command(0x000E, 0, bytes([0x05, 0x08])) == bytes.fromhex("0e00000002000508")
    complete = struct.pack("<HHH", 0x0001, 0, 6) + struct.pack("<HB", 0x000E, 0) + bytes.fromhex("080500")
    assert parse_mgmt_reply(complete, 0x000E) == (0, bytes.fromhex("080500"))
    assert parse_mgmt_reply(complete, 0x0004) is None
    status = struct.pack("<HHH", 0x0002, 0, 3) + struct.pack("<HB", 0x000E, 0x14)
    assert parse_mgmt_reply(status, 0x000E) == (0x14, b"")
    assert parse_mgmt_reply(struct.pack("<HHH", 0x0009, 0, 0), 0x000E) is None


def test_claiming_the_class_returns_what_to_restore(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter_class, "kernel_class", lambda index=0: (0x01, 0x04))
    monkeypatch.setattr(adapter_class, "set_kernel_class", lambda major, minor, index=0: calls.append((major, minor)))
    assert adapter_class.claim_gamepad_class() == (0x01, 0x04)
    assert calls == [(0x05, 0x08)]

    # Already a gamepad (an earlier run did not restore it): nothing to undo.
    monkeypatch.setattr(adapter_class, "kernel_class", lambda index=0: (0x05, 0x08))
    assert adapter_class.claim_gamepad_class() is None
    assert calls == [(0x05, 0x08)]


def test_a_kernel_refusal_is_not_fatal(monkeypatch):
    def refuse(index=0):
        raise OSError("no management interface")

    monkeypatch.setattr(adapter_class, "kernel_class", refuse)
    assert adapter_class.claim_gamepad_class() is None

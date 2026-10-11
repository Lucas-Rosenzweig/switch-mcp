"""Make the adapter announce itself as a gamepad, and check that it does.

The Switch 2 hangs up on a reconnecting device whose class is not a gamepad.
NUXBT writes the class with a raw HCI command, behind the kernel's back: the
kernel still believes the adapter is a computer and writes that class again
whenever bluetoothd changes something that affects it (discoverability, a
profile being registered). Whether a reconnection goes out with the right
class then depends on timing.

Two things are done about it: the kernel is told the major and minor class
through the management interface, so its own rewrites stay a gamepad, and the
class the adapter really holds is read back so the log can tell a refused
reconnection apart from one made under the wrong class.
"""
from __future__ import annotations

import logging
import socket
import struct
import time

from .linkkeys import _EV_COMMAND_COMPLETE, _EV_COMMAND_STATUS, _HEADER, _open_mgmt

log = logging.getLogger(__name__)

# Peripheral / gamepad, with the limited-discoverable bit a Pro Controller sets.
GAMEPAD_CLASS = 0x002508
_GAMEPAD_MAJOR, _GAMEPAD_MINOR = 0x05, 0x08
_OP_READ_INFO = 0x0004
_OP_SET_DEVICE_CLASS = 0x000E
# Offset of the class in the Read Controller Information reply: address,
# version, manufacturer, supported settings, current settings.
_INFO_CLASS_OFFSET = 6 + 1 + 2 + 4 + 4
_HCI_READ_CLASS = 0x0C23
_HCI_EVENT_PACKET = 0x04
_HCI_COMMAND_COMPLETE = 0x0E
_STATUS_BUSY = 0x0A
_BUSY_ATTEMPTS = 5


class MgmtError(OSError):
    def __init__(self, opcode: int, status: int) -> None:
        super().__init__(f"Bluetooth management command 0x{opcode:04x} failed (status {status})")
        self.status = status


def is_gamepad(device_class: int | None) -> bool:
    """True if the major and minor class are a gamepad's, whatever the service bits."""
    return device_class is not None and device_class & 0x1FFF == GAMEPAD_CLASS & 0x1FFF


def format_class(device_class: int | None) -> str:
    return "unknown" if device_class is None else f"0x{device_class:06x}"


def pack_mgmt_command(opcode: int, index: int, params: bytes = b"") -> bytes:
    return _HEADER.pack(opcode, index, len(params)) + params


def parse_mgmt_reply(packet: bytes, opcode: int) -> tuple[int, bytes] | None:
    """(status, return parameters) if `packet` answers `opcode`, else None."""
    if len(packet) < _HEADER.size + 3:
        return None
    event, _index, _length = _HEADER.unpack_from(packet)
    if event not in (_EV_COMMAND_COMPLETE, _EV_COMMAND_STATUS):
        return None
    answered, status = struct.unpack_from("<HB", packet, _HEADER.size)
    if answered != opcode:
        return None
    return status, packet[_HEADER.size + 3:]


def parse_read_class_reply(packet: bytes) -> int | None:
    """Decode the HCI Command Complete event of Read Class of Device."""
    if len(packet) < 10 or packet[0] != _HCI_EVENT_PACKET or packet[1] != _HCI_COMMAND_COMPLETE:
        return None
    opcode, status = struct.unpack_from("<HB", packet, 4)
    if opcode != _HCI_READ_CLASS or status != 0:
        return None
    return int.from_bytes(packet[7:10], "little")


def _mgmt(opcode: int, index: int, params: bytes = b"") -> bytes:
    sock = _open_mgmt()
    try:
        sock.settimeout(3)
        sock.send(pack_mgmt_command(opcode, index, params))
        while True:
            reply = parse_mgmt_reply(sock.recv(1024), opcode)
            if reply is None:
                continue
            status, returned = reply
            if status != 0:
                raise MgmtError(opcode, status)
            return returned
    finally:
        sock.close()


def kernel_class(index: int = 0) -> tuple[int, int]:
    """(major, minor) class the kernel holds for the adapter."""
    info = _mgmt(_OP_READ_INFO, index)
    minor, major = info[_INFO_CLASS_OFFSET], info[_INFO_CLASS_OFFSET + 1]
    # The kernel reports the limited-discoverable bit with the major class
    # but refuses it, and the two format bits of the minor class, when set.
    return major & 0x1F, minor & 0xFC


def set_kernel_class(major: int, minor: int, index: int = 0) -> None:
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            _mgmt(_OP_SET_DEVICE_CLASS, index, bytes([major, minor]))
            return
        except MgmtError as exc:
            # The kernel is still writing a previous class or name change.
            if exc.status != _STATUS_BUSY or attempt == _BUSY_ATTEMPTS - 1:
                raise
            time.sleep(0.2)


def claim_gamepad_class(index: int = 0) -> tuple[int, int] | None:
    """Tell the kernel the adapter is a gamepad; returns the class it held
    before so it can be given back, or None if nothing was changed."""
    try:
        previous = kernel_class(index)
        if previous == (_GAMEPAD_MAJOR, _GAMEPAD_MINOR):
            return None
        set_kernel_class(_GAMEPAD_MAJOR, _GAMEPAD_MINOR, index)
        return previous
    except (OSError, IndexError):
        log.warning("Could not set the adapter class through the kernel", exc_info=True)
        return None


def read_class(index: int = 0) -> int | None:
    """The class of device the adapter holds right now, or None if unreadable."""
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    except OSError:
        return None
    try:
        sock.bind((index,))
        # Only let through the answer to our own command: event packets,
        # Command Complete events, for this opcode.
        sock.setsockopt(
            socket.SOL_HCI, socket.HCI_FILTER,
            struct.pack("<LLLH2x", 1 << _HCI_EVENT_PACKET, 1 << _HCI_COMMAND_COMPLETE, 0, _HCI_READ_CLASS),
        )
        sock.settimeout(1)
        sock.send(b"\x01" + struct.pack("<HB", _HCI_READ_CLASS, 0))
        return parse_read_class_reply(sock.recv(64))
    except OSError:
        return None
    finally:
        sock.close()

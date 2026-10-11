"""Keep the console's Bluetooth link key across sessions.

The Switch pairs with "no bonding", so Linux treats the link key as temporary
and drops it when the link closes, while the console keeps its copy. From then
on the console refuses to pair again and the PC cannot authenticate: only
"Disconnect Controllers" on the console gets out of it. A real controller
simply stores the key, so this module does the same: it records keys as the
kernel announces them and loads them back before a reconnection.

It talks to the kernel's Bluetooth management interface, which requires
CAP_NET_ADMIN.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_HCI_DEV_NONE = 0xFFFF
_HCI_CHANNEL_CONTROL = 3
_OP_LOAD_LINK_KEYS = 0x0012
_EV_COMMAND_COMPLETE = 0x0001
_EV_COMMAND_STATUS = 0x0002
_EV_NEW_LINK_KEY = 0x0009
_BREDR = 0
# For "disconnected" the code is the reason (0 unspecified, 1 timeout, 2 local,
# 3 remote, 4 authentication failure); for the failures it is an error status.
_LINK_EVENTS = {0x000B: "connected", 0x000C: "disconnected", 0x000D: "connect failed", 0x0011: "auth failed"}
_HEADER = struct.Struct("<HHH")
# store hint, address, address type, key type, key, PIN length
_NEW_KEY = struct.Struct("<B6sBB16sB")
_KEY_ENTRY = struct.Struct("<6sBB16sB")


def default_store_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(state) / "switch-mcp" / "link_keys.json"


def _address_to_bytes(address: str) -> bytes:
    return bytes.fromhex(address.replace(":", ""))[::-1]


def _address_from_bytes(raw: bytes) -> str:
    return ":".join(f"{byte:02X}" for byte in raw[::-1])


def parse_new_link_key(params: bytes) -> dict | None:
    """Decode a New Link Key event; None if it is not a BR/EDR key."""
    if len(params) < _NEW_KEY.size:
        return None
    _hint, address, address_type, key_type, key, pin_length = _NEW_KEY.unpack_from(params)
    if address_type != _BREDR:
        return None
    return {
        "address": _address_from_bytes(address),
        "key_type": key_type,
        "key": key.hex(),
        "pin_length": pin_length,
    }


def pack_load_link_keys(index: int, keys: list[dict]) -> bytes:
    params = struct.pack("<BH", 0, len(keys))
    for entry in keys:
        params += _KEY_ENTRY.pack(
            _address_to_bytes(entry["address"]), _BREDR, entry["key_type"],
            bytes.fromhex(entry["key"]), entry["pin_length"],
        )
    return _HEADER.pack(_OP_LOAD_LINK_KEYS, index, len(params)) + params


class KeyStore:
    """Link keys on disk, one per console address, readable by the owner only."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_store_path()
        self._lock = threading.Lock()

    def all(self) -> list[dict]:
        with self._lock:
            try:
                return list(json.loads(self._path.read_text()).values())
            except (OSError, ValueError):
                return []

    def save(self, entry: dict) -> None:
        with self._lock:
            try:
                keys = json.loads(self._path.read_text())
            except (OSError, ValueError):
                keys = {}
            keys[entry["address"]] = entry
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(keys, file)
            os.replace(temporary, self._path)


class _SockaddrHci(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("dev", ctypes.c_ushort), ("channel", ctypes.c_ushort)]


def _open_mgmt() -> socket.socket:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    # Python's own bind() cannot name the control channel on every version.
    address = _SockaddrHci(socket.AF_BLUETOOTH, _HCI_DEV_NONE, _HCI_CHANNEL_CONTROL)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.bind(sock.fileno(), ctypes.byref(address), ctypes.sizeof(address)) != 0:
        error = ctypes.get_errno()
        sock.close()
        raise OSError(error, f"Cannot open the Bluetooth management interface: {os.strerror(error)}")
    return sock


class LinkKeyKeeper:
    def __init__(self, store: KeyStore | None = None) -> None:
        self.store = store or KeyStore()
        self._started = False

    def start(self) -> None:
        """Record every link key the kernel announces from now on."""
        if self._started:
            return
        sock = _open_mgmt()
        self._started = True
        threading.Thread(target=self._listen, args=(sock,), daemon=True, name="link-keys").start()

    def _listen(self, sock: socket.socket) -> None:
        while True:
            try:
                packet = sock.recv(1024)
            except OSError:
                log.warning("Bluetooth management socket closed; link keys are no longer recorded")
                return
            if len(packet) < _HEADER.size:
                continue
            event, _index, _length = _HEADER.unpack_from(packet)
            if event in _LINK_EVENTS and len(packet) >= _HEADER.size + 7:
                # Cheap trace of the link's life, so a failed reconnection can
                # be explained from the log without running btmon as root.
                address = _address_from_bytes(packet[_HEADER.size:_HEADER.size + 6])
                detail = packet[_HEADER.size + 7] if len(packet) > _HEADER.size + 7 else None
                log.info("Bluetooth %s %s (code %s)", _LINK_EVENTS[event], address, detail)
            if event != _EV_NEW_LINK_KEY:
                continue
            entry = parse_new_link_key(packet[_HEADER.size:])
            if entry is not None:
                self.store.save(entry)
                log.info("Stored the link key of console %s", entry["address"])

    def known_consoles(self) -> list[str]:
        return [entry["address"] for entry in self.store.all()]

    def load_into_kernel(self, index: int = 0) -> list[str]:
        """Give the stored keys back to the kernel; returns their addresses.

        This replaces the kernel's whole BR/EDR key list. bluetoothd reloads
        its own keys when it restarts (`switch-mcp stop`).
        """
        keys = self.store.all()
        if not keys:
            return []
        sock = _open_mgmt()
        try:
            sock.settimeout(3)
            sock.send(pack_load_link_keys(index, keys))
            while True:
                packet = sock.recv(1024)
                event, _index, _length = _HEADER.unpack_from(packet)
                if event not in (_EV_COMMAND_COMPLETE, _EV_COMMAND_STATUS):
                    continue
                opcode, status = struct.unpack_from("<HB", packet, _HEADER.size)
                if opcode != _OP_LOAD_LINK_KEYS:
                    continue
                if status != 0:
                    raise OSError(f"The kernel refused the stored link keys (status {status})")
                return [entry["address"] for entry in keys]
        finally:
            sock.close()

"""Log the power mode the console puts the link in.

The console moves the link to sniff mode, where the controller may only
transmit at fixed intervals. How many reports per second get through depends
on that interval, so it is logged whenever it changes.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading

log = logging.getLogger(__name__)

_HCI_EVENT_PACKET = 0x04
_EV_MODE_CHANGE = 0x14
_MODES = {0: "active", 1: "hold", 2: "sniff", 3: "park"}
_SLOT_MS = 0.625


def parse_mode_change(packet: bytes) -> str | None:
    """Describe an HCI Mode Change event, or None for anything else."""
    if len(packet) < 9 or packet[0] != _HCI_EVENT_PACKET or packet[1] != _EV_MODE_CHANGE:
        return None
    status, _handle, mode, interval = struct.unpack_from("<BHBH", packet, 3)
    if status != 0:
        return f"mode change failed (status {status})"
    name = _MODES.get(mode, f"mode {mode}")
    if mode == 0:
        return name
    return f"{name}, one transmit slot every {interval * _SLOT_MS:g} ms"


def start(index: int = 0) -> None:
    """Log link mode changes of adapter `index` from a daemon thread. Needs CAP_NET_RAW."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        sock.bind((index,))
        sock.setsockopt(
            socket.SOL_HCI, socket.HCI_FILTER,
            struct.pack("<LLLH2x", 1 << _HCI_EVENT_PACKET, 1 << _EV_MODE_CHANGE, 0, 0),
        )
    except OSError:
        sock.close()
        raise
    threading.Thread(target=_listen, args=(sock,), daemon=True, name="link-mode").start()


def _listen(sock: socket.socket) -> None:
    while True:
        try:
            described = parse_mode_change(sock.recv(64))
        except OSError:
            log.warning("HCI socket closed; link mode changes are no longer logged")
            return
        if described is not None:
            log.info("Bluetooth link is now %s", described)

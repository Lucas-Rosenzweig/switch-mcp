import stat
import struct

from switch_mcp.controller.linkkeys import KeyStore, pack_load_link_keys, parse_new_link_key

ADDRESS = "98:E2:55:00:11:22"
KEY = bytes(range(16))


def new_key_event(address_type=0):
    return struct.pack("<B6sBB16sB", 0, bytes.fromhex("221100 55E298".replace(" ", "")), address_type, 4, KEY, 0)


def test_parse_new_link_key():
    assert parse_new_link_key(new_key_event()) == {
        "address": ADDRESS, "key_type": 4, "key": KEY.hex(), "pin_length": 0,
    }


def test_low_energy_and_short_events_are_ignored():
    assert parse_new_link_key(new_key_event(address_type=1)) is None
    assert parse_new_link_key(b"\x00\x01") is None


def test_load_command_round_trips_the_event():
    entry = parse_new_link_key(new_key_event())
    packet = pack_load_link_keys(0, [entry])
    opcode, index, length = struct.unpack_from("<HHH", packet)
    assert (opcode, index, length) == (0x0012, 0, len(packet) - 6)
    debug_keys, count = struct.unpack_from("<BH", packet, 6)
    assert (debug_keys, count) == (0, 1)
    # Same bytes as the event, minus its leading store-hint byte.
    assert packet[9:] == new_key_event()[1:]


def test_store_is_private_and_keeps_one_key_per_console(tmp_path):
    path = tmp_path / "state" / "keys.json"
    store = KeyStore(path)
    assert store.all() == []
    entry = parse_new_link_key(new_key_event())
    store.save(entry)
    store.save({**entry, "key": "ff" * 16})
    assert store.all() == [{**entry, "key": "ff" * 16}]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

import json

import pytest

nuxbt = pytest.importorskip("nuxbt.nuxbt")

from switch_mcp.controller import PadState
from switch_mcp.controller import nuxbt_backend
from switch_mcp.controller.nuxbt_backend import NuxbtBackend, build_packet


def template():
    return json.loads(json.dumps(nuxbt.DIRECT_INPUT_PACKET))


def test_buttons_and_sticks_are_mapped():
    packet = build_packet(template(), PadState(
        buttons=frozenset({"A", "UP", "ZL", "L_STICK"}), left=(1.0, -0.5), right=(0.0, 0.25),
    ))
    assert packet["A"] and packet["DPAD_UP"] and packet["ZL"] and packet["L_STICK"]["PRESSED"]
    assert not packet["B"] and not packet["R_STICK"]["PRESSED"]
    assert (packet["L_STICK"]["X_VALUE"], packet["L_STICK"]["Y_VALUE"]) == (100, -50)
    assert (packet["R_STICK"]["X_VALUE"], packet["R_STICK"]["Y_VALUE"]) == (0, 25)


def test_released_state_still_differs_from_nuxbt_idle_packet():
    # Otherwise NUXBT stops streaming and the console sees the release late.
    from nuxbt.controller.input import DIRECT_INPUT_IDLE_PACKET

    packet = build_packet(template(), PadState())
    assert json.dumps(packet) != json.dumps(DIRECT_INPUT_IDLE_PACKET)
    assert {key for key in packet if key not in DIRECT_INPUT_IDLE_PACKET} == {"SWITCH_MCP_KEEP_STREAMING"}


def test_every_pad_button_exists_in_nuxbt():
    from switch_mcp.controller import BUTTONS

    for button in BUTTONS:
        packet = build_packet(template(), PadState(buttons=frozenset({button})))
        assert set(packet) - {"SWITCH_MCP_KEEP_STREAMING"} == set(nuxbt.DIRECT_INPUT_PACKET)


class StubNuxbt:
    def __init__(self):
        self.packets = []
        self.state = {0: {"state": "connecting"}}

    def create_input_packet(self):
        return template()

    def set_controller_input(self, index, packet):
        self.packets.append(packet)


def test_stream_starts_when_the_link_comes_up_and_inputs_wait_for_it(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(nuxbt_backend, "_WATCH_INTERVAL_S", 0.01)
    backend = NuxbtBackend(settle_s=0.1)
    backend._nx, backend._index = StubNuxbt(), 0
    threading.Thread(target=backend._watch_link, args=(0,), daemon=True).start()
    time.sleep(0.05)
    assert backend._nx.packets == [] and backend.status()["state"] == "connecting"

    backend._nx.state[0] = {"state": "connected"}
    time.sleep(0.05)
    # A neutral packet that NUXBT will not treat as idle, so reports flow.
    assert len(backend._nx.packets) == 1 and nuxbt_backend._KEEP_STREAMING in backend._nx.packets[0]
    assert backend.status()["state"] == "settling"
    time.sleep(0.12)
    assert backend.status()["state"] == "connected"

    # NUXBT reconnects by itself after a drop and would replay held inputs.
    backend._nx.set_controller_input(0, build_packet(template(), PadState(buttons=frozenset({"A"}))))
    backend._nx.state[0] = {"state": "reconnecting"}
    time.sleep(0.05)
    assert not backend._nx.packets[-1]["A"] and backend.status()["state"] == "reconnecting"

    backend._index = None  # the watcher stops with its controller


def test_configured_console_limits_direct_reconnection():
    known = ["98:E2:55:00:11:22", "98:E2:55:33:44:55"]
    assert NuxbtBackend()._reconnection_targets(known) == known
    assert NuxbtBackend(switch_address="98:e2:55:33:44:55")._reconnection_targets(known) == ["98:E2:55:33:44:55"]
    # No key for it: reconnecting would be refused, so pair instead.
    assert NuxbtBackend(switch_address="98:E2:55:66:77:88")._reconnection_targets(known) == []


def test_backend_factory_passes_the_configuration():
    from switch_mcp.controller import make_backend

    backend = make_backend("nuxbt", settle_s=1.5, switch_address="98:E2:55:00:11:22")
    assert (backend._settle_s, backend._switch_address) == (1.5, "98:E2:55:00:11:22")
    assert make_backend("nuxbt")._settle_s == nuxbt_backend.DEFAULT_SETTLE_S
    make_backend("fake", settle_s=1.5, switch_address="98:E2:55:00:11:22")  # ignored, not refused

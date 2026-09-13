# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This file is licensed under the MIT license.
# See LICENSE.md file in the project root for details.

"""Tests that the KAYHAN STATUS packet definition accepts everything the
microservice injects into it. inject_tlm writes the item hash into the packet,
and an oversized string or out of range value raises there, which would lose the
whole status report.
"""

import pytest
from openc3.packets.packet_config import PacketConfig

from conftest import TLM_FILE, options, tlm_row

BASE_TIME = 1757275200.0


@pytest.fixture
def status_packet():
    config = PacketConfig()
    config.process_file(str(TLM_FILE), "KAYHAN")
    packet = config.telemetry["KAYHAN"]["STATUS"]
    packet.restore_defaults()
    return packet


def write(packet, item_hash):
    for name, value in item_hash.items():
        packet.write(name, value, "CONVERTED")
    return packet


class TestStatusPacketDefinition:
    def test_defines_the_expected_states(self, status_packet):
        states = status_packet.get_item("STATE").states
        assert set(states) == {"INITIALIZED", "OK", "NO_DATA", "ERROR"}

    def test_consecutive_errors_has_limits(self, status_packet):
        # So a failing integration turns yellow then red without anyone watching
        assert status_packet.get_item("CONSECUTIVE_ERRORS").limits.values["DEFAULT"]


class TestInjectedStatus:
    """Every run injects the same set of items, so each must fit its definition"""

    def _status(self, microservice, kayhan_gps, rows, option_list=None):
        service = microservice(option_list)
        kayhan_gps.rows = rows
        service.run_once()
        return kayhan_gps.injected[-1][2]

    def test_ok_status_writes_cleanly(self, status_packet, microservice, kayhan_gps):
        rows = [tlm_row(BASE_TIME + second) for second in range(60)]
        status = self._status(microservice, kayhan_gps, rows)

        write(status_packet, status)

        assert status_packet.read("STATE") == "OK"
        assert status_packet.read("MESSAGE") == status["MESSAGE"]
        assert status_packet.read("NORAD_ID") == 25544
        assert status_packet.read("SAMPLES_SENT") == 6

    def test_no_data_status_writes_cleanly(self, status_packet, microservice, kayhan_gps):
        status = self._status(microservice, kayhan_gps, [])

        write(status_packet, status)

        assert status_packet.read("STATE") == "NO_DATA"

    def test_error_status_writes_cleanly(self, status_packet, microservice, kayhan_gps):
        status = self._status(microservice, kayhan_gps, [], options(GPS_NORAD_ID=None))

        write(status_packet, status)

        assert status_packet.read("STATE") == "ERROR"
        assert "GPS_NORAD_ID" in status_packet.read("MESSAGE")

    def test_sets_every_item_in_the_packet(self, status_packet, microservice, kayhan_gps):
        status = self._status(microservice, kayhan_gps, [tlm_row(BASE_TIME)])

        # Nothing may be left at its default from a previous run, apart from the
        # PKTID id item and the derived items COSMOS fills in itself
        defined = {item.name for item in status_packet.sorted_items if item.data_type != "DERIVED"}
        assert defined - {"PKTID"} == set(status)

    def test_worst_case_values_fit(self, status_packet, microservice, kayhan_gps):
        status = dict(self._status(microservice, kayhan_gps, [tlm_row(BASE_TIME)]))
        status.update(
            {
                "STATE": "ERROR",
                "MESSAGE": "E" * kayhan_gps.MAX_MESSAGE_CHARS,
                "GPS_SOURCE": "S" * kayhan_gps.MAX_SOURCE_CHARS,
                "NORAD_ID": 10**9 - 1,  # Kayhan allows 9 digit IDs
                "FILENAME": f"gnss_{10**9 - 1}_2026_09_07_20_00_00Z.json",
                "QUERY_START": "2026-09-07T19:00:00.123456Z",
                "QUERY_END": "2026-09-07T20:00:00.123456Z",
                "LAST_SUCCESS": "2026-09-07T20:00:00.123456Z",
                "NEXT_RUN": "2026-09-07T21:00:00.123456Z",
                "UPLOAD_SIZE": 2**32 - 1,
                "SAMPLES_FOUND": 2**32 - 1,
                "SAMPLES_SENT": 2**32 - 1,
                "RUN_COUNT": 2**32 - 1,
                "WINDOW_DURATION": 14400.0,
            }
        )

        write(status_packet, status)

        assert len(status_packet.read("MESSAGE")) == kayhan_gps.MAX_MESSAGE_CHARS
        assert status_packet.read("NORAD_ID") == 10**9 - 1

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This file is licensed under the MIT license.
# See LICENSE.md file in the project root for details.

"""Tests for the KAYHAN_GPS microservice."""

import json
import re

from conftest import AVAILABLE_ITEMS, options, tlm_row

# 2025-09-07T20:00:00Z, an arbitrary but readable epoch for the history windows
BASE_TIME = 1757275200.0
GNSS_TIME_FORMAT = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00"


class TestOptions:
    def test_parses_the_plugin_defaults(self, microservice):
        service = microservice()
        assert service.config_error is None
        assert service.norad_id == 25544
        assert service.kayhan_target_name == "KAYHAN"
        assert service.gps_target_name == "INST"
        assert service.gps_packet_name == "ADCS"
        assert service.gps_item_names == ["POSX", "POSY", "POSZ", "VELX", "VELY", "VELZ"]
        assert service.operator == "Example Operator"
        assert service.ecef_frame == "ITRF2020"
        assert service.position_scale == 1.0
        assert service.velocity_scale == 1.0
        assert service.period == 3600
        assert service.sample_interval == 10.0

    def test_configures_the_satcat_sdk(self, microservice, kayhan_gps):
        microservice()
        settings = kayhan_gps.satcat_settings
        assert settings.auth_method == "client_credentials"
        assert settings.auth_client_id == "test-client-id"
        assert settings.auth_client_secret == "test-client-secret"
        assert settings.default_timeout == 120
        # The SDK inserts an "api" subdomain, which needs the trailing slash
        assert str(settings.satcat_base_url) == "https://satcat.com/"

    def test_operator_name_may_contain_spaces(self, microservice):
        service = microservice(options(KAYHAN_OPERATOR=["Acme", "Space", "Systems"]))
        assert service.operator == "Acme Space Systems"

    def test_velocity_may_be_disabled(self, microservice):
        service = microservice(options(GPS_SEND_VELOCITY="false"))
        assert service.send_velocity is False
        assert service.gps_item_names == ["POSX", "POSY", "POSZ"]

    def test_unknown_option_is_logged_not_fatal(self, microservice):
        service = microservice(options(WHAT_IS_THIS="42"))
        assert service.config_error is None
        assert any("WHAT_IS_THIS" in message for message in service.logger.messages["error"])


class TestConfigValidation:
    """Misconfiguration is reported through telemetry rather than crash looping"""

    def test_norad_id_is_required(self, microservice):
        assert "GPS_NORAD_ID" in microservice(options(GPS_NORAD_ID=None)).config_error

    def test_gps_packet_is_required(self, microservice):
        assert "GPS_PACKET_NAME" in microservice(options(GPS_PACKET_NAME=None)).config_error

    def test_gps_items_are_required(self, microservice):
        assert "item name" in microservice(options(GPS_Y_ITEM_NAME=None)).config_error

    def test_velocity_items_are_only_required_when_sending_velocity(self, microservice):
        assert microservice(options(GPS_VZ_ITEM_NAME=None)).config_error is not None
        service = microservice(options(GPS_VZ_ITEM_NAME=None, GPS_SEND_VELOCITY="false"))
        assert service.config_error is None

    def test_operator_is_required(self, microservice):
        assert "KAYHAN_OPERATOR" in microservice(options(KAYHAN_OPERATOR=None)).config_error

    def test_credentials_are_required(self, microservice, monkeypatch):
        monkeypatch.delenv("SATCAT_AUTH_CLIENT_SECRET")
        assert "KAYHAN_CLIENT_SECRET" in microservice().config_error


class TestQueryGpsHistory:
    def test_requests_the_window_as_five_element_lookup_items(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME)]

        service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        items, kwargs = kayhan_gps.lookup_calls[-1]
        # The time series lookup takes [target, packet, item, value type, limits].
        # Anything else raises "not enough values to unpack (expected 5, got 4)"
        assert all(len(item) == 5 for item in items)
        assert items[0] == ["INST", "ADCS", "PACKET_TIMESECONDS", "RAW", None]
        # get_tlm_available tacks __LIMITS onto POSX which have limits, which we
        # don't need, so no limits state is requested
        assert items[1] == ["INST", "ADCS", "POSX", "RAW", None]
        assert kwargs["start_time"] == "2025-09-07T20:00:00Z"
        assert kwargs["end_time"] == "2025-09-07T20:01:00Z"
        assert kwargs["scope"] == "DEFAULT"

    def test_downsamples_to_the_sample_interval(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME + second) for second in range(60)]

        found, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        assert found == 60
        assert len(samples) == 6
        assert samples[0]["time"] == "2025-09-07T20:00:00.000000+00:00"
        assert samples[1]["time"] == "2025-09-07T20:00:10.000000+00:00"

    def test_unpacks_position_and_velocity(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME)]

        _, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        assert samples[0] == {
            "time": "2025-09-07T20:00:00.000000+00:00",
            "ecefPosX": 1.0,
            "ecefPosY": 2.0,
            "ecefPosZ": 3.0,
            "ecefVelX": 4.0,
            "ecefVelY": 5.0,
            "ecefVelZ": 6.0,
            "dataValid": 1,
        }

    def test_position_only_omits_the_velocity_fields(self, microservice, kayhan_gps):
        service = microservice(options(GPS_SEND_VELOCITY="false"))
        kayhan_gps.available = AVAILABLE_ITEMS[:4]
        kayhan_gps.rows = [tlm_row(BASE_TIME)[:4]]

        _, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        assert list(samples[0]) == ["time", "ecefPosX", "ecefPosY", "ecefPosZ", "dataValid"]

    def test_scales_telemetry_to_meters(self, microservice, kayhan_gps):
        # Kayhan fixes the unit system to kg-m-s-rad, so kilometer telemetry is scaled
        service = microservice(options(GPS_POSITION_SCALE="1000.0", GPS_VELOCITY_SCALE="1000.0"))
        kayhan_gps.rows = [tlm_row(BASE_TIME)]

        _, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        assert samples[0]["ecefPosX"] == 1000.0
        assert samples[0]["ecefVelZ"] == 6000.0

    def test_handles_a_flattened_single_row_response(self, microservice, kayhan_gps):
        # The lookup returns just the pairs, not a list of rows, for one row
        service = microservice()
        kayhan_gps.rows = tlm_row(BASE_TIME)

        found, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        assert found == 1
        assert samples[0]["ecefPosX"] == 1.0

    def test_handles_no_data(self, microservice, kayhan_gps):
        service = microservice()
        for empty in ({}, []):
            kayhan_gps.rows = empty
            assert service.query_gps_history(BASE_TIME, BASE_TIME + 60) == (0, [])

    def test_skips_rows_missing_a_value(self, microservice, kayhan_gps):
        service = microservice()
        without_fix = tlm_row(BASE_TIME + 100)
        without_fix[2][0] = None
        kayhan_gps.rows = [tlm_row(BASE_TIME), without_fix, tlm_row(BASE_TIME + 200)]

        found, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 300)

        assert found == 3
        assert len(samples) == 2

    def test_sorts_samples_by_time(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME + 20), tlm_row(BASE_TIME), tlm_row(BASE_TIME + 10)]

        _, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        assert [sample["time"] for sample in samples] == [
            "2025-09-07T20:00:00.000000+00:00",
            "2025-09-07T20:00:10.000000+00:00",
            "2025-09-07T20:00:20.000000+00:00",
        ]

    def test_raises_when_an_item_does_not_exist(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.available = [AVAILABLE_ITEMS[0], None] + AVAILABLE_ITEMS[2:]

        try:
            service.query_gps_history(BASE_TIME, BASE_TIME + 60)
            raise AssertionError("expected a RuntimeError")
        except RuntimeError as error:
            assert "POSX" in str(error)


class TestUploadGnss:
    def test_uses_the_kayhan_filename_convention(self, microservice):
        service = microservice()
        filename, _ = service.upload_gnss([], BASE_TIME)
        assert filename == "gnss_25544_2025_09_07_20_00_00Z.json"

    def test_payload_matches_the_kayhan_example(self, microservice, kayhan_gps, gnss_example):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME + second * 10) for second in range(4)]
        _, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 60)

        filename, size = service.upload_gnss(samples, BASE_TIME)

        object_id, content, uploaded_name = kayhan_gps.uploads[-1]
        assert object_id == 25544
        assert uploaded_name == filename
        assert size == len(content)
        payload = json.loads(content)
        assert payload["meta"] == {
            "operator": "Example Operator",
            "objectId": 25544,
            "timeSystem": "UTC",
            "ecefFrame": "ITRF2020",
            "unitSystem": "kg-m-s-rad",
        }
        # Same keys, in the same order, holding the same types as Kayhan's example
        assert list(payload) == list(gnss_example)
        assert list(payload["meta"]) == list(gnss_example["meta"])
        assert list(payload["data"][0]) == list(gnss_example["data"][0])
        for key, value in gnss_example["meta"].items():
            assert type(payload["meta"][key]) is type(value)
        for key, value in gnss_example["data"][0].items():
            assert type(payload["data"][0][key]) is type(value)

    def test_measurement_times_match_the_example_format(self, gnss_example):
        assert re.fullmatch(GNSS_TIME_FORMAT, gnss_example["data"][0]["time"])
        assert re.fullmatch(GNSS_TIME_FORMAT, gnss_example["data"][-1]["time"])

    def test_round_trips_the_kayhan_example(self, microservice, kayhan_gps, gnss_example):
        """Feeding the example's own measurements back through the microservice
        must reproduce the example file"""
        from datetime import datetime

        service = microservice(options(GPS_NORAD_ID="12345"))
        service.sample_interval = 1  # The example samples once a minute
        kayhan_gps.rows = [
            [[datetime.fromisoformat(entry["time"]).timestamp(), None]]
            + [[entry[key], None] for key in ["ecefPosX", "ecefPosY", "ecefPosZ", "ecefVelX", "ecefVelY", "ecefVelZ"]]
            for entry in gnss_example["data"]
        ]

        _, samples = service.query_gps_history(BASE_TIME, BASE_TIME + 3600)
        filename, _ = service.upload_gnss(samples, datetime.fromisoformat("2025-03-26T17:41:39+00:00").timestamp())

        assert filename == "gnss_12345_2025_03_26_17_41_39Z.json"
        assert json.loads(kayhan_gps.uploads[-1][1]) == gnss_example


class TestRunOnce:
    def test_reports_a_successful_upload(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME + second) for second in range(60)]

        service.run_once()

        target_name, packet_name, status = kayhan_gps.injected[-1]
        assert (target_name, packet_name) == ("KAYHAN", "STATUS")
        assert status["STATE"] == "OK"
        assert status["SAMPLES_FOUND"] == 60
        assert status["SAMPLES_SENT"] == 6
        assert status["RUN_COUNT"] == 1
        assert status["SUCCESS_COUNT"] == 1
        assert status["ERROR_COUNT"] == 0
        assert status["CONSECUTIVE_ERRORS"] == 0
        assert status["NORAD_ID"] == 25544
        assert status["GPS_SOURCE"] == "INST ADCS POSX POSY POSZ VELX VELY VELZ"
        assert status["FILENAME"].startswith("gnss_25544_")
        assert status["UPLOAD_SIZE"] > 0
        assert status["LAST_SUCCESS"]

    def test_reports_no_data(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = []

        service.run_once()

        status = kayhan_gps.injected[-1][2]
        assert status["STATE"] == "NO_DATA"
        assert "No INST ADCS GPS samples" in status["MESSAGE"]
        assert status["SUCCESS_COUNT"] == 0
        assert status["ERROR_COUNT"] == 0
        assert kayhan_gps.uploads == []

    def test_reports_an_error_and_keeps_the_window_for_retry(self, microservice, kayhan_gps):
        service = microservice()

        def explode(items, **kwargs):
            raise RuntimeError("tsdb unavailable")

        kayhan_gps.CvtModel.tsdb_lookup = explode

        service.run_once()
        failed = kayhan_gps.injected[-1][2]
        service.run_once()
        retry = kayhan_gps.injected[-1][2]

        assert failed["STATE"] == "ERROR"
        assert "tsdb unavailable" in failed["MESSAGE"]
        assert failed["ERROR_COUNT"] == 1
        assert failed["CONSECUTIVE_ERRORS"] == 1
        # The same GPS data is queried again next period rather than being lost
        assert retry["QUERY_START"] == failed["QUERY_START"]
        assert retry["QUERY_END"] > failed["QUERY_END"]
        assert retry["CONSECUTIVE_ERRORS"] == 2
        # The failure is logged with its traceback, not just reported in STATUS
        logged = service.logger.messages["error"][-1]
        assert "tsdb unavailable" in logged
        assert "Traceback" in logged

    def test_truncates_a_long_error_to_fit_the_message_item(self, microservice, kayhan_gps):
        service = microservice()

        def explode(items, **kwargs):
            raise RuntimeError("x" * 5000)

        kayhan_gps.CvtModel.tsdb_lookup = explode

        service.run_once()

        assert len(kayhan_gps.injected[-1][2]["MESSAGE"]) == kayhan_gps.MAX_MESSAGE_CHARS

    def test_advances_the_window_after_a_successful_run(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME)]

        service.run_once()
        first_end = kayhan_gps.injected[-1][2]["QUERY_END"]
        service.run_once()

        # The next run picks up exactly where the last one stopped
        assert kayhan_gps.injected[-1][2]["QUERY_START"] == first_end

    def test_clamps_how_far_a_retry_backfills(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = []
        service.window_start = 0  # As if the upload had been failing for years

        service.run_once()

        status = kayhan_gps.injected[-1][2]
        expected = service.period * kayhan_gps.MAX_BACKFILL_PERIODS
        assert abs(status["WINDOW_DURATION"] - expected) < 2

    def test_reports_misconfiguration_instead_of_crashing(self, microservice, kayhan_gps):
        service = microservice(options(GPS_NORAD_ID=None))

        service.run_once()

        status = kayhan_gps.injected[-1][2]
        assert status["STATE"] == "ERROR"
        assert "GPS_NORAD_ID" in status["MESSAGE"]
        # A NORAD ID is still needed to fill the UINT item
        assert status["NORAD_ID"] == 0

    def test_a_failed_status_injection_does_not_stop_the_microservice(self, microservice, kayhan_gps):
        service = microservice()
        kayhan_gps.rows = [tlm_row(BASE_TIME)]

        def explode(target_name, packet_name, item_hash):
            raise RuntimeError("decom microservice is down")

        kayhan_gps.inject_tlm = explode

        service.run_once()  # Must not raise

        assert any("decom microservice is down" in message for message in service.logger.messages["error"])

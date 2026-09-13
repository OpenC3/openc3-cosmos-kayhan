# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This file is licensed under the MIT license.
# See LICENSE.md file in the project root for details.

"""Shared fixtures for the Kayhan plugin tests.

The KAYHAN_GPS microservice is imported for real, including the satcat SDK. Only
the pieces which need a running COSMOS are replaced: the Microservice base class
constructor (which reads its config from Redis) and the telemetry APIs (which
read and write Redis and the time series database).
"""

import json
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
sys.path.insert(0, str(PLUGIN_DIR / "microservices" / "KAYHAN_GPS"))

os.environ["OPENC3_NO_STORE"] = "1"
# The microservice reads its Kayhan credentials from the secrets COSMOS injects
os.environ.setdefault("SATCAT_AUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("SATCAT_AUTH_CLIENT_SECRET", "test-client-secret")

TLM_FILE = PLUGIN_DIR / "targets" / "KAYHAN" / "cmd_tlm" / "tlm.txt"
# The GNSS file format Kayhan provided. The uploaded payload is asserted against
# this so a change to either end fails the tests rather than the upload.
GNSS_EXAMPLE_FILE = TESTS_DIR / "gnss_example.json"

# The microservice OPTIONs as plugin.txt renders them with its defaults
DEFAULT_OPTIONS = [
    ["KAYHAN_TARGET_NAME", "KAYHAN"],
    ["GPS_NORAD_ID", "25544"],
    ["GPS_TARGET_NAME", "INST"],
    ["GPS_PACKET_NAME", "ADCS"],
    ["GPS_X_ITEM_NAME", "POSX"],
    ["GPS_Y_ITEM_NAME", "POSY"],
    ["GPS_Z_ITEM_NAME", "POSZ"],
    ["GPS_VX_ITEM_NAME", "VELX"],
    ["GPS_VY_ITEM_NAME", "VELY"],
    ["GPS_VZ_ITEM_NAME", "VELZ"],
    ["GPS_SEND_VELOCITY", "true"],
    ["KAYHAN_OPERATOR", "Example Operator"],
    ["GPS_ECEF_FRAME", "ITRF2020"],
    ["GPS_POSITION_SCALE", "1.0"],
    ["GPS_VELOCITY_SCALE", "1.0"],
    ["PERIOD", "3600"],
    ["SAMPLE_INTERVAL", "10"],
    ["SATCAT_BASE_URL", "https://satcat.com"],
    ["SATCAT_TIMEOUT", "120"],
]

# What get_tlm_available returns for the default options. POSX carries limits so
# the __LIMITS suffix handling is covered by every test which queries history.
AVAILABLE_ITEMS = [
    "INST__ADCS__PACKET_TIMESECONDS__RAW",
    "INST__ADCS__POSX__RAW__LIMITS",
    "INST__ADCS__POSY__RAW",
    "INST__ADCS__POSZ__RAW",
    "INST__ADCS__VELX__RAW",
    "INST__ADCS__VELY__RAW",
    "INST__ADCS__VELZ__RAW",
]


def options(**overrides):
    """The default OPTIONs with individual ones replaced, or removed when None.
    A list value becomes a multi word OPTION, e.g. ["Acme", "Space"]"""
    result = [option for option in DEFAULT_OPTIONS if option[0] not in overrides]
    for name, value in overrides.items():
        if value is None:
            continue
        result.append([name] + (list(value) if isinstance(value, list) else [value]))
    return result


def tlm_row(seconds, offset=0.0):
    """One get_tlm_values history row of [value, limits_state] pairs:
    packet time, then position XYZ and velocity XYZ"""
    values = [seconds] + [component + offset for component in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]]
    return [[value, None] for value in values]


class FakeLogger:
    def __init__(self):
        self.messages = {"info": [], "warn": [], "error": []}

    def info(self, message, **kwargs):
        self.messages["info"].append(message)

    def warn(self, message, **kwargs):
        self.messages["warn"].append(message)

    def error(self, message, **kwargs):
        self.messages["error"].append(message)


class FakeSleeper:
    def sleep(self, _seconds):
        return False

    def cancel(self):
        pass


@pytest.fixture
def gnss_example():
    """The GNSS file format example provided by Kayhan"""
    with open(GNSS_EXAMPLE_FILE) as file:
        return json.load(file)


@pytest.fixture
def kayhan_gps(monkeypatch):
    """The kayhan_gps module with its COSMOS and Kayhan dependencies stubbed.

    Exposes the calls the microservice made so tests can assert on them:
      module.tlm_values_calls - (items, kwargs) passed to get_tlm_values
      module.injected         - (target, packet, item_hash) passed to inject_tlm
      module.uploads          - (object_id, content, filename) sent to Kayhan
      module.available        - what get_tlm_available returns, set per test
      module.rows             - what get_tlm_values returns, set per test
    """
    import openc3.microservices.microservice as microservice_module

    # The real constructor reads the microservice config from Redis. Stand in for
    # it, taking the OPTIONs the microservice fixture selected for this test.
    selected_options = {"options": DEFAULT_OPTIONS}

    def fake_init(self, name):
        self.name = name
        self.scope = name.split("__")[0]
        self.config = {"options": selected_options["options"]}
        self.logger = FakeLogger()
        self.state = "INITIALIZED"
        self.error = None
        self.count = 0
        self.cancel_thread = False

    monkeypatch.setattr(microservice_module.Microservice, "__init__", fake_init)
    monkeypatch.setattr(microservice_module.Microservice, "shutdown", lambda self: None)

    import kayhan_gps as module

    module.selected_options = selected_options
    module.available = list(AVAILABLE_ITEMS)
    module.rows = []
    module.tlm_values_calls = []
    module.injected = []
    module.uploads = []

    def get_tlm_available(items):
        return module.available

    def get_tlm_values(items, **kwargs):
        module.tlm_values_calls.append((items, kwargs))
        return module.rows

    def inject_tlm(target_name, packet_name, item_hash):
        module.injected.append((target_name, packet_name, item_hash))

    monkeypatch.setattr(module, "get_tlm_available", get_tlm_available)
    monkeypatch.setattr(module, "get_tlm_values", get_tlm_values)
    monkeypatch.setattr(module, "inject_tlm", inject_tlm)
    monkeypatch.setattr(module, "Sleeper", FakeSleeper)

    # Capture uploads instead of calling the real Kayhan SatCat API
    class FakeODClient:
        def __init__(self, client):
            self.client = client

        def upload_file(self, object_id, path_or_buf, filename=None):
            module.uploads.append((object_id, path_or_buf.read(), filename))
            return {"status": "ok"}

    monkeypatch.setattr(module.Client, "od", property(FakeODClient))
    monkeypatch.setattr(module.Client, "__enter__", lambda self: self)
    monkeypatch.setattr(module.Client, "__exit__", lambda self, *args: None)
    # The real constructor checks PyPI for a newer SDK over the network
    monkeypatch.setattr(module.Client, "__init__", lambda self, *args, **kwargs: None)

    return module


@pytest.fixture
def microservice(kayhan_gps):
    """Build a KayhanGps with the given OPTIONs, defaulting to the plugin defaults"""

    def build(option_list=None):
        kayhan_gps.selected_options["options"] = DEFAULT_OPTIONS if option_list is None else option_list
        return kayhan_gps.KayhanGps("DEFAULT__USER__KAYHAN_GPS")

    return build

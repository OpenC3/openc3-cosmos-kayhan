# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This file is licensed under the MIT license.
# See LICENSE.md file in the project root for details.

"""Tests that plugin.txt, the telemetry definition and the screen agree with the
microservice. A typo in an OPTION name only shows up as an "Unknown option" log
line at runtime, and a screen naming a missing item only fails when opened.
"""

import re

import pytest
from openc3.packets.packet_config import PacketConfig

from conftest import PLUGIN_DIR, TLM_FILE

PLUGIN_FILE = PLUGIN_DIR / "plugin.txt"
SCREEN_FILE = PLUGIN_DIR / "targets" / "KAYHAN" / "screens" / "status.txt"
# The plugin.txt ERB is only ever "<%= variable %>", which this substitutes
ERB_TAG = re.compile(r"<%=\s*(\w+)\s*%>")


def render_plugin():
    """plugin.txt with its VARIABLE defaults substituted, as COSMOS renders it"""
    source = PLUGIN_FILE.read_text()
    variables = dict(re.findall(r"^VARIABLE\s+(\S+)\s+(.*)$", source, re.MULTILINE))
    variables = {name: value.strip().strip('"') for name, value in variables.items()}
    unknown = {tag for tag in ERB_TAG.findall(source) if tag not in variables}
    assert not unknown, f"plugin.txt uses undeclared variables: {sorted(unknown)}"
    assert "<%" not in ERB_TAG.sub(lambda m: variables[m.group(1)], source), (
        "plugin.txt grew ERB this test cannot render, update render_plugin"
    )
    return ERB_TAG.sub(lambda match: variables[match.group(1)], source)


@pytest.fixture(scope="module")
def rendered():
    return render_plugin()


@pytest.fixture(scope="module")
def plugin_options(rendered):
    """The OPTION lines as the microservice receives them, [name, *values]"""
    lines = re.findall(r"^\s*OPTION\s+(.*)$", rendered, re.MULTILINE)
    return [[part.strip('"') for part in re.findall(r'"[^"]*"|\S+', line)] for line in lines]


class TestPluginOptions:
    def test_every_option_is_handled_by_the_microservice(self, microservice, plugin_options):
        service = microservice(plugin_options)

        unknown = [message for message in service.logger.messages["error"] if "Unknown option" in message]
        assert unknown == []

    def test_the_defaults_are_a_valid_configuration(self, microservice, plugin_options):
        service = microservice(plugin_options)

        assert service.config_error is None

    def test_the_defaults_land_where_expected(self, microservice, plugin_options):
        service = microservice(plugin_options)

        # An operator name with spaces has to survive the config parser
        assert service.operator == "Example Operator"
        assert service.norad_id > 0
        assert service.period > 0
        assert service.sample_interval > 0
        assert service.gps_item_names == ["POSX", "POSY", "POSZ", "VELX", "VELY", "VELZ"]

    def test_the_status_target_matches_the_telemetry_definition(self, microservice, plugin_options, rendered):
        service = microservice(plugin_options)
        target_name = re.search(r"^TARGET\s+KAYHAN\s+(\S+)$", rendered, re.MULTILINE).group(1)

        # The microservice injects into whatever the TARGET line named
        assert service.kayhan_target_name == target_name.upper()

    def test_credentials_are_declared_as_secrets(self, rendered):
        secrets = dict(re.findall(r"^\s*SECRET\s+ENV\s+(\S+)\s+(\S+)$", rendered, re.MULTILINE))
        # The environment variable names the satcat SDK and microservice read
        assert secrets == {
            "KAYHAN_CLIENT_ID": "SATCAT_AUTH_CLIENT_ID",
            "KAYHAN_CLIENT_SECRET": "SATCAT_AUTH_CLIENT_SECRET",
        }


def screen_widgets():
    """The LABELVALUE lines in the status screen, split into their parameters"""
    widgets = []
    for line in SCREEN_FILE.read_text().splitlines():
        parameters = line.split()
        if parameters and parameters[0] == "LABELVALUE":
            widgets.append(parameters[1:])
    return widgets


class TestStatusScreen:
    def test_every_screen_item_exists(self):
        config = PacketConfig()
        config.process_file(str(TLM_FILE), "KAYHAN")
        packet = config.telemetry["KAYHAN"]["STATUS"]
        defined = {item.name for item in packet.sorted_items}

        named = {parameters[2] for parameters in screen_widgets()}

        assert named, "the status screen displays no items"
        assert named <= defined, f"screen names missing items: {sorted(named - defined)}"

    def test_screen_targets_the_status_packet(self):
        for parameters in screen_widgets():
            assert parameters[0:2] == ["KAYHAN", "STATUS"], parameters

    def test_screen_value_types_are_supported(self):
        # WITH_UNITS is not a screen value type, only RAW, CONVERTED and FORMATTED
        types = {parameters[3] for parameters in screen_widgets() if len(parameters) > 3}

        assert types <= {"RAW", "CONVERTED", "FORMATTED"}, types

"""Tests for the diagnostics that make user bug reports actionable.

Two separate incidents motivated these: issue #40 (a broker credential
rejection surfacing as a raw aiomqtt traceback that read like a crash in
shark2mqtt) and issue #27 (a reporter who could see which shadow
properties existed but never what they were set to).
"""

from __future__ import annotations

import logging

import pytest
from aiomqtt.exceptions import MqttConnectError, MqttError

from src.main import _is_mqtt_auth_failure
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device


class TestMqttAuthFailureDetection:
    """The credential hint must key off the numeric code, not message text.

    MQTT 3.1.1 reports a bare CONNACK int; MQTT 5 reports a paho ReasonCode
    whose number lives in `.value`. The stringified message differs between
    them (135 renders as "Unknown error" when built from a bare int), so
    substring matching on the message is not reliable.
    """

    @pytest.mark.parametrize("rc", [4, 5])
    def test_v311_credential_codes(self, rc):
        assert _is_mqtt_auth_failure(MqttConnectError(rc)) is True

    @pytest.mark.parametrize("rc", [134, 135])
    def test_v5_reason_codes(self, rc):
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        err = MqttConnectError(ReasonCode(PacketTypes.CONNACK, identifier=rc))
        assert _is_mqtt_auth_failure(err) is True

    def test_matches_the_error_reported_in_issue_40(self):
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.reasoncodes import ReasonCode

        err = MqttConnectError(ReasonCode(PacketTypes.CONNACK, identifier=135))
        assert str(err) == "[code:135] Not authorized"
        assert _is_mqtt_auth_failure(err) is True

    @pytest.mark.parametrize("rc", [1, 2, 3])
    def test_non_credential_refusals(self, rc):
        # Wrong protocol version / bad client id / server unavailable are
        # real failures, but telling the user to check their password
        # would send them down the wrong path.
        assert _is_mqtt_auth_failure(MqttConnectError(rc)) is False

    def test_plain_mqtt_error_has_no_code(self):
        assert _is_mqtt_auth_failure(MqttError("broker went away")) is False


class TestShadowValueDump:
    """Issue #27: names alone can't answer "what was it set to?"."""

    def _dump(self, caplog) -> str:
        return "\n".join(
            r.message for r in caplog.records if "Shadow values" in r.message
        )

    def test_dump_includes_property_values(self, caplog):
        data = make_skegox_device()
        data["shadow"]["properties"]["reported"]["SmartMopEnabled"] = {"value": 1}
        data["shadow"]["properties"]["reported"]["PadPriming"] = {"value": 0}

        with caplog.at_level(logging.DEBUG, logger="src.shark_device"):
            SharkVacuum.from_skegox(data)

        dump = self._dump(caplog)
        assert "SmartMopEnabled" in dump
        assert "PadPriming" in dump
        # The whole point: the value, not just the name.
        assert "'SmartMopEnabled': 1" in dump
        assert "'PadPriming': 0" in dump

    def test_long_values_are_truncated(self, caplog):
        data = make_skegox_device()
        data["shadow"]["properties"]["reported"]["JSON_Data_Log"] = {
            "value": "x" * 5000
        }

        with caplog.at_level(logging.DEBUG, logger="src.shark_device"):
            SharkVacuum.from_skegox(data)

        dump = self._dump(caplog)
        assert "truncated" in dump
        # A 5000-char blob must not land in the log in full.
        assert "x" * 500 not in dump

    def test_nothing_dumped_above_debug(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.shark_device"):
            SharkVacuum.from_skegox(make_skegox_device())
        assert self._dump(caplog) == ""

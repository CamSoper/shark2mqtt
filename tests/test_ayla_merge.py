"""Tests for AYLA_MERGE: Ayla-only robots on half-migrated accounts (#46)."""

from __future__ import annotations

import asyncio

import pytest

from src.main import _ayla_only_devices, poll_loop
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device


def ayla_vac(dsn: str, *, online: bool = True, bsn: str | None = "absent") -> SharkVacuum:
    vac = SharkVacuum({
        "dsn": dsn,
        "product_name": f"Ayla {dsn}",
        "connection_status": "Online" if online else "Offline",
    })
    if bsn != "absent":
        vac._properties["GET_Battery_Serial_Num"] = bsn
    return vac


class TestAylaOnlyDevices:
    def test_keeps_online_robot_skegox_lacks(self):
        vac = ayla_vac("AC000W1", bsn=None)
        assert _ayla_only_devices([vac], {"SND1"}) == [vac]

    def test_skips_robot_whose_battery_serial_maps_to_skegox(self):
        vac = ayla_vac("AC000W2", bsn="BSN-SND1")
        assert _ayla_only_devices([vac], {"SND1"}) == []

    def test_skips_robot_with_same_dsn(self):
        vac = ayla_vac("SND1")
        assert _ayla_only_devices([vac], {"SND1"}) == []

    def test_skips_offline_stale_record(self):
        vac = ayla_vac("AC000W3", online=False)
        assert _ayla_only_devices([vac], {"SND1"}) == []

    def test_unrelated_battery_serial_is_kept(self):
        vac = ayla_vac("AC000W4", bsn="BSN-OTHER")
        assert _ayla_only_devices([vac], {"SND1"}) == [vac]


async def _one_poll(mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event):
    mock_config.poll_interval = 0.2
    mock_config.map_image = False
    mock_api.fetch_property_file.return_value = None  # no MARD
    task = asyncio.create_task(poll_loop(
        mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config,
        {}, {}, {}, command_event,
    ))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_off_by_default_never_calls_ayla(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
):
    mock_api.get_all_devices.return_value = [make_skegox_device(dsn="SND1")]
    mock_ayla_api.get_devices.return_value = [ayla_vac("AC000W1", bsn=None)]

    await _one_poll(mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event)

    mock_ayla_api.get_devices.assert_not_called()
    published = {c.args[0].dsn for c in mock_mqtt.publish_discovery.call_args_list}
    assert published == {"SND1"}


@pytest.mark.asyncio
async def test_on_publishes_ayla_only_robot(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
):
    mock_config.ayla_merge = True
    mock_api.get_all_devices.return_value = [make_skegox_device(dsn="SND1")]
    extra = ayla_vac("AC000W1", bsn=None)
    stale = ayla_vac("AC000W2", bsn="BSN-SND1")
    mock_ayla_api.get_devices.return_value = [extra, stale]

    await _one_poll(mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event)

    published = {c.args[0].dsn for c in mock_mqtt.publish_discovery.call_args_list}
    assert published == {"SND1", "AC000W1"}

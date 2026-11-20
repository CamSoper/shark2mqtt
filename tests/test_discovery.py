"""Discovery IDs must not change: dashboards, scripts, Assist and HomeKit
depend on entity IDs derived from these unique_id / object_id values."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.mqtt_client import MqttClient
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device
from .ur2850_fixtures import make_ur2850

# Entities every robot gets, as (unique_id suffix, object_id suffix), from
# the v1.5.5 discovery code. "" is the vacuum itself.
BASE_ENTITIES = {
    ("", ""),
    ("_battery", "_battery"),
    ("_rssi", "_rssi"),
    ("_charging", "_charging"),
    ("_error", "_error"),
    ("_error_text", "_error_text"),
    ("_evacuating", "_evacuating"),
    ("_warning", "_warning"),
    ("_dock_error", "_dock_error"),
    ("_runtime", "_runtime"),
    ("_replace_battery", "_replace_battery"),
    ("_recommend_randr", "_recommend_randr"),
}
ROOM_ENTITIES = {("_clean_mode", "_clean_mode")}
MOP_ENTITIES = {("_water_flow", "_water_flow")}
NEW_JOB_ENTITIES = {("_sys_st", "_sys_st"), ("_job_active", "_job_active")}


async def discovered_ids(mock_config, vac: SharkVacuum) -> set[tuple[str, str]]:
    client = MqttClient(mock_config)
    client._publish = AsyncMock()
    await client.publish_discovery(vac)
    ids = set()
    for call in client._publish.call_args_list:
        payload = call.args[1]
        if isinstance(payload, dict) and "unique_id" in payload:
            ids.add((payload["unique_id"], payload["object_id"]))
    return ids


def expected(uid: str, slug: str, entities: set[tuple[str, str]]) -> set[tuple[str, str]]:
    return {(uid + u, slug + o) for u, o in entities}


@pytest.mark.asyncio
async def test_legacy_robot_ids_unchanged(mock_config):
    vac = SharkVacuum.from_skegox(make_skegox_device(dsn="DSN123", name="Test Shark"))
    ids = await discovered_ids(mock_config, vac)
    assert ids == expected("shark2mqtt_DSN123", "test_shark", BASE_ENTITIES)


@pytest.mark.asyncio
async def test_ur2850_ids_unchanged_plus_new_entities(mock_config):
    vac = make_ur2850("docked_idle")
    ids = await discovered_ids(mock_config, vac)

    uid, slug = "shark2mqtt_SND_TEST", "sharkvac"
    room_buttons = {
        (f"{uid}_clean_{r}", f"{slug}_clean_{r}")
        for r in ("room_a", "room_b", "room_c", "room_d")
    }
    assert ids == (
        expected(uid, slug, BASE_ENTITIES | ROOM_ENTITIES | MOP_ENTITIES | NEW_JOB_ENTITIES)
        | room_buttons
    )


@pytest.mark.asyncio
async def test_new_entities_follow_field_presence(mock_config):
    vac = make_ur2850("docked_idle", Operating_Mode_Ex=None)
    ids = await discovered_ids(mock_config, vac)
    assert ("shark2mqtt_SND_TEST_sys_st", "sharkvac_sys_st") not in ids
    assert ("shark2mqtt_SND_TEST_job_active", "sharkvac_job_active") in ids

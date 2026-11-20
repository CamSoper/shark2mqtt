"""Tests for poll_loop command_event wake-up behavior."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.main import poll_loop
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device
from .ur2850_fixtures import ur2850_raw


@pytest.mark.asyncio
async def test_poll_wakes_early_on_command_event(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
):
    """Poll loop should wake immediately when command_event is set."""
    mock_config.poll_interval = 10  # long enough we'd notice waiting

    poll_count = 0

    async def counting_get_all():
        nonlocal poll_count
        poll_count += 1
        return []

    mock_api.get_all_devices.side_effect = counting_get_all

    # Set the event after a short delay to wake the loop early
    async def set_event_soon():
        await asyncio.sleep(0.1)
        command_event.set()

    start = time.monotonic()
    task = asyncio.create_task(poll_loop(
        mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config,
        {}, {}, {}, command_event,
    ))
    await set_event_soon()
    # Give the loop time to wake, wait the 5s post-command delay, and run the second poll
    await asyncio.sleep(5.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start

    assert poll_count == 2
    # Should complete well under the 10s poll interval (5s delay + overhead)
    assert elapsed < 8.0


@pytest.mark.asyncio
async def test_poll_waits_full_interval_without_event(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
):
    """Without the event, poll loop waits the full interval."""
    mock_config.poll_interval = 0.3

    poll_count = 0

    async def counting_get_all():
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 2:
            raise asyncio.CancelledError
        return []

    mock_api.get_all_devices.side_effect = counting_get_all

    start = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await poll_loop(
            mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config,
            {}, {}, {}, command_event,
        )
    elapsed = time.monotonic() - start

    assert poll_count == 2
    # Should have waited roughly the full interval
    assert elapsed >= 0.25


@pytest.mark.asyncio
async def test_event_cleared_after_wake(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
):
    """Event should be cleared after waking the poll loop."""
    mock_config.poll_interval = 10

    poll_count = 0
    event_state_after_wake = None

    async def counting_get_all():
        nonlocal poll_count, event_state_after_wake
        poll_count += 1
        if poll_count == 2:
            # Check event state at the start of the second poll cycle
            event_state_after_wake = command_event.is_set()
            raise asyncio.CancelledError
        return []

    mock_api.get_all_devices.side_effect = counting_get_all

    # Pre-set the event so the first sleep wakes immediately
    command_event.set()

    with pytest.raises(asyncio.CancelledError):
        await poll_loop(
            mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config,
            {}, {}, {}, command_event,
        )

    assert poll_count == 2
    assert event_state_after_wake is False


async def _first_interval(mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config,
                          command_event, monkeypatch, raw_device) -> float:
    """Run one poll cycle and return the timeout it waits with."""
    mock_api.get_all_devices.return_value = [raw_device]
    mock_api.fetch_property_file.return_value = None
    seen: list[float] = []

    async def fake_wait_for(awaitable, timeout):
        seen.append(timeout)
        awaitable.close()
        raise asyncio.CancelledError

    monkeypatch.setattr("src.main.asyncio.wait_for", fake_wait_for)
    with pytest.raises(asyncio.CancelledError):
        await poll_loop(
            mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config,
            {}, {}, {}, command_event,
        )
    return seen[0]


@pytest.mark.asyncio
async def test_job_on_dock_polls_fast(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event, monkeypatch,
):
    """A job back on the dock shows docked but still needs fast polling."""
    assert SharkVacuum.from_skegox(ur2850_raw("recharge_mid_job")).ha_state == "docked"
    interval = await _first_interval(
        mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
        monkeypatch, ur2850_raw("recharge_mid_job"),
    )
    assert interval == mock_config.poll_interval_active


@pytest.mark.asyncio
async def test_job_with_unreadable_sys_st_polls_fast(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event, monkeypatch,
):
    """Only the job is readable: state falls back to docked, polling is fast."""
    raw = ur2850_raw("stale_scheduled_run", Operating_Mode_Ex=None)
    assert SharkVacuum.from_skegox(raw).ha_state == "docked"
    interval = await _first_interval(
        mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
        monkeypatch, raw,
    )
    assert interval == mock_config.poll_interval_active


@pytest.mark.asyncio
async def test_idle_docked_polls_slow(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event, monkeypatch,
):
    interval = await _first_interval(
        mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
        monkeypatch, ur2850_raw("docked_idle"),
    )
    assert interval == mock_config.poll_interval


@pytest.mark.asyncio
async def test_legacy_docked_polls_slow(
    mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event, monkeypatch,
):
    interval = await _first_interval(
        mock_api, mock_ayla_api, mock_mqtt, mock_auth, mock_config, command_event,
        monkeypatch, make_skegox_device(operating_mode=0),
    )
    assert interval == mock_config.poll_interval

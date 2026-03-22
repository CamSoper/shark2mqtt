"""Tests for poll_loop command_event wake-up behavior."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.main import poll_loop

from .conftest import make_skegox_device


@pytest.mark.asyncio
async def test_poll_wakes_early_on_command_event(
    mock_api, mock_mqtt, mock_auth, mock_config, command_event,
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
        mock_api, mock_mqtt, mock_auth, mock_config,
        {}, {}, command_event,
    ))
    await set_event_soon()
    # Give the loop a moment to wake and run the second poll
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start

    assert poll_count == 2
    # Should complete well under the 10s poll interval
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_poll_waits_full_interval_without_event(
    mock_api, mock_mqtt, mock_auth, mock_config, command_event,
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
            mock_api, mock_mqtt, mock_auth, mock_config,
            {}, {}, command_event,
        )
    elapsed = time.monotonic() - start

    assert poll_count == 2
    # Should have waited roughly the full interval
    assert elapsed >= 0.25


@pytest.mark.asyncio
async def test_event_cleared_after_wake(
    mock_api, mock_mqtt, mock_auth, mock_config, command_event,
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
            mock_api, mock_mqtt, mock_auth, mock_config,
            {}, {}, command_event,
        )

    assert poll_count == 2
    assert event_state_after_wake is False

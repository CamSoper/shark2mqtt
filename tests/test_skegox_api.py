from unittest.mock import AsyncMock

import pytest

from src.skegox_api import SkegoxApi


@pytest.mark.asyncio
async def test_visual_floor_fetch_bypasses_wrapper_cache():
    api = SkegoxApi.__new__(SkegoxApi)
    api._household_id = "household"
    api._request = AsyncMock(return_value={"files": [{"presignedUrl": "https://example/map"}]})

    result = await api.fetch_property_file("device", "Visual_Floor_1", cache_bust=True)
    await api.fetch_property_file("device", "Visual_Floor_1", cache_bust=True)

    assert result is None
    first_path = api._request.await_args_list[0].args[1]
    second_path = api._request.await_args_list[1].args[1]
    assert "properties=Visual_Floor_1&cacheBust=" in first_path
    assert first_path != second_path
    for call in api._request.await_args_list:
        assert call.kwargs["headers"] == {
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
        }
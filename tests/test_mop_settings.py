"""Tests for MOP_ROBOTS / NO_MOP_ROBOTS and the wet/dry latch."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.main import _apply_mop_settings
from src.shark_device import SharkVacuum

from .conftest import make_skegox_device


def _device(name: str = "Rodimus Prime", dsn: str = "DSN1", params: str | None = None) -> SharkVacuum:
    vac = SharkVacuum.from_skegox(make_skegox_device(dsn=dsn, name=name))
    if params is not None:
        vac._properties["GET_CleaningParameters"] = params
    return vac


def _config(mop: str = "", no_mop: str = "") -> MagicMock:
    config = MagicMock()
    config.mop_robots = mop
    config.no_mop_robots = no_mop
    return config


WET_DRY = '{"CleanStage":2,"Deep":0,"Wet":0,"Dry":1}'


def test_no_mop_matches_name_case_insensitively():
    vac = _device(params=WET_DRY)
    _apply_mop_settings(vac, _config(no_mop="rodimus prime, Starscream"), set())
    assert vac.mop_override is False
    assert vac.has_wet_dry is False


def test_mop_matches_dsn():
    vac = _device(dsn="AC123")
    _apply_mop_settings(vac, _config(mop="ac123"), set())
    assert vac.has_wet_dry is True


def test_no_override_leaves_auto_detection():
    vac = _device(params="")
    _apply_mop_settings(vac, _config(), set())
    assert vac.mop_override is None
    assert vac.has_wet_dry is False


def test_latch_carries_across_polls():
    seen: set[str] = set()
    first = _device(params=WET_DRY)
    _apply_mop_settings(first, _config(), seen)
    assert seen == {"DSN1"}
    later = _device(params="")  # value cleared between runs
    _apply_mop_settings(later, _config(), seen)
    assert later.has_wet_dry is True


def test_overridden_robot_never_latches():
    seen: set[str] = set()
    vac = _device(params=WET_DRY)
    _apply_mop_settings(vac, _config(no_mop="Rodimus Prime"), seen)
    assert seen == set()

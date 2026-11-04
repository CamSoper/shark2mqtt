"""Tests for sys_st / job parsing and job-aware state (UR2850ZEUS shape)."""

from __future__ import annotations

import pytest

from .conftest import make_skegox_device
from .ur2850_fixtures import DRY_JOB, make_ur2850
from src.shark_device import SharkVacuum


class TestSysStParsing:
    def test_from_json_string(self):
        assert make_ur2850("cleaning_dry").sys_st == 6

    def test_from_dict(self):
        vac = make_ur2850("docked_idle", Operating_Mode_Ex={"operating_mode": 3, "sys_st": 13})
        assert vac.sys_st == 13

    @pytest.mark.parametrize(
        "raw",
        ["", "not json", "[1, 2]", '{"operating_mode": 3}', '{"sys_st": "6"}',
         '{"sys_st": true}', 6, None],
    )
    def test_unreadable_is_none(self, raw):
        vac = make_ur2850("docked_idle", Operating_Mode_Ex=raw)
        assert vac.sys_st is None

    def test_absent_on_other_models(self):
        assert SharkVacuum.from_skegox(make_skegox_device()).sys_st is None


class TestJobParsing:
    def test_star_means_no_job(self):
        vac = make_ur2850("docked_idle")
        assert vac.job_active is False
        assert vac.job_clean_type is None
        assert vac.job_trigger is None

    def test_dry_scheduled_job(self):
        vac = make_ur2850("cleaning_dry")
        assert vac.job_active is True
        assert vac.job_clean_type == "dry"
        assert vac.job_trigger == "schedule"

    def test_wet_job_without_trigger(self):
        vac = make_ur2850("cleaning_wet")
        assert vac.job_active is True
        assert vac.job_clean_type == "wet"
        assert vac.job_trigger is None

    def test_job_as_dict(self):
        vac = make_ur2850("cleaning_dry", AreasToClean_V3={"cleantype": "dry"})
        assert vac.job_active is True
        assert vac.job_clean_type == "dry"

    @pytest.mark.parametrize("raw", ["", "garbage", "[]", 0, None])
    def test_unreadable_is_none(self, raw):
        vac = make_ur2850("cleaning_dry", AreasToClean_V3=raw)
        assert vac.job_active is None
        assert vac.job_clean_type is None

    def test_absent_on_other_models(self):
        assert SharkVacuum.from_skegox(make_skegox_device()).job_active is None


class TestUsesJobState:
    def test_both_fields_readable(self):
        assert make_ur2850("docked_idle").uses_job_state

    def test_needs_sys_st(self):
        assert not make_ur2850("cleaning_dry", Operating_Mode_Ex=None).uses_job_state

    def test_needs_job(self):
        assert not make_ur2850("cleaning_dry", AreasToClean_V3=None).uses_job_state

    def test_legacy_model(self):
        assert not SharkVacuum.from_skegox(make_skegox_device()).uses_job_state


class TestJobAttributes:
    def test_attributes_while_cleaning(self):
        attrs = make_ur2850("cleaning_dry").to_attributes_payload()
        assert attrs["sys_st"] == 6
        assert attrs["job_active"] is True
        assert attrs["job_clean_type"] == "dry"
        assert attrs["job_trigger"] == "schedule"

    def test_attributes_with_no_job(self):
        attrs = make_ur2850("docked_idle").to_attributes_payload()
        assert attrs["sys_st"] == 13
        assert attrs["job_active"] is False
        assert attrs["job_clean_type"] is None
        assert attrs["job_trigger"] is None

    def test_legacy_model_attribute_keys_unchanged(self):
        attrs = SharkVacuum.from_skegox(make_skegox_device()).to_attributes_payload()
        for key in ("sys_st", "job_active", "job_clean_type", "job_trigger"):
            assert key not in attrs

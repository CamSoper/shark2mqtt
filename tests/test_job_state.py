"""Tests for sys_st / job parsing and job-aware state (UR2850ZEUS shape)."""

from __future__ import annotations

import pytest

from .conftest import make_skegox_device
from .ur2850_fixtures import make_ur2850
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


# Expected HA state per sample for a model reporting both job fields.
JOB_AWARE_STATES = {
    "docked_idle": "docked",
    "cleaning_dry": "cleaning",
    "cleaning_wet": "cleaning",
    "paused": "paused",  # today: idle
    "returning": "returning",
    "dock_activity_no_job": "docked",  # today: returning (DockedStatus lagged)
    "dock_activity_with_job": "cleaning",  # today: cleaning only by luck of mode 7
    "error": "error",
    "recharge_mid_job": "docked",
    "stale_scheduled_run": "cleaning",  # today: docked for the whole run
}


class TestJobAwareState:
    @pytest.mark.parametrize("sample,expected", sorted(JOB_AWARE_STATES.items()))
    def test_sample_states(self, sample, expected):
        assert make_ur2850(sample).ha_state == expected

    def test_dock_activity_after_run_is_docked(self):
        # Real 12:38:27 sample: on the dock (sys_st 32) with DockedStatus and
        # Charging_Status still False. v1.5.5 showed "returning" here.
        vac = make_ur2850("dock_activity_no_job")
        assert vac.is_docked is False
        assert vac.ha_state == "docked"

    def test_stale_scheduled_run_is_cleaning(self):
        # Cloud said docked + charging + RETURN + MissionComplete for the whole
        # run; only the job was live.
        vac = make_ur2850("stale_scheduled_run")
        assert vac.is_docked and vac.operating_mode.name == "RETURN"
        assert vac.ha_state == "cleaning"

    def test_paused_job_in_other_sys_st_is_not_paused(self):
        assert make_ur2850("paused", Operating_Mode_Ex='{"sys_st":6}').ha_state == "cleaning"

    def test_recharging_to_resume_signal_means_docked(self):
        vac = make_ur2850("cleaning_dry", Operating_Mode_Ex='{"sys_st":32}', RechargingToResume=True)
        assert vac.ha_state == "docked"
        assert vac.job_active is True

    def test_returning_with_job(self):
        assert make_ur2850("cleaning_dry", Operating_Mode_Ex='{"sys_st":7}').ha_state == "returning"

    def test_error_wins_over_job(self):
        assert make_ur2850("cleaning_dry", Error_Code=9).ha_state == "error"

    def test_sys_st_5_without_error_code_falls_back(self):
        # No job, unknown-to-the-table sys_st: today's mapping (RETURN, not
        # docked -> returning).
        vac = make_ur2850("returning", Operating_Mode_Ex='{"sys_st":5}')
        assert vac.ha_state == "returning"

    def test_unknown_sys_st_without_job_falls_back(self):
        vac = make_ur2850("docked_idle", Operating_Mode_Ex='{"sys_st":99}')
        assert vac.ha_state == "docked"

    def test_only_one_field_uses_todays_logic(self):
        # Stale run, but sys_st unreadable: v1.5.5 behaviour (docked).
        vac = make_ur2850("stale_scheduled_run", Operating_Mode_Ex=None)
        assert vac.ha_state == "docked"


# ha_state from v1.5.5 (c45fe74) for a robot without Operating_Mode_Ex /
# AreasToClean_V3, generated by running that commit's code. Per operating
# mode (None = absent, 5 = unmapped), states for (docked, charging, error) in
# order: (0,0,0) (0,0,9) (0,1,0) (0,1,9) (1,0,0) (1,0,9) (1,1,0) (1,1,9).
_I, _E, _D = "idle", "error", "docked"
_C, _P, _R = "cleaning", "paused", "returning"
V155_STATES = {
    None: [_I, _E, _I, _E, _I, _E, _I, _E],
    0: [_I, _E, _D, _E, _D, _E, _D, _E],
    1: [_P, _E, _P, _E, _P, _E, _P, _E],
    2: [_C, _E, _C, _E, _C, _E, _C, _E],
    3: [_R, _E, _D, _E, _D, _E, _D, _E],
    4: [_C, _E, _C, _E, _C, _E, _C, _E],
    5: [_I, _E, _I, _E, _I, _E, _I, _E],
    6: [_C, _E, _C, _E, _C, _E, _C, _E],
    7: [_C, _E, _C, _E, _C, _E, _C, _E],
    8: [_C, _E, _C, _E, _C, _E, _C, _E],
}
COMBOS = [(d, c, e) for d in (0, 1) for c in (0, 1) for e in (0, 9)]


@pytest.mark.parametrize("mode", list(V155_STATES))
def test_legacy_robot_states_match_v155(mode):
    for (docked, charging, error), expected in zip(COMBOS, V155_STATES[mode]):
        data = make_skegox_device()
        reported = data["shadow"]["properties"]["reported"]
        if mode is None:
            del reported["Operating_Mode"]
        else:
            reported["Operating_Mode"]["value"] = mode
        reported["DockedStatus"]["value"] = docked
        reported["Charging_Status"]["value"] = charging
        reported["Error_Code"]["value"] = error
        vac = SharkVacuum.from_skegox(data)
        assert vac.ha_state == expected, (mode, docked, charging, error)

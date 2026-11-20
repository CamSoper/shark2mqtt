"""Shadow fixtures for a UR2850ZEUS (vacuum + mop, Skegox backend).

REAL samples are copied from sanitized bridge DEBUG "Shadow values" lines
(2026-11-04 and 2026-11-05), reduced to the properties the state logic reads. Values are
verbatim; room and floor IDs were replaced with placeholders.

SYNTHETIC samples are states missing from the retained logs. They are built
from the same format, with the values observed on the robot at the time.
Each is marked SYNTHETIC.
"""

from __future__ import annotations

import json
from typing import Any

from src.shark_device import SharkVacuum

DRY_JOB = (
    '{"areas_to_clean":{"UserRoom":["ROOM_A","ROOM_B","ROOM_C","ROOM_D","ROOM_E","ROOM_F"]},'
    '"clean_count":1,"floor_id":"FLOOR_ID","trigger":"schedule","cleantype":"dry"}'
)
# REAL 2026-11-05: one-room mop run started from the Shark app. No "trigger"
# key, unlike the scheduled DRY_JOB.
WET_JOB = (
    '{"areas_to_clean":{"UserRoom":["ROOM_D"]},'
    '"clean_count":1,"floor_id":"FLOOR_ID","cleantype":"wet"}'
)


def _ex(mode: int, sys_st: int) -> str:
    return json.dumps({"operating_mode": mode, "sys_st": sys_st}, separators=(",", ":"))


def _sample(
    *,
    mode: int,
    sys_st: int,
    job: str,
    docked: bool,
    charging: bool,
    error: int = 0,
    mission_complete: bool = True,
    recharging_to_resume: bool = False,
    evacuating: bool = False,
    flow_mode: int = 0,
    power_mode: int = 0,
) -> dict[str, Any]:
    return {
        "AreasToClean_V3": job,
        "Charging_Status": charging,
        "DockedStatus": docked,
        "Error_Code": error,
        "Evacuating": evacuating,
        "Flow_Mode": flow_mode,
        "MissionComplete": mission_complete,
        "MopPlateAttached": True,
        "Operating_Mode": mode,
        "Operating_Mode_Ex": _ex(mode, sys_st),
        "Power_Mode": power_mode,
        "RechargingToResume": recharging_to_resume,
        "Robot_Room_List": "FLOOR_ID:ROOM_A:ROOM_B:ROOM_C:ROOM_D",
        "Warning_Code": 8,
    }


SAMPLES: dict[str, dict[str, Any]] = {
    # REAL 2026-11-04 12:43:50. HA showed docked.
    "docked_idle": _sample(mode=3, sys_st=13, job="*", docked=True, charging=True),
    # REAL 2026-11-04 12:37:05, scheduled dry run. HA showed cleaning.
    "cleaning_dry": _sample(mode=6, sys_st=6, job=DRY_JOB, docked=False, charging=False),
    # REAL 2026-11-04 12:37:46. Job already cleared while returning.
    "returning": _sample(mode=3, sys_st=7, job="*", docked=False, charging=False),
    # REAL 2026-11-04 12:38:27. On the dock but DockedStatus still False;
    # HA wrongly showed returning.
    "dock_activity_no_job": _sample(mode=3, sys_st=32, job="*", docked=False, charging=False),
    # REAL 2026-11-05 11:43:27, mop run started from the Shark app. HA showed
    # cleaning. MissionComplete is True mid-run, so it can't mean "finished".
    "cleaning_wet": _sample(
        mode=7, sys_st=6, job=WET_JOB, docked=False, charging=False,
        flow_mode=1, power_mode=2,
    ),
    # REAL 2026-11-05 12:46:21, scheduled dry run. Heading back mid-job to
    # empty the bin (Evacuating already True, job kept).
    "returning_to_empty": _sample(
        mode=3, sys_st=7, job=DRY_JOB, docked=False, charging=False,
        evacuating=True, mission_complete=False, power_mode=2,
    ),
    # REAL 2026-11-05 12:49:48. On the dock emptying mid-job; DockedStatus
    # still False. HA showed cleaning (fixed: docked).
    "emptying_mid_job": _sample(
        mode=3, sys_st=32, job=DRY_JOB, docked=False, charging=False,
        evacuating=True, mission_complete=False, power_mode=2,
    ),
    # REAL 2026-11-05 12:50:29. Error 9 "No dustbin" while emptying.
    "error_while_emptying": _sample(
        mode=0, sys_st=5, job=DRY_JOB, docked=True, charging=False, error=9,
        evacuating=True, mission_complete=False, power_mode=2,
    ),
    # REAL 2026-11-05 12:50:50. Error cleared, the robot dropped the job,
    # still dock activity.
    "dock_activity_after_error": _sample(
        mode=3, sys_st=32, job="*", docked=False, charging=False,
        mission_complete=False, power_mode=2,
    ),
    # SYNTHETIC: paused from the app or lifted (mode 0, sys_st 4, job kept).
    "paused": _sample(mode=0, sys_st=4, job=DRY_JOB, docked=False, charging=False),
    # SYNTHETIC: filling on the dock before a mop run (sys_st 32 with a job).
    "dock_activity_with_job": _sample(mode=7, sys_st=32, job=WET_JOB, docked=True, charging=True),
    # SYNTHETIC: error 9 "No dustbin" (sys_st 5).
    "error": _sample(mode=0, sys_st=5, job=DRY_JOB, docked=False, charging=False, error=9),
    # SYNTHETIC: mid-job recharge (job kept, docked, sys_st 13).
    "recharge_mid_job": _sample(mode=3, sys_st=13, job=DRY_JOB, docked=True, charging=True),
    # SYNTHETIC: the stale scheduled run of 2026-10-30. Out cleaning, but the
    # cloud kept reporting docked + charging + RETURN; only the job was live.
    "stale_scheduled_run": _sample(mode=3, sys_st=32, job=DRY_JOB, docked=True, charging=True),
}


def ur2850_raw(state: str, **overrides: Any) -> dict[str, Any]:
    """A named sample wrapped like a Skegox API device. None drops a field."""
    reported = {**SAMPLES[state], **overrides}
    return {
        "deviceId": "SND_TEST",
        "metadata": {"deviceName": "SharkVac"},
        "registry": {
            "Battery_Serial_Num": "BSN-SND_TEST",
            "Device_Model_Number": "UR2850ZEUS",
        },
        "telemetry": {"Battery_Capacity": 100, "RSSI": -74},
        "connectivityStatus": {"connected": True},
        "shadow": {
            "properties": {
                "reported": {
                    k: {"value": v} for k, v in reported.items() if v is not None
                },
            },
        },
    }


def make_ur2850(state: str, **overrides: Any) -> SharkVacuum:
    """Build a SharkVacuum from a named sample."""
    return SharkVacuum.from_skegox(ur2850_raw(state, **overrides))

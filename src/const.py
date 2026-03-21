"""Shark/Ayla/Auth0 constants and enums."""

from dataclasses import dataclass
from enum import IntEnum


@dataclass(frozen=True)
class RegionConfig:
    """Region-specific API configuration."""

    auth0_url: str
    auth0_token_url: str
    auth0_client_id: str
    auth0_redirect_uri: str
    ayla_login_url: str
    ayla_device_url: str
    ayla_app_id: str
    ayla_app_secret: str


REGIONS: dict[str, RegionConfig] = {
    "us": RegionConfig(
        auth0_url="https://login.sharkninja.com",
        auth0_token_url="https://login.sharkninja.com/oauth/token",
        auth0_client_id="wsguxrqm77mq4LtrTrwg8ZJUxmSrexGi",
        auth0_redirect_uri="com.sharkninja.shark://login.sharkninja.com/ios/com.sharkninja.shark/callback",
        ayla_login_url="https://user-sharkue1.aylanetworks.com",
        ayla_device_url="https://ads-sharkue1.aylanetworks.com",
        ayla_app_id="ios_shark_prod-3A-id",
        ayla_app_secret="ios_shark_prod-74tFWGNg34LQCmR0m45SsThqrqs",
    ),
    "eu": RegionConfig(
        auth0_url="https://logineu.sharkninja.com",
        auth0_token_url="https://logineu.sharkninja.com/oauth/token",
        auth0_client_id="rKDx9O18dBrY3eoJMTkRiBZHDvd9Mx1I",
        auth0_redirect_uri="com.sharkninja.shark://logineu.sharkninja.com/ios/com.sharkninja.shark/callback",
        ayla_login_url="https://user-field-eu.aylanetworks.com",
        ayla_device_url="https://ads-eu.aylanetworks.com",
        ayla_app_id="android_shark_prod-lg-id",
        ayla_app_secret="android_shark_prod-xuf9mlHOo0p3Ty5bboFROSyRBlE",
    ),
}

AUTH0_SCOPES = "openid email profile offline_access"
AUTH0_CUSTOM_SCHEME = "com.sharkninja.shark://"


class OperatingMode(IntEnum):
    """Shark vacuum operating modes."""

    STOP = 0
    PAUSE = 1
    START = 2
    RETURN = 3
    EXPLORE = 4
    # Modes 5-6 unknown
    MOP = 7
    VACUUM_AND_MOP = 8


class PowerMode(IntEnum):
    """Suction power levels."""

    ECO = 1
    NORMAL = 2
    MAX = 3


# Map OperatingMode to Home Assistant vacuum state strings
OPERATING_MODE_TO_HA_STATE: dict[OperatingMode, str] = {
    OperatingMode.STOP: "idle",
    OperatingMode.PAUSE: "paused",
    OperatingMode.START: "cleaning",
    OperatingMode.RETURN: "returning",
    OperatingMode.EXPLORE: "cleaning",
    OperatingMode.MOP: "cleaning",
    OperatingMode.VACUUM_AND_MOP: "cleaning",
}

POWER_MODE_NAMES: dict[PowerMode, str] = {
    PowerMode.ECO: "eco",
    PowerMode.NORMAL: "normal",
    PowerMode.MAX: "max",
}

POWER_MODE_BY_NAME: dict[str, PowerMode] = {v: k for k, v in POWER_MODE_NAMES.items()}

# HA command strings to OperatingMode
HA_COMMAND_TO_MODE: dict[str, OperatingMode] = {
    "start": OperatingMode.START,
    "stop": OperatingMode.STOP,
    "pause": OperatingMode.PAUSE,
    "return_to_base": OperatingMode.RETURN,
}

# Ayla device property names
PROP_OPERATING_MODE = "Operating_Mode"
PROP_CHARGING_STATUS = "Charging_Status"
PROP_BATTERY_CAPACITY = "Battery_Capacity"
PROP_ERROR_CODE = "Error_Code"
PROP_EXTENDED_ERROR_CODE = "Extended_Error_Code"
PROP_RSSI = "RSSI"
PROP_POWER_MODE = "Power_Mode"
PROP_DOCKED_STATUS = "DockedStatus"
PROP_FIND_DEVICE = "Find_Device"
PROP_EXEC_COMMAND = "Exec_Command"
PROP_ROBOT_ROOM_LIST = "Robot_Room_List"
PROP_ROOM_DEFINITION = "Room_Definition"
PROP_DEVICE_MODEL_NUMBER = "Device_Model_Number"
PROP_ROBOT_FIRMWARE_VERSION = "Robot_Firmware_Version"

# Error code descriptions (common ones)
ERROR_CODES: dict[int, str] = {
    0: "No error",
    1: "Side brush stuck",
    2: "Main brush stuck",
    3: "Left wheel stuck",
    4: "Right wheel stuck",
    5: "Cliff sensor error",
    6: "Bumper stuck",
    7: "Dust bin missing",
    8: "Low battery",
    9: "Charging error",
}

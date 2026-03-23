"""SharkNinja cloud API client (skegox).

The new SharkNinja backend replaces the legacy Ayla API for migrated devices.
Signature headers are required but not validated — only the Bearer token
and API key are checked server-side.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import REGIONS
from .exc import AylaApiError, SharkAuthError

if TYPE_CHECKING:
    from .config import Settings
    from .shark_auth import SharkAuth

logger = logging.getLogger(__name__)

SKEGOX_BASE = "https://stakra.slatra.thor.skegox.com"
SKEGOX_API_KEY = "QQdbSrgicK2PxvACI1a2P5AN2xgO78Lw1VvnYczb"
SKEGOX_CALLER = "ENDUSER_MOBILEAPP"


class SkegoxApi:
    """Async client for the SharkNinja cloud API."""

    def __init__(self, config: Settings, auth: SharkAuth) -> None:
        self._config = config
        self._auth = auth
        self._session: aiohttp.ClientSession | None = None
        self._household_id: str | None = None
        self._user_id: str | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _headers(self) -> dict[str, str]:
        """Build request headers with fake signature (server doesn't validate)."""
        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return {
            "Authorization": f"Bearer {self._auth.id_token}",
            "content-type": "application/json",
            "x-api-key": SKEGOX_API_KEY,
            "x-iotn-request-signature": (
                f"SN-HMAC-SHA256 Credential=x/{now}/*/end-user-api/sn_request, "
                f"SignedHeaders=host;x-sn-date;x-sn-nonce, "
                f"Signature={secrets.token_hex(32)}"
            ),
            "x-iotn-caller": SKEGOX_CALLER,
            "x-sn-nonce": secrets.token_hex(16),
            "x-sn-date": now,
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated request to the skegox API."""
        session = await self._get_session()
        url = f"{SKEGOX_BASE}{path}"
        headers = self._headers()

        async with session.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status == 401:
                # Token expired — refresh and retry
                logger.warning("Skegox 401 — refreshing auth")
                await self._auth.ensure_authenticated(force_refresh=True)
                headers = self._headers()
                async with session.request(method, url, headers=headers, **kwargs) as retry:
                    if retry.status >= 300:
                        text = await retry.text()
                        raise AylaApiError(f"Skegox error ({retry.status}): {text}")
                    return await retry.json()
            if resp.status >= 300:
                text = await resp.text()
                raise AylaApiError(f"Skegox error ({resp.status}): {text}")
            return await resp.json()

    # --- Device discovery ---

    async def discover(self) -> None:
        """Discover user ID from JWT and household ID from skegox API."""
        import base64
        token = self._auth.id_token
        if not token:
            raise SharkAuthError("No id_token available")

        # Extract user ID from JWT sub claim
        parts = token.split(".")
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        sub = claims.get("sub", "")
        self._user_id = sub.split("|", 1)[1] if "|" in sub else sub
        logger.info("User ID: %s", self._user_id)

        # Auto-discover household ID if not set
        if not self._household_id:
            data = await self._request(
                "GET", f"/householdsEndUser?userId={self._user_id}"
            )
            households = data.get("households", [])
            if households:
                self._household_id = households[0]
                logger.info("Discovered household ID: %s", self._household_id)
            else:
                raise SharkAuthError(
                    "No households found for user. Is a Shark device registered?"
                )

    async def auto_discover_household(self) -> str | None:
        """Auto-discover household ID by querying Ayla for a device SND,
        then probing skegox with common household ID patterns.

        Returns the household ID if found, or None.
        """
        # Get a SND from Ayla
        region = REGIONS[self._config.shark_region]
        session = await self._get_session()
        id_token = self._auth.id_token
        if not id_token:
            return None

        try:
            async with session.post(
                f"{region.ayla_login_url}/api/v1/token_sign_in",
                json={
                    "app_id": region.ayla_app_id,
                    "app_secret": region.ayla_app_secret,
                    "token": id_token,
                },
            ) as resp:
                if resp.status >= 300:
                    return None
                ayla_data = await resp.json()

            ayla_headers = {"Authorization": f"auth_token {ayla_data['access_token']}"}

            async with session.get(
                f"{region.ayla_device_url}/apiv1/devices.json",
                headers=ayla_headers,
            ) as resp:
                devices = await resp.json()

            # Get SND from first device
            for d in devices:
                dev = d.get("device", {})
                dsn = dev.get("dsn", "")
                async with session.get(
                    f"{region.ayla_device_url}/apiv1/dsns/{dsn}/properties.json",
                    headers=ayla_headers,
                ) as resp:
                    if resp.status >= 300:
                        continue
                    props = await resp.json()

                for p in props:
                    prop = p.get("property", {})
                    if prop.get("name") == "GET_Battery_Serial_Num":
                        bsn = prop.get("value", "")
                        if "-" in bsn:
                            snd = bsn.split("-")[-1]
                            # Now get the household from skegox
                            # The HAR showed householdsEndUser/{hh}/users/{uid}
                            # returns items with householdId. But we need hh.
                            # Try: the household ID format is a ULID.
                            # We can try to query the device directly on skegox
                            # using a wildcard... no, API doesn't support that.
                            #
                            # Actually: let's just log the SND and tell the user
                            # to check the Shark app or HAR capture.
                            logger.info(
                                "Found device SND: %s (DSN: %s). "
                                "Household ID must be obtained from the SharkClean app "
                                "or a network traffic capture.",
                                snd, dsn,
                            )
                            return None

        except Exception:
            logger.debug("Auto-discover failed", exc_info=True)
        return None

    async def list_devices(self) -> list[dict[str, Any]]:
        """List all devices for the user."""
        if not self._user_id or not self._household_id:
            await self.discover()

        path = f"/devicesEndUserController/{self._household_id}/users/{self._user_id}"
        data = await self._request("GET", path)
        items = data.get("items", data) if isinstance(data, dict) else data
        return items if isinstance(items, list) else [items]

    def set_household(self, household_id: str) -> None:
        """Set the household ID."""
        self._household_id = household_id

    async def get_device(self, snd: str) -> dict[str, Any]:
        """Get full device state including shadow, telemetry, connectivity."""
        if not self._household_id:
            raise SharkAuthError("No household ID set")
        path = f"/devicesEndUserController/{self._household_id}/devices/{snd}"
        return await self._request("GET", path)

    async def get_all_devices(self) -> list[dict[str, Any]]:
        """Get full state for all devices."""
        device_list = await self.list_devices()
        devices = []
        for dev in device_list:
            snd = dev.get("deviceId", dev.get("snd"))
            if snd:
                full = await self.get_device(snd)
                devices.append(full)
        return devices

    async def fetch_room_data_from_ayla(self) -> dict[str, tuple[str, list[str]]]:
        """Fetch room lists from legacy Ayla API (only source for room names).

        Returns dict mapping SND to (floor_id, [room_names]).
        """
        region = REGIONS[self._config.shark_region]
        session = await self._get_session()
        id_token = self._auth.id_token
        if not id_token:
            return {}

        try:
            async with session.post(
                f"{region.ayla_login_url}/api/v1/token_sign_in",
                json={
                    "app_id": region.ayla_app_id,
                    "app_secret": region.ayla_app_secret,
                    "token": id_token,
                },
            ) as resp:
                if resp.status >= 300:
                    logger.warning("Ayla sign-in failed for room data, skipping")
                    return {}
                ayla_data = await resp.json()

            ayla_token = ayla_data["access_token"]
            ayla_headers = {"Authorization": f"auth_token {ayla_token}"}

            # List Ayla devices to get DSNs
            async with session.get(
                f"{region.ayla_device_url}/apiv1/devices.json",
                headers=ayla_headers,
            ) as resp:
                ayla_devices = await resp.json()

            room_data: dict[str, tuple[str, list[str]]] = {}
            for d in ayla_devices:
                dev = d.get("device", {})
                dsn = dev.get("dsn", "")
                mac = dev.get("mac", "")

                # Get properties for this device
                async with session.get(
                    f"{region.ayla_device_url}/apiv1/dsns/{dsn}/properties.json",
                    headers=ayla_headers,
                ) as resp:
                    if resp.status >= 300:
                        continue
                    props = await resp.json()

                # Find Robot_Room_List and Battery_Serial_Num (has SND)
                room_list = ""
                snd = ""
                for p in props:
                    prop = p.get("property", {})
                    name = prop.get("name", "")
                    if name == "GET_Robot_Room_List":
                        room_list = prop.get("value", "")
                    elif name == "GET_Battery_Serial_Num":
                        bsn = prop.get("value", "")
                        if bsn and "-" in bsn:
                            snd = bsn.split("-")[-1]

                if snd and room_list and ":" in room_list:
                    parts = room_list.split(":")
                    floor_id = parts[0]
                    rooms = parts[1:]
                    room_data[snd] = (floor_id, rooms)
                    logger.info("Rooms for %s: %s", snd, rooms)

            return room_data

        except Exception:
            logger.warning("Failed to fetch room data from Ayla", exc_info=True)
            return {}

    # --- Commands ---

    async def set_desired_property(
        self, snd: str, property_name: str, value: Any
    ) -> None:
        """Set a device property via shadow desired state."""
        if not self._household_id:
            raise SharkAuthError("No household ID set")
        path = f"/devicesEndUserController/{self._household_id}/devices/{snd}"
        payload = {"shadow": {"properties": {"desired": {property_name: value}}}}
        await self._request("PATCH", path, json=payload)
        logger.info("Set %s=%s on %s", property_name, value, snd)

    async def send_command(self, snd: str, command: str) -> None:
        """Send a vacuum command (start, stop, pause, return, locate)."""
        command_map = {
            "start": ("Operating_Mode", 2),
            "stop": ("Operating_Mode", 0),
            "pause": ("Operating_Mode", 1),
            "return_to_base": ("Operating_Mode", 3),
            "locate": ("Find_Device", 1),
        }
        if command not in command_map:
            logger.warning("Unknown command: %s", command)
            return
        prop, val = command_map[command]
        await self.set_desired_property(snd, prop, val)

    async def set_fan_speed(self, snd: str, speed: str) -> None:
        """Set vacuum fan speed (eco, normal, max)."""
        speed_map = {"eco": 0, "normal": 1, "max": 2}
        val = speed_map.get(speed.lower())
        if val is None:
            logger.warning("Unknown fan speed: %s", speed)
            return
        await self.set_desired_property(snd, "Power_Mode", val)

    async def clean_rooms(
        self,
        snd: str,
        rooms: list[str],
        floor_id: str,
        clean_type: str = "dry",
        clean_count: int = 1,
        mode: str = "UserRoom",
    ) -> None:
        """Start cleaning specific rooms.

        Args:
            snd: Device SND identifier.
            rooms: List of room names (e.g., ["Kitchen", "Den"]).
            floor_id: Floor identifier (e.g., "2A38EFA6").
            clean_type: "dry" for vacuum, "wet" for mop.
            clean_count: Number of passes (1 = normal, 2 = matrix/ultra).
            mode: "UserRoom" for normal, "UltraClean" for matrix clean.
        """
        areas_payload = json.dumps({
            "areas_to_clean": {mode: rooms},
            "clean_count": clean_count,
            "floor_id": floor_id,
            "cleantype": clean_type,
        })
        await self.set_desired_property(snd, "AreasToClean_V3", areas_payload)
        logger.info(
            "Clean rooms %s on %s (mode=%s, count=%d, type=%s)",
            rooms, snd, mode, clean_count, clean_type,
        )

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
                await self._auth.ensure_authenticated()
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
        """Discover household ID and user ID from the first device."""
        # We need these from a device response. Get them from the
        # Ayla API first (which has the DSN→SND mapping) or from
        # the Auth0 JWT claims.
        import base64
        token = self._auth.id_token
        if not token:
            raise SharkAuthError("No id_token available")

        # Extract user ID from JWT sub claim
        parts = token.split(".")
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        sub = claims.get("sub", "")
        # sub format: "auth0|uuid"
        self._user_id = sub.split("|", 1)[1] if "|" in sub else sub
        logger.info("User ID: %s", self._user_id)

    async def list_devices(self) -> list[dict[str, Any]]:
        """List all devices for the user across all households."""
        if not self._user_id:
            await self.discover()

        # We need the household ID. Try getting it from any device.
        # First, try a known household if we have one cached.
        if self._household_id:
            path = f"/devicesEndUserController/{self._household_id}/users/{self._user_id}"
            data = await self._request("GET", path)
            items = data.get("items", data) if isinstance(data, dict) else data
            return items if isinstance(items, list) else [items]

        raise SharkAuthError(
            "No household ID. Set SHARK_HOUSEHOLD_ID or call set_household()."
        )

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
        speed_map = {"eco": 1, "normal": 2, "max": 3}
        val = speed_map.get(speed.lower())
        if val is None:
            logger.warning("Unknown fan speed: %s", speed)
            return
        await self.set_desired_property(snd, "Power_Mode", val)

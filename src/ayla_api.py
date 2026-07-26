"""Ayla IoT API client for Shark devices.

Adapted from the sharkiq library and TheOneOgre's fork.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

import aiohttp
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .const import (
    POWER_MODE_BY_NAME,
    PROP_SET_FIND_DEVICE,
    PROP_SET_FLOW_MODE,
    PROP_SET_OPERATING_MODE,
    PROP_SET_POWER_MODE,
    REGIONS,
    OperatingMode,
    RegionConfig,
)
from .exc import AylaApiError, SharkAuthError
from .shark_device import SharkVacuum

if TYPE_CHECKING:
    from .config import Settings
    from .shark_auth import SharkAuth

logger = logging.getLogger(__name__)

# Ayla auth header format
_AUTH_HEADER = "auth_token {}"

# Refresh Ayla tokens 5 minutes before expiry
_REFRESH_BUFFER = timedelta(minutes=5)


class MardData(NamedTuple):
    """Parsed Mobile_App_Room_Definition data."""

    name_map: dict[str, str]
    rooms: list[str]
    floor_id: str | None


_EMPTY_MARD = MardData({}, [], None)


def parse_mard(
    body: bytes, product_name: str, dsn: str, source: str = "MARD",
) -> MardData:
    """Parse a MARD file body into name_map, rooms, and floor_id.

    Display name = user_room_name when non-empty, else robot_room_name.
    On accounts where robot_room_name already holds display names the
    map is identity; on accounts with AZ_N placeholders it rewrites
    through user_room_name.

    `source` tags info/debug log lines so the caller can distinguish
    Ayla MARD from Skegox MARD. See issue #4.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.debug("%s for %s: not valid JSON", source, product_name)
        return _EMPTY_MARD

    name_map: dict[str, str] = {}
    rooms: list[str] = []
    for area in parsed.get("areas", []):
        meta = area.get("area_meta_data", "")
        if not meta.startswith("UserRoom:"):
            continue
        robot_name = area.get("robot_room_name", "") or ""
        user_name = area.get("user_room_name", "") or ""
        if not robot_name:
            continue
        display_name = user_name or robot_name
        name_map[robot_name] = display_name
        rooms.append(display_name)

    mard_floor_id = parsed.get("floor_id")
    if not (isinstance(mard_floor_id, str) and mard_floor_id):
        mard_floor_id = None

    non_identity = {k: v for k, v in name_map.items() if k != v}
    if non_identity:
        logger.info(
            "%s name map for %s (%s) contains rewrites: %s",
            source, product_name, dsn, non_identity,
        )
    else:
        logger.debug(
            "%s name map for %s (%s) is identity (%d entries)",
            source, product_name, dsn, len(name_map),
        )

    if rooms:
        logger.info(
            "%s rooms for %s (%s): %s",
            source, product_name, dsn, rooms,
        )

    return MardData(name_map, rooms, mard_floor_id)


def debug_dump_mard_structure(
    body: bytes, label: str, source: str = "MARD",
) -> None:
    """DEBUG helper: structural summary of a MARD file.

    The MARD file can be hundreds of KB and a raw text dump is unwieldy.
    This emits a compact structural view (top-level keys, area prefixes,
    and one line per area sans coordinate data) so investigations like
    issue #4 stay readable. `source` tags the log line so MARD from
    different origins (Ayla vs Skegox) is distinguishable.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.debug("%s structural dump for %s: not valid JSON", source, label)
        return
    if not isinstance(parsed, dict):
        logger.debug(
            "%s structural dump for %s: top-level is %s, not dict",
            source, label, type(parsed).__name__,
        )
        return

    logger.debug(
        "%s top-level keys for %s: %s",
        source, label, sorted(parsed.keys()),
    )
    floor_id = parsed.get("floor_id")
    if floor_id:
        logger.debug(
            "%s top-level floor_id for %s: %s",
            source, label, floor_id,
        )
    for key, value in parsed.items():
        if key in ("areas", "floor_id"):
            continue
        if isinstance(value, (list, dict)):
            logger.debug(
                "%s top-level %s for %s: %s (len=%d)",
                source, key, label,
                type(value).__name__, len(value),
            )

    areas = parsed.get("areas")
    if not isinstance(areas, list):
        logger.debug("%s areas for %s: missing or not a list", source, label)
        return

    prefix_counts: dict[str, int] = {}
    for area in areas:
        if not isinstance(area, dict):
            continue
        meta = area.get("area_meta_data", "") or ""
        prefix = meta.split(":", 1)[0] if ":" in meta else meta or "(none)"
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
    logger.debug(
        "%s areas for %s: total=%d, meta_data prefixes=%s",
        source, label, len(areas), prefix_counts,
    )

    for area in areas:
        if not isinstance(area, dict):
            continue
        logger.debug(
            "%s area for %s: meta=%r robot=%r user=%r uuid=%r",
            source, label,
            area.get("area_meta_data"),
            area.get("robot_room_name"),
            area.get("user_room_name"),
            area.get("uuid"),
        )


class AylaApi:
    """Async client for the Ayla Networks IoT API."""

    def __init__(self, config: Settings, auth: SharkAuth) -> None:
        self._region: RegionConfig = REGIONS[config.shark_region]
        self._auth = auth
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: datetime | None = None
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @property
    def token_expiring_soon(self) -> bool:
        if not self._token_expiry:
            return True
        return datetime.now(timezone.utc) >= self._token_expiry - _REFRESH_BUFFER

    # --- Authentication ---

    async def sign_in(self, id_token: str) -> None:
        """Exchange an Auth0 id_token for Ayla access credentials.

        POST {login_url}/api/v1/token_sign_in
        """
        url = f"{self._region.ayla_login_url}/api/v1/token_sign_in"
        payload = {
            "app_id": self._region.ayla_app_id,
            "app_secret": self._region.ayla_app_secret,
            "token": id_token,
        }

        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status >= 300:
                text = await resp.text()
                raise SharkAuthError(
                    f"Ayla token_sign_in failed ({resp.status}): {text}"
                )
            data = await resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        # Ayla tokens typically expire in 1 hour
        expires_in = data.get("expires_in", 3600)
        self._token_expiry = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        # Persist to auth manager for restart survival
        self._auth.update_ayla_tokens(
            self._access_token, self._refresh_token, self._token_expiry
        )
        logger.info("Ayla sign-in successful, token expires in %ds", expires_in)

    async def refresh_auth(self) -> None:
        """Refresh the Ayla access token.

        POST {login_url}/users/refresh_token.json
        """
        token = self._refresh_token or self._auth.ayla_refresh_token
        if not token:
            raise SharkAuthError("No Ayla refresh token available")

        url = f"{self._region.ayla_login_url}/users/refresh_token.json"
        payload = {"user": {"refresh_token": token}}

        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status >= 300:
                text = await resp.text()
                raise SharkAuthError(
                    f"Ayla refresh failed ({resp.status}): {text}"
                )
            data = await resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        expires_in = data.get("expires_in", 3600)
        self._token_expiry = datetime.now(timezone.utc) + timedelta(
            seconds=expires_in
        )

        self._auth.update_ayla_tokens(
            self._access_token, self._refresh_token, self._token_expiry
        )
        logger.info("Ayla token refreshed, expires in %ds", expires_in)

    async def _ensure_ayla_auth(self) -> None:
        """Ensure we have a valid Ayla access token."""
        # Try loading from auth manager if we don't have one
        if not self._access_token and self._auth.ayla_access_token:
            self._access_token = self._auth.ayla_access_token
            self._refresh_token = self._auth.ayla_refresh_token

        if self.token_expiring_soon:
            try:
                await self.refresh_auth()
            except SharkAuthError:
                # Ayla refresh failed — need full re-auth via Auth0
                logger.warning("Ayla refresh failed, re-authenticating via Auth0")
                id_token = await self._auth.ensure_authenticated()
                try:
                    await self.sign_in(id_token)
                except SharkAuthError:
                    # Stale Auth0 id_token — force refresh and retry
                    logger.warning("Ayla sign-in 401, forcing Auth0 refresh")
                    id_token = await self._auth.ensure_authenticated(force_refresh=True)
                    await self.sign_in(id_token)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": _AUTH_HEADER.format(self._access_token)}

    # --- API requests ---

    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _request(
        self, method: str, url: str, **kwargs: Any
    ) -> Any:
        """Make an authenticated Ayla API request with retry."""
        await self._ensure_ayla_auth()
        session = await self._get_session()

        async with session.request(
            method, url, headers=self._headers, **kwargs
        ) as resp:
            if resp.status == 401:
                # Token expired mid-request — refresh and retry once
                logger.warning("Ayla 401 — refreshing token")
                await self.refresh_auth()
                async with session.request(
                    method, url, headers=self._headers, **kwargs
                ) as retry_resp:
                    if retry_resp.status >= 300:
                        text = await retry_resp.text()
                        raise AylaApiError(
                            f"Ayla API error ({retry_resp.status}): {text}"
                        )
                    return await retry_resp.json()

            if resp.status >= 300:
                text = await resp.text()
                raise AylaApiError(f"Ayla API error ({resp.status}): {text}")
            return await resp.json()

    # --- Device operations ---

    async def list_devices(self) -> list[dict[str, Any]]:
        """Fetch all devices on the account.

        GET {device_url}/apiv1/devices.json
        """
        url = f"{self._region.ayla_device_url}/apiv1/devices.json"
        data = await self._request("GET", url)
        return [d["device"] for d in data if "device" in d]

    async def get_device_properties(
        self, dsn: str
    ) -> list[dict[str, Any]]:
        """Fetch all properties for a device.

        GET {device_url}/apiv1/dsns/{dsn}/properties.json
        """
        url = f"{self._region.ayla_device_url}/apiv1/dsns/{dsn}/properties.json"
        return await self._request("GET", url)

    async def set_device_property(
        self, dsn: str, name: str, value: Any
    ) -> None:
        """Set a device property value.

        POST {device_url}/apiv1/dsns/{dsn}/properties/{name}/datapoints.json
        """
        url = (
            f"{self._region.ayla_device_url}/apiv1/dsns/{dsn}"
            f"/properties/{name}/datapoints.json"
        )
        payload = {"datapoint": {"value": value}}
        await self._request("POST", url, json=payload)
        logger.info("Set %s=%s on device %s", name, value, dsn)

    async def get_devices(self) -> list[SharkVacuum]:
        """Fetch all devices with their properties."""
        raw_devices = await self.list_devices()
        vacuums = []

        for device_data in raw_devices:
            vacuum = SharkVacuum(device_data)
            try:
                props = await self.get_device_properties(vacuum.dsn)
                vacuum.update_properties(props)

                # Parse room list from GET_Robot_Room_List
                room_list = vacuum._properties.get("GET_Robot_Room_List", "")
                if room_list and isinstance(room_list, str) and ":" in room_list:
                    parts = room_list.split(":")
                    vacuum.floor_id = parts[0]
                    vacuum.rooms = parts[1:]

                # Detect AreasToClean_V3 capability
                prop_names = {
                    p.get("property", {}).get("name") for p in props
                }
                vacuum.has_areas_v3 = "SET_AreasToClean_V3" in prop_names

                # Use MARD as authoritative room source (issue #4).
                # MARD has the complete, correct room list with display
                # names; Robot_Room_List may have phantoms or placeholders.
                mard = await self._fetch_mard(vacuum)
                vacuum.room_name_map = mard.name_map
                if mard.rooms:
                    vacuum.rooms = mard.rooms
                if mard.floor_id:
                    vacuum.floor_id = mard.floor_id

                if logger.isEnabledFor(logging.DEBUG):
                    sorted_names = sorted(prop_names - {None})
                    hint_keywords = ("room", "area", "zone", "map", "floor")
                    hint_props = {
                        k: vacuum._properties.get(k)
                        for k in sorted_names
                        if any(kw in k.lower() for kw in hint_keywords)
                    }
                    logger.debug(
                        "Ayla shadow dump for %s (%s): "
                        "GET_Robot_Room_List=%r, parsed_floor_id=%r, parsed_rooms=%r",
                        vacuum.product_name, vacuum.dsn,
                        vacuum._properties.get("GET_Robot_Room_List"),
                        vacuum.floor_id, vacuum.rooms,
                    )
                    logger.debug(
                        "Ayla property names for %s: %s",
                        vacuum.product_name, sorted_names,
                    )
                    logger.debug(
                        "Ayla room/area/zone/map/floor properties for %s: %s",
                        vacuum.product_name, hint_props,
                    )
                    await self._debug_dump_file_datapoints(vacuum)
            except AylaApiError:
                logger.warning(
                    "Failed to fetch properties for %s", vacuum.dsn
                )
            vacuums.append(vacuum)

        logger.info("Fetched %d Ayla device(s)", len(vacuums))
        return vacuums

    async def _fetch_file_datapoint(self, dp_url: str) -> bytes | None:
        """Resolve an Ayla file-type datapoint URL to its actual file bytes.

        Ayla `base_type: file` properties expose a datapoint URL in the
        property value; fetching that returns JSON with a `file` field
        pointing to the real content. Returns None on any failure.
        """
        if not isinstance(dp_url, str) or not dp_url.startswith("http"):
            return None
        try:
            dp = await self._request("GET", dp_url)
            file_url = dp.get("datapoint", {}).get("file")
            if not file_url:
                return None
            session = await self._get_session()
            async with session.get(file_url) as resp:
                return await resp.read()
        except Exception:
            logger.debug("File datapoint fetch failed for %s", dp_url, exc_info=True)
            return None

    async def _fetch_mard(
        self, vacuum: SharkVacuum,
    ) -> MardData:
        """Fetch and parse Ayla Mobile_App_Room_Definition. See issue #4."""
        dp_url = vacuum._properties.get("Mobile_App_Room_Definition")
        body = await self._fetch_file_datapoint(dp_url)
        if not body:
            return _EMPTY_MARD
        return parse_mard(body, vacuum.product_name, vacuum.dsn, source="Ayla MARD")

    async def _debug_dump_file_datapoints(self, vacuum: SharkVacuum) -> None:
        """DEBUG helper: fetch and log selected Ayla file-type datapoints.

        Used to investigate issues like #4 where room names come through
        as placeholder zone IDs. Dumps the file contents for properties
        that are the most likely sources of the real display names.
        """
        candidates = (
            "Mobile_App_Room_Definition",
            "GET_Zones",
            "GET_Persistent_Floor_1",
        )
        for prop in candidates:
            dp_url = vacuum._properties.get(prop)
            if not isinstance(dp_url, str) or not dp_url.startswith("http"):
                logger.debug(
                    "File datapoint %s for %s: not set (%r)",
                    prop, vacuum.product_name, dp_url,
                )
                continue
            body = await self._fetch_file_datapoint(dp_url)
            if body is None:
                logger.debug(
                    "File datapoint %s for %s: fetch failed",
                    prop, vacuum.product_name,
                )
                continue

            if prop == "Mobile_App_Room_Definition":
                self._debug_dump_mard_structure(vacuum, body)

            try:
                text = body.decode("utf-8")
                if text.lstrip().startswith(("{", "[")):
                    try:
                        parsed = json.loads(text)
                        logger.debug(
                            "File datapoint %s for %s (JSON, %d bytes): %s",
                            prop, vacuum.product_name, len(body),
                            json.dumps(parsed)[:4000],
                        )
                        continue
                    except json.JSONDecodeError:
                        pass
                logger.debug(
                    "File datapoint %s for %s (text, %d bytes): %s",
                    prop, vacuum.product_name, len(body), text[:2000],
                )
            except UnicodeDecodeError:
                logger.debug(
                    "File datapoint %s for %s (binary, %d bytes, first 64 hex): %s",
                    prop, vacuum.product_name, len(body), body[:64].hex(),
                )

    def _debug_dump_mard_structure(
        self, vacuum: SharkVacuum, body: bytes,
    ) -> None:
        """DEBUG helper: structural summary of Ayla Mobile_App_Room_Definition."""
        debug_dump_mard_structure(body, vacuum.product_name, source="Ayla MARD")

    # --- Commands ---

    _COMMAND_MAP: dict[str, tuple[str, int]] = {
        "start": (PROP_SET_OPERATING_MODE, OperatingMode.START),
        "stop": (PROP_SET_OPERATING_MODE, OperatingMode.STOP),
        "pause": (PROP_SET_OPERATING_MODE, OperatingMode.PAUSE),
        "return_to_base": (PROP_SET_OPERATING_MODE, OperatingMode.RETURN),
        "locate": (PROP_SET_FIND_DEVICE, 1),
    }

    async def send_command(self, dsn: str, command: str) -> None:
        """Send a command to the vacuum via Ayla property datapoint."""
        entry = self._COMMAND_MAP.get(command)
        if not entry:
            logger.warning("Unknown command: %s", command)
            return
        prop_name, value = entry
        await self.set_device_property(dsn, prop_name, value)

    async def set_fan_speed(self, dsn: str, speed: str) -> None:
        """Set fan speed via Ayla property datapoint."""
        mode = POWER_MODE_BY_NAME.get(speed)
        if mode is None:
            logger.warning("Unknown fan speed: %s", speed)
            return
        await self.set_device_property(dsn, PROP_SET_POWER_MODE, int(mode))

    async def set_water_flow(self, dsn: str, level: str) -> None:
        """Set mop water flow level via Ayla property datapoint.

        Same 0/1/2 scale as Power_Mode; only applies to vac+mop combo
        models with Flow_Mode in their shadow.
        """
        mode = POWER_MODE_BY_NAME.get(level)
        if mode is None:
            logger.warning("Unknown water flow level: %s", level)
            return
        await self.set_device_property(dsn, PROP_SET_FLOW_MODE, int(mode))

    async def clean_rooms(
        self,
        dsn: str,
        rooms: list[str],
        floor_id: str,
        clean_type: str = "dry",
        clean_count: int = 1,
        mode: str = "UserRoom",
        use_v3: bool = False,
    ) -> None:
        """Start cleaning specific rooms via Ayla property datapoints."""
        if use_v3:
            payload = json.dumps({
                "areas_to_clean": {mode: rooms},
                "clean_count": clean_count,
                "floor_id": floor_id,
                "cleantype": clean_type,
            })
            await self.set_device_property(dsn, "SET_AreasToClean_V3", payload)
        else:
            payload = json.dumps({
                "floor_id": floor_id,
                "areas_to_clean": [f"{mode}:{room}" for room in rooms],
                "clean_count": clean_count,
            })
            await self.set_device_property(dsn, "SET_Areas_To_Clean", payload)
            await self.set_device_property(
                dsn, PROP_SET_OPERATING_MODE, OperatingMode.START,
            )
        logger.info(
            "Ayla clean rooms %s on %s (mode=%s, count=%d, v3=%s)",
            rooms, dsn, mode, clean_count, use_v3,
        )

"""Ayla IoT API client for Shark devices.

Adapted from the sharkiq library and TheOneOgre's fork.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .const import REGIONS, RegionConfig
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
            if resp.status != 200:
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
            if resp.status != 200:
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
                    if retry_resp.status != 200:
                        text = await retry_resp.text()
                        raise AylaApiError(
                            f"Ayla API error ({retry_resp.status}): {text}"
                        )
                    return await retry_resp.json()

            if resp.status != 200:
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
            except AylaApiError:
                logger.warning(
                    "Failed to fetch properties for %s", vacuum.dsn
                )
            vacuums.append(vacuum)

        logger.info("Fetched %d device(s)", len(vacuums))
        return vacuums

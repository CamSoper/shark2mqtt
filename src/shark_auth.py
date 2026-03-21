"""Auth0 authentication and token management.

Handles the full auth cascade:
1. Load cached tokens from disk
2. Refresh via Auth0 refresh_token grant (no browser)
3. Headless browser login (added in Phase 8)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from pydantic import BaseModel

from .config import Settings
from .const import AUTH0_CUSTOM_SCHEME, AUTH0_SCOPES, REGIONS, RegionConfig
from .exc import SharkAuthError, SharkAuthLockedError

logger = logging.getLogger(__name__)

TOKEN_FILENAME = "shark2mqtt_tokens.json"

# Circuit breaker limits
MAX_CONSECUTIVE_FAILURES = 2
BACKOFF_SECONDS = 30 * 60  # 30 minutes
MAX_BROWSER_LAUNCHES_PER_DAY = 3


class TokenData(BaseModel):
    """Persisted authentication tokens."""

    auth0_refresh_token: str | None = None
    auth0_id_token: str | None = None
    ayla_access_token: str | None = None
    ayla_refresh_token: str | None = None
    ayla_token_expiry: str | None = None
    saved_at: str | None = None


class SharkAuth:
    """Manages Auth0 authentication lifecycle."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._region: RegionConfig = REGIONS[config.shark_region]
        self._token_path = Path(config.token_dir) / TOKEN_FILENAME
        self._tokens: TokenData | None = None

        # Circuit breaker state
        self._consecutive_failures = 0
        self._backoff_until: float = 0
        self._browser_launches_today: int = 0
        self._browser_launch_day: int = 0  # day of year

    @property
    def id_token(self) -> str | None:
        """Current Auth0 id_token for Ayla sign-in."""
        return self._tokens.auth0_id_token if self._tokens else None

    @property
    def ayla_access_token(self) -> str | None:
        return self._tokens.ayla_access_token if self._tokens else None

    @property
    def ayla_refresh_token(self) -> str | None:
        return self._tokens.ayla_refresh_token if self._tokens else None

    def update_ayla_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expiry: datetime,
    ) -> None:
        """Called by AylaApi after sign-in or refresh to persist Ayla tokens."""
        if not self._tokens:
            self._tokens = TokenData()
        self._tokens.ayla_access_token = access_token
        self._tokens.ayla_refresh_token = refresh_token
        self._tokens.ayla_token_expiry = expiry.isoformat()
        self._save_tokens()

    async def ensure_authenticated(self) -> str:
        """Return a valid Auth0 id_token, refreshing if needed.

        Auth cascade:
        1. Load cached tokens from disk
        2. Try Auth0 refresh_token grant
        3. Launch headless browser (if available)

        Raises SharkAuthError if all methods fail.
        """
        # Check circuit breaker backoff
        if time.monotonic() < self._backoff_until:
            remaining = int(self._backoff_until - time.monotonic())
            raise SharkAuthError(
                f"Auth backoff active, {remaining}s remaining. "
                "Too many recent failures."
            )

        # Step 1: Load cached tokens
        if not self._tokens:
            self._tokens = self._load_tokens()

        # If we have a valid id_token, return it
        if self._tokens and self._tokens.auth0_id_token:
            logger.debug("Using cached Auth0 id_token")
            return self._tokens.auth0_id_token

        # Step 2: Try refresh_token grant
        if self._tokens and self._tokens.auth0_refresh_token:
            try:
                await self._refresh_auth0_token()
                self._consecutive_failures = 0
                return self._tokens.auth0_id_token  # type: ignore[return-value]
            except SharkAuthError:
                logger.warning("Auth0 refresh_token grant failed")

        # Step 3: Try browser auth
        try:
            await self._browser_authenticate()
            self._consecutive_failures = 0
            return self._tokens.auth0_id_token  # type: ignore[return-value]
        except SharkAuthError:
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._backoff_until = time.monotonic() + BACKOFF_SECONDS
                logger.error(
                    "Auth circuit breaker tripped after %d failures. "
                    "Backing off for %d minutes.",
                    self._consecutive_failures,
                    BACKOFF_SECONDS // 60,
                )
            raise

    async def _refresh_auth0_token(self) -> None:
        """Exchange Auth0 refresh_token for a new id_token."""
        if not self._tokens or not self._tokens.auth0_refresh_token:
            raise SharkAuthError("No Auth0 refresh token available")

        payload = {
            "grant_type": "refresh_token",
            "client_id": self._region.auth0_client_id,
            "refresh_token": self._tokens.auth0_refresh_token,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._region.auth0_token_url, json=payload
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    error = data.get("error", "unknown")
                    desc = data.get("error_description", "")
                    if resp.status == 429:
                        raise SharkAuthLockedError(
                            f"Auth0 rate limited: {error} {desc}"
                        )
                    raise SharkAuthError(
                        f"Auth0 refresh failed ({resp.status}): {error} {desc}"
                    )

                self._tokens.auth0_id_token = data["id_token"]
                # Auth0 may rotate the refresh token
                if "refresh_token" in data:
                    self._tokens.auth0_refresh_token = data["refresh_token"]
                self._save_tokens()
                logger.info("Auth0 token refreshed successfully")

    async def _browser_authenticate(self) -> None:
        """Authenticate via headless Chromium browser with PKCE.

        Launches Playwright, navigates to Auth0 login, fills credentials,
        intercepts the custom-scheme redirect to extract the auth code,
        and exchanges it for tokens.
        """
        self._check_browser_rate_limit()
        self._record_browser_launch()

        from playwright.async_api import async_playwright

        state = secrets.token_urlsafe(16)
        verifier, challenge = self.generate_pkce_pair()
        authorize_url = self.build_authorize_url(state, challenge)

        logger.info("Launching headless browser for Auth0 login")

        auth_code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context_kwargs: dict[str, Any] = {}
                if self._config.log_level.upper() == "DEBUG":
                    debug_har = str(Path(self._config.token_dir) / "auth_debug.har")
                    context_kwargs["record_har_path"] = debug_har
                    logger.debug("Recording HAR to %s", debug_har)

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                # Intercept the custom-scheme redirect
                async def _intercept_redirect(route: Any) -> None:
                    url = route.request.url
                    if url.startswith(AUTH0_CUSTOM_SCHEME):
                        parsed = urllib.parse.urlparse(url)
                        params = urllib.parse.parse_qs(parsed.query)
                        if "code" in params:
                            if not auth_code_future.done():
                                auth_code_future.set_result(params["code"][0])
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", _intercept_redirect)

                # Navigate to Auth0 authorize
                await page.goto(authorize_url, wait_until="networkidle")

                # Fill login form
                await page.fill('input[name="username"], input[type="email"]',
                                self._config.shark_username)
                await page.fill('input[name="password"], input[type="password"]',
                                self._config.shark_password)
                await page.click('button[type="submit"]')

                # Handle passkey enrollment interstitial
                try:
                    skip_btn = page.locator('text="Continue without passkeys"')
                    await skip_btn.click(timeout=5000)
                except Exception:
                    pass  # Interstitial may not appear

                # Wait for the redirect interception to capture the code
                code = await asyncio.wait_for(auth_code_future, timeout=30)

                logger.info("Auth code captured from redirect")

            except asyncio.TimeoutError:
                # Save screenshot for debugging
                try:
                    screenshot_path = str(
                        Path(self._config.token_dir)
                        / f"auth_failure_{int(time.time())}.png"
                    )
                    await page.screenshot(path=screenshot_path)
                    logger.error("Auth timed out. Screenshot saved to %s", screenshot_path)
                except Exception:
                    logger.error("Auth timed out and screenshot failed")
                raise SharkAuthError("Browser auth timed out waiting for redirect")
            except Exception as exc:
                # Save screenshot for debugging
                try:
                    screenshot_path = str(
                        Path(self._config.token_dir)
                        / f"auth_failure_{int(time.time())}.png"
                    )
                    await page.screenshot(path=screenshot_path)
                    logger.error("Auth failed. Screenshot saved to %s", screenshot_path)
                except Exception:
                    pass
                raise SharkAuthError(f"Browser auth failed: {exc}") from exc
            finally:
                await browser.close()

        # Exchange code for tokens
        await self.exchange_code_for_tokens(code, verifier)

    # --- PKCE helpers ---

    @staticmethod
    def generate_pkce_pair() -> tuple[str, str]:
        """Generate a PKCE code_verifier and code_challenge pair."""
        verifier = secrets.token_urlsafe(32)
        challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        """Build the Auth0 /authorize URL with PKCE params."""
        params = {
            "response_type": "code",
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "client_id": self._region.auth0_client_id,
            "redirect_uri": self._region.auth0_redirect_uri,
            "scope": AUTH0_SCOPES,
            "state": state,
            "prompt": "login",
        }
        return f"{self._region.auth0_url}/authorize?{urlencode(params)}"

    async def exchange_code_for_tokens(
        self, code: str, code_verifier: str
    ) -> None:
        """Exchange an authorization code for Auth0 tokens."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": self._region.auth0_client_id,
            "code_verifier": code_verifier,
            "code": code,
            "redirect_uri": self._region.auth0_redirect_uri,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._region.auth0_token_url, json=payload
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    error = data.get("error", "unknown")
                    desc = data.get("error_description", "")
                    raise SharkAuthError(
                        f"Auth0 code exchange failed ({resp.status}): {error} {desc}"
                    )

                if not self._tokens:
                    self._tokens = TokenData()
                self._tokens.auth0_id_token = data["id_token"]
                self._tokens.auth0_refresh_token = data.get("refresh_token")
                self._save_tokens()
                logger.info("Auth0 code exchange successful")

    # --- Token persistence ---

    def _load_tokens(self) -> TokenData | None:
        """Load tokens from disk."""
        if not self._token_path.exists():
            logger.debug("No token file found at %s", self._token_path)
            return None
        try:
            data = json.loads(self._token_path.read_text())
            tokens = TokenData(**data)
            logger.info("Loaded cached tokens from %s", self._token_path)
            return tokens
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Failed to load token file: %s", e)
            return None

    def _save_tokens(self) -> None:
        """Atomically write tokens to disk."""
        if not self._tokens:
            return

        self._tokens.saved_at = datetime.now(timezone.utc).isoformat()
        self._token_path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(
            dir=self._token_path.parent, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self._tokens.model_dump_json(indent=2))
            os.replace(tmp_path, self._token_path)
            logger.debug("Tokens saved to %s", self._token_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # --- Circuit breaker helpers ---

    def _check_browser_rate_limit(self) -> None:
        """Check if we've exceeded browser launch limits."""
        today = datetime.now(timezone.utc).timetuple().tm_yday
        if today != self._browser_launch_day:
            self._browser_launches_today = 0
            self._browser_launch_day = today

        if self._browser_launches_today >= MAX_BROWSER_LAUNCHES_PER_DAY:
            raise SharkAuthLockedError(
                f"Browser launch limit ({MAX_BROWSER_LAUNCHES_PER_DAY}/day) reached. "
                "Waiting until tomorrow to prevent account lockout."
            )

    def _record_browser_launch(self) -> None:
        """Record a browser launch for rate limiting."""
        today = datetime.now(timezone.utc).timetuple().tm_yday
        if today != self._browser_launch_day:
            self._browser_launches_today = 0
            self._browser_launch_day = today
        self._browser_launches_today += 1

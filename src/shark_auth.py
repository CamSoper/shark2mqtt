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

        from patchright.async_api import async_playwright

        state = secrets.token_urlsafe(16)
        verifier, challenge = self.generate_pkce_pair()
        authorize_url = self.build_authorize_url(state, challenge)

        # Use headed mode if DISPLAY is set (e.g., via xvfb-run).
        # Headed mode bypasses Cloudflare Turnstile CAPTCHA.
        # In Docker, use: xvfb-run python -m src.main
        has_display = bool(os.environ.get("DISPLAY"))
        chromium_path = self._find_chromium() if has_display else None
        headless = not has_display or chromium_path is None

        if headless:
            logger.info("Launching headless browser for Auth0 login")
        else:
            logger.info("Launching headed browser for Auth0 login (DISPLAY=%s)", os.environ["DISPLAY"])

        auth_code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        async with async_playwright() as p:
            launch_args = ["--no-sandbox", "--disable-setuid-sandbox"]
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "args": launch_args,
            }
            if not headless:
                launch_args.append("--disable-gpu")
                launch_kwargs["executable_path"] = chromium_path
                logger.debug("Using Chromium at %s", chromium_path)
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                context_kwargs: dict[str, Any] = {}
                if self._config.log_level.upper() == "DEBUG":
                    debug_har = str(Path(self._config.token_dir) / "auth_debug.har")
                    context_kwargs["record_har_path"] = debug_har
                    logger.debug("Recording HAR to %s", debug_har)

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                # Use CDP (Chrome DevTools Protocol) to intercept the auth code.
                # Auth0's /authorize/resume returns a 302 to the custom scheme
                # com.sharkninja.shark://...?code=...&state=...
                # Chromium can't navigate to custom schemes, so Playwright's
                # route/response handlers don't catch it. CDP's
                # Network.requestWillBeSent with redirectResponse does.
                cdp = await context.new_cdp_session(page)

                def _on_cdp_request(params: dict) -> None:
                    url = params.get("request", {}).get("url", "")
                    if url.startswith(AUTH0_CUSTOM_SCHEME):
                        logger.info("Auth code captured via CDP redirect")
                        parsed = urllib.parse.urlparse(url)
                        qs = urllib.parse.parse_qs(parsed.query)
                        if "code" in qs and not auth_code_future.done():
                            auth_code_future.set_result(qs["code"][0])

                cdp.on("Network.requestWillBeSent", _on_cdp_request)
                await cdp.send("Network.enable")

                # Navigate to Auth0 authorize
                await page.goto(authorize_url, wait_until="domcontentloaded")

                auth_error: Exception | None = None
                try:
                    # Fill email — Auth0 uses a multi-step flow
                    username_input = page.locator(
                        'input[name="username"], input[type="email"]'
                    ).first
                    await username_input.wait_for(state="visible", timeout=15000)
                    await username_input.fill(self._config.shark_username)

                    # Handle Cloudflare Turnstile CAPTCHA if present
                    # The checkbox is inside an iframe from challenges.cloudflare.com
                    try:
                        turnstile_frame = page.frame_locator(
                            'iframe[src*="challenges.cloudflare.com"]'
                        )
                        checkbox = turnstile_frame.locator(
                            'input[type="checkbox"], '
                            'label, '
                            'body'
                        ).first
                        # Wait briefly for Turnstile to load
                        await page.wait_for_timeout(2000)
                        if await page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
                            logger.debug("Turnstile CAPTCHA detected, clicking")
                            await checkbox.click(timeout=10000)
                            # Wait for Turnstile to verify
                            await page.wait_for_timeout(5000)
                    except Exception as captcha_err:
                        logger.debug("CAPTCHA handling: %s", captcha_err)

                    # Click Continue/Submit (Auth0 uses "Continue" button)
                    submit_btn = page.locator(
                        'button[type="submit"], button:has-text("Continue")'
                    ).first
                    await submit_btn.click()

                    # Wait for password field to appear (second step)
                    password_input = page.locator(
                        'input[name="password"], input[type="password"]'
                    ).locator("visible=true").first
                    await password_input.wait_for(state="visible", timeout=30000)
                    await password_input.fill(self._config.shark_password)

                    # Submit password via Enter key (more reliable than button click)
                    logger.debug("Submitting password")
                    await password_input.press("Enter")
                    logger.debug("Password submitted, waiting for response")

                    # Wait for page navigation after password submit
                    await page.wait_for_timeout(3000)

                    # Handle passkey enrollment interstitial
                    try:
                        skip_btn = page.locator('text="Continue without passkeys"')
                        await skip_btn.click(timeout=10000)
                        logger.debug("Skipped passkey enrollment")
                    except Exception:
                        pass  # Interstitial may not appear

                    # Wait for the redirect interception to capture the code
                    code = await asyncio.wait_for(auth_code_future, timeout=60)
                    logger.info("Auth code captured from redirect")

                except Exception as exc:
                    auth_error = exc
                    # Screenshot while page is still alive
                    await self._save_failure_screenshot(page)

            finally:
                await browser.close()

            if auth_error is not None:
                if isinstance(auth_error, (asyncio.TimeoutError, TimeoutError)):
                    raise SharkAuthError(
                        "Browser auth timed out waiting for redirect. "
                        "Check credentials or screenshot in TOKEN_DIR."
                    )
                raise SharkAuthError(
                    f"Browser auth failed: {auth_error}"
                ) from auth_error

        # Exchange code for tokens
        await self.exchange_code_for_tokens(code, verifier)

    # --- Browser helpers ---

    @staticmethod
    def _find_chromium() -> str | None:
        """Find the full Chromium binary (not headless shell)."""
        import glob
        patterns = [
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
        ]
        for pattern in patterns:
            matches = glob.glob(pattern)
            if matches:
                logger.debug("Found Chromium at %s", matches[0])
                return matches[0]
        return None

    # --- Screenshot helpers ---

    async def _save_failure_screenshot(self, page: Any) -> None:
        """Save a screenshot on auth failure for debugging."""
        try:
            screenshot_path = str(
                Path(self._config.token_dir)
                / f"auth_failure_{int(time.time())}.png"
            )
            await page.screenshot(path=screenshot_path)
            logger.error("Auth failure screenshot saved to %s", screenshot_path)
        except Exception:
            logger.debug("Could not save failure screenshot")

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

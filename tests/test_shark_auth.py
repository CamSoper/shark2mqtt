"""Tests for SharkAuth error handling and circuit-breaker behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.exc import SharkAuthError
from src.shark_auth import SharkAuth


def make_auth_config(tmp_path) -> MagicMock:
    config = MagicMock()
    config.shark_region = "us"
    config.shark_username = "test@example.com"
    config.shark_password = "hunter2"
    config.token_dir = str(tmp_path)
    config.log_level = "INFO"
    return config


class TestRefreshFailureLogging:
    @pytest.mark.asyncio
    async def test_refresh_failure_reason_is_logged(self, tmp_path, caplog):
        auth = SharkAuth(make_auth_config(tmp_path))
        auth._tokens = MagicMock(auth0_id_token=None, auth0_refresh_token="stale-token")
        auth._refresh_auth0_token = AsyncMock(
            side_effect=SharkAuthError("Auth0 refresh failed (400): invalid_grant expired")
        )
        auth._browser_authenticate = AsyncMock(side_effect=SharkAuthError("no browser"))

        with caplog.at_level("WARNING"):
            with pytest.raises(SharkAuthError):
                await auth.ensure_authenticated()

        assert any(
            "invalid_grant" in record.message for record in caplog.records
        ), "the actual Auth0 rejection reason should be logged, not swallowed"


class TestBrowserAuthErrorHandling:
    @pytest.mark.asyncio
    async def test_navigation_failure_becomes_shark_auth_error(self, tmp_path):
        """A page.goto() failure (e.g. login.sharkninja.com unreachable) must
        raise SharkAuthError — not escape as a raw patchright/Playwright
        exception — so it reaches ensure_authenticated()'s circuit breaker
        instead of bypassing it and retry-hammering Auth0 every poll cycle.
        """
        auth = SharkAuth(make_auth_config(tmp_path))
        auth._save_failure_screenshot = AsyncMock()
        auth._extract_page_error = AsyncMock(return_value=None)

        mock_page = AsyncMock()
        mock_page.goto.side_effect = TimeoutError("Page.goto: Timeout 30000ms exceeded.")

        mock_cdp = AsyncMock()
        mock_cdp.on = MagicMock()  # synchronous event-registration call

        mock_context = AsyncMock()
        mock_context.new_page.return_value = mock_page
        mock_context.new_cdp_session.return_value = mock_cdp

        mock_browser = AsyncMock()
        mock_browser.new_context.return_value = mock_context

        mock_chromium = AsyncMock()
        mock_chromium.launch.return_value = mock_browser

        mock_playwright_obj = MagicMock()
        mock_playwright_obj.chromium = mock_chromium

        mock_playwright_cm = AsyncMock()
        mock_playwright_cm.__aenter__.return_value = mock_playwright_obj
        mock_playwright_cm.__aexit__.return_value = None

        with patch("patchright.async_api.async_playwright", return_value=mock_playwright_cm):
            with pytest.raises(SharkAuthError):
                await auth._browser_authenticate()

        # Navigation failures should still get a diagnostic screenshot,
        # same as email/password failures do.
        auth._save_failure_screenshot.assert_awaited_once()

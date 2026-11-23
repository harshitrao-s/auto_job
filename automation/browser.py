"""Playwright browser lifecycle.

Uses a *persistent context* so an existing, user-controlled Chrome session is
reused: the bot never handles credentials. Two modes:

* CHROME_USER_DATA_DIR set   -> your real Chrome profile (Chrome must be closed)
* CHROME_USER_DATA_DIR empty -> a dedicated ./browser_profile you log into once

The browser identifies itself normally (no user-agent spoofing, no automation
flag masking). Pacing is deliberately slow so the session stays light on
LinkedIn's servers.
"""
from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from config import BrowserSettings
from utils.logger import get_logger

log = get_logger(__name__)


class BrowserManager:
    def __init__(self, settings: BrowserSettings) -> None:
        self.settings = settings
        self._playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> Page:
        self.settings.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()

        launch_kwargs = {
            "user_data_dir": str(self.settings.user_data_dir),
            "headless": self.settings.headless,
            "slow_mo": self.settings.slow_mo_ms,
            "viewport": {"width": 1440, "height": 900},
            "args": ["--profile-directory=" + self.settings.profile_directory],
            "locale": "en-IN",
            "timezone_id": "Asia/Kolkata",
        }

        try:
            self.context = self._playwright.chromium.launch_persistent_context(
                channel=self.settings.channel, **launch_kwargs
            )
            log.info(
                "Launched %s with profile %s",
                self.settings.channel,
                self.settings.user_data_dir,
            )
        except PlaywrightError as exc:
            # Typical causes: Chrome not installed for this channel, or the
            # profile is locked because Chrome is already running.
            log.warning(
                "Could not launch channel '%s' (%s) - falling back to bundled Chromium",
                self.settings.channel,
                str(exc).splitlines()[0],
            )
            self.context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        self.context.set_default_timeout(self.settings.nav_timeout_ms)
        self.context.set_default_navigation_timeout(self.settings.nav_timeout_ms)

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self.page

    def stop(self) -> None:
        for closer in (
            lambda: self.context.close() if self.context else None,
            lambda: self._playwright.stop() if self._playwright else None,
        ):
            try:
                closer()
            except Exception as exc:  # pragma: no cover - shutdown best effort
                log.debug("Shutdown step failed: %s", exc)
        self.context = None
        self.page = None
        self._playwright = None
        log.info("Browser closed")

    def __enter__(self) -> Page:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- helpers ------------------------------------------------------------ #
    def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        if not self.page:
            raise RuntimeError("Browser not started")
        log.debug("Navigating to %s", url)
        self.page.goto(url, wait_until=wait_until)

    def screenshot(self, tag: str) -> Optional[Path]:
        """Capture the viewport for post-mortem debugging of a failure."""
        if not (self.settings.screenshot_on_error and self.page):
            return None
        self.settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)[:60]
        path = self.settings.screenshot_dir / (
            datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + safe + ".png"
        )
        try:
            self.page.screenshot(path=str(path), full_page=False)
            log.info("Screenshot saved: %s", path)
            return path
        except PlaywrightError as exc:
            log.debug("Screenshot failed: %s", exc)
            return None


def human_pause(minimum: float = 1.0, maximum: float = 3.0) -> None:
    """Randomised pause so page requests stay well spaced out."""
    time.sleep(random.uniform(minimum, max(minimum, maximum)))

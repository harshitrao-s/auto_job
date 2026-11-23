"""Session handling.

The bot never types a password and never attempts to defeat a CAPTCHA,
2FA or a security checkpoint. It only *reuses* a session the user already
owns, in one of two ways:

1. A persistent Chrome profile where the user is already signed in.
2. A cookies JSON file the user exported from their own logged-in browser
   (Cookie-Editor / EditThisCookie format, or Playwright's storage_state).

If neither yields a logged-in session, the run pauses and asks the user to
sign in by hand in the open window.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout

from utils.logger import get_logger

log = get_logger(__name__)

LINKEDIN_FEED = "https://www.linkedin.com/feed/"
NAUKRI_HOME = "https://www.naukri.com/mnjuser/homepage"

# Pages that mean "a human must act" - we stop rather than work around them.
CHECKPOINT_MARKERS = (
    "/checkpoint/challenge",
    "/checkpoint/lg/login-submit",
    "/uas/login",
    "captcha",
)


class AuthenticationRequired(RuntimeError):
    """Raised when no usable session exists and no human is available."""


class SecurityCheckpoint(RuntimeError):
    """Raised when the platform shows a challenge - the run must stop."""


# --------------------------------------------------------------------------- #
# cookie import
# --------------------------------------------------------------------------- #


def _normalise_cookie(raw: Dict[str, Any], default_domain: str) -> Optional[Dict[str, Any]]:
    """Convert one browser-extension cookie record into Playwright's shape."""
    name = raw.get("name")
    value = raw.get("value")
    if not name or value is None:
        return None

    cookie: Dict[str, Any] = {
        "name": str(name),
        "value": str(value),
        "domain": str(raw.get("domain") or default_domain),
        "path": str(raw.get("path") or "/"),
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", True)),
    }

    expires = raw.get("expirationDate", raw.get("expires"))
    if isinstance(expires, (int, float)) and expires > 0:
        cookie["expires"] = float(expires)

    same_site = str(raw.get("sameSite", "")).lower()
    cookie["sameSite"] = {
        "no_restriction": "None",
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
        "unspecified": "Lax",
    }.get(same_site, "Lax")

    # A host-only cookie must not carry a leading dot mismatch.
    if cookie["domain"] and not cookie["domain"].startswith("."):
        if raw.get("hostOnly") is False:
            cookie["domain"] = "." + cookie["domain"]
    return cookie


def load_cookies(context: BrowserContext, cookies_path: Path, default_domain: str) -> int:
    """Inject a user-exported cookies JSON file into the browser context.

    Accepts either a bare list of cookies or a Playwright storage_state object
    ({"cookies": [...]}) . Returns how many cookies were added.
    """
    path = Path(cookies_path)
    if not path.exists():
        raise AuthenticationRequired("Cookies file not found: " + str(path))

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise AuthenticationRequired("Cookies file is not valid JSON: " + str(exc)) from exc

    raw_cookies: List[Dict[str, Any]]
    if isinstance(payload, dict):
        raw_cookies = payload.get("cookies", [])
    elif isinstance(payload, list):
        raw_cookies = payload
    else:
        raise AuthenticationRequired("Unrecognised cookies file structure")

    cookies = [c for c in (_normalise_cookie(r, default_domain) for r in raw_cookies) if c]
    if not cookies:
        raise AuthenticationRequired("No usable cookies found in " + str(path))

    context.add_cookies(cookies)
    log.info("Loaded %d cookies from %s", len(cookies), path.name)
    return len(cookies)


# --------------------------------------------------------------------------- #
# session checks
# --------------------------------------------------------------------------- #


def _looks_like_checkpoint(page: Page) -> bool:
    url = (page.url or "").lower()
    return any(marker in url for marker in CHECKPOINT_MARKERS)


# li_at is LinkedIn's session cookie - only a signed-in browser holds it.
LINKEDIN_AUTH_COOKIES = {"li_at"}


def is_linkedin_logged_in(page: Page) -> bool:
    """Three independent checks, same strategy as the Naukri probe.

    LinkedIn renamed its `global-nav` classes, so selector probing alone is
    not reliable: the redirect behaviour of /feed/ is the real signal.
    """
    try:
        page.goto(LINKEDIN_FEED, wait_until="domcontentloaded")
    except PWTimeout:
        log.warning("Timed out loading the LinkedIn feed")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeout:
        pass

    if _looks_like_checkpoint(page):
        raise SecurityCheckpoint(
            "LinkedIn is showing a login/security challenge at " + page.url +
            " - complete it yourself in the open window, then re-run."
        )

    url = (page.url or "").lower()
    log.info("LinkedIn landed on: %s", url)

    # 1. A logged-out visitor is bounced to the login page or the authwall.
    if any(marker in url for marker in ("/login", "/authwall", "/signup")):
        log.info("Redirected to the login page - session is not active")
        return False
    # /feed/, /feed/foryou/, or a profile page all mean a live session.
    if "/feed" in url or "linkedin.com/in/" in url:
        return True

    # 2. Chrome elements only a signed-in page renders.
    for selector in (
        "button[aria-label*='Me' i]",
        "a[href*='/in/']",
        "div.global-nav__me",
        "[data-view-name='navigation-bar']",
        "nav[aria-label*='Primary' i]",
    ):
        try:
            page.wait_for_selector(selector, timeout=4000, state="attached")
            return True
        except PWTimeout:
            continue
        except Exception:
            continue

    # 3. The session cookie itself.
    try:
        names = {c.get("name", "") for c in page.context.cookies()}
        if LINKEDIN_AUTH_COOKIES & names:
            log.info("LinkedIn li_at cookie present - treating the session as active")
            return True
    except Exception:
        pass

    return False


# Cookies Naukri only issues to a signed-in session.
NAUKRI_AUTH_COOKIES = {"nauk_at", "nauk_rt", "nauk_sid", "is_login", "NKWAP"}


def is_naukri_logged_in(page: Page) -> bool:
    """Three independent checks, cheapest first - any one of them is enough.

    Selector probing alone is unreliable here: Naukri ships hashed class names
    and renders its header late, so we lead with the redirect behaviour of a
    members-only URL and fall back to the session cookies.
    """
    try:
        page.goto(NAUKRI_HOME, wait_until="domcontentloaded")
    except PWTimeout:
        log.warning("Timed out loading the Naukri homepage")
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeout:
        pass

    url = (page.url or "").lower()
    log.info("Naukri landed on: %s", url)

    # 1. A logged-out visitor is redirected away from /mnjuser/.
    if "login" in url:
        log.info("Redirected to the login page - session is not active")
        return False
    if "/mnjuser/" in url:
        return True

    # 2. Header elements that only a signed-in page renders.
    for selector in (
        "div[class*='nI-gNb-drawer']",
        "a[href*='/mnjuser/profile']",
        "div[class*='view-profile']",
        "div[class*='nI-gNb-menuItems']",
    ):
        try:
            page.wait_for_selector(selector, timeout=4000, state="attached")
            return True
        except PWTimeout:
            continue
        except Exception:
            continue

    # 3. Session cookies issued only after a real login.
    try:
        names = {c.get("name", "") for c in page.context.cookies()}
        if NAUKRI_AUTH_COOKIES & names:
            log.info("Naukri auth cookie present - treating the session as active")
            return True
    except Exception:
        pass

    return False


def ensure_logged_in(
    page: Page,
    platform: str = "linkedin",
    interactive: bool = True,
    cookies_path: Optional[Path] = None,
) -> None:
    """Guarantee a usable session or raise.

    Order: cookies file (if given) -> existing profile session -> ask the human.
    """
    platform = platform.lower()
    checker = is_naukri_logged_in if platform == "naukri" else is_linkedin_logged_in
    domain = ".naukri.com" if platform == "naukri" else ".linkedin.com"

    if cookies_path and Path(cookies_path).exists():
        try:
            load_cookies(page.context, Path(cookies_path), domain)
        except AuthenticationRequired as exc:
            log.warning("%s", exc)

    if checker(page):
        log.info("%s session is active", platform)
        return

    if not interactive:
        raise AuthenticationRequired(
            "No active " + platform + " session and INTERACTIVE=false. "
            "Export a fresh cookies JSON or sign in once in the browser profile."
        )

    print("\n" + "=" * 72)
    print("SIGN IN REQUIRED - " + platform.upper())
    print("=" * 72)
    print("A browser window is open. Please sign in there yourself")
    print("(including any OTP / CAPTCHA / 2FA step). The bot will not touch it.")
    print("When you can see your logged-in homepage, come back here.")
    try:
        input("Press Enter once you are signed in > ")
    except (EOFError, KeyboardInterrupt):
        raise AuthenticationRequired("Sign-in cancelled by the user")

    if not checker(page):
        raise AuthenticationRequired(
            "Still not signed in to " + platform + " - aborting this run."
        )
    log.info("%s session confirmed after manual sign-in", platform)


def save_storage_state(context: BrowserContext, path: Path) -> None:
    """Persist the current session so the next run can reuse it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(path))
        log.debug("Saved storage state to %s", path)
    except Exception as exc:  # pragma: no cover
        log.debug("Could not save storage state: %s", exc)

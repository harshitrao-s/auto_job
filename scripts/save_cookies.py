"""Cookie exporter - no browser extension needed.

Opens a CLEAN browser window (fresh profile, nothing saved to disk - the same
isolation an incognito window gives you), waits while YOU log in by hand, then
writes the resulting session to cookies/<platform>_cookies.json in exactly the
format automation/authentication.py expects.

    python scripts/save_cookies.py naukri
    python scripts/save_cookies.py linkedin
    python scripts/save_cookies.py naukri --keep-open

Nothing is typed for you: no password, no OTP, no CAPTCHA. The script only
reads the cookies your own login produced, including HttpOnly ones that a
DevTools console snippet cannot reach.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from config import load_settings  # noqa: E402

LOGIN_URLS = {
    "naukri": "https://www.naukri.com/nlogin/login",
    "linkedin": "https://www.linkedin.com/login",
}
HOME_URLS = {
    "naukri": "https://www.naukri.com/mnjuser/homepage",
    "linkedin": "https://www.linkedin.com/feed/",
}
DOMAIN_FILTER = {
    "naukri": "naukri.com",
    "linkedin": "linkedin.com",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Export your own login cookies to JSON")
    parser.add_argument("platform", choices=["naukri", "linkedin"])
    parser.add_argument("--output", help="override the output path")
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the window open after saving")
    args = parser.parse_args()

    settings = load_settings()
    default_output = (
        settings.paths.naukri_cookies if args.platform == "naukri"
        else settings.paths.linkedin_cookies
    )
    output = Path(args.output) if args.output else default_output
    output.parent.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print("COOKIE EXPORT - " + args.platform.upper())
    print("=" * 70)
    print("A clean browser window will open (nothing is saved to your Chrome")
    print("profile - it behaves like an incognito window).")
    print("")
    print("  1. Log in there yourself (password / OTP / CAPTCHA - all by you).")
    print("  2. Wait until your logged-in homepage is visible.")
    print("  3. Come back here and press Enter.")
    print("")

    with sync_playwright() as playwright:
        # A persistent context with no user_data_dir is not possible, so we use
        # a normal (non-persistent) context: it lives only in memory.
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False, slow_mo=50)
        except Exception:
            browser = playwright.chromium.launch(headless=False, slow_mo=50)

        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
        page = context.new_page()
        page.goto(LOGIN_URLS[args.platform], wait_until="domcontentloaded")

        try:
            input("Press Enter once you are fully logged in > ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            browser.close()
            return 1

        # Visit the logged-in homepage so every session cookie is issued.
        try:
            page.goto(HOME_URLS[args.platform], wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception:
            pass

        cookies = context.cookies()
        domain = DOMAIN_FILTER[args.platform]
        relevant = [c for c in cookies if domain in (c.get("domain") or "")]

        if not relevant:
            print("\nNo " + domain + " cookies found - are you definitely logged in?")
            browser.close()
            return 2

        with output.open("w", encoding="utf-8") as handle:
            json.dump({"cookies": relevant}, handle, indent=2)

        print("\nSaved " + str(len(relevant)) + " cookies -> " + str(output))
        print("This file IS your login session. Keep it private (it is gitignored).")
        print("Re-run this script whenever the bot says the session expired.")

        if args.keep_open:
            try:
                input("Press Enter to close the browser > ")
            except (EOFError, KeyboardInterrupt):
                pass
        browser.close()

    print("\nNow run:  python main.py --platform " + args.platform + " --dry-run --max 3\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

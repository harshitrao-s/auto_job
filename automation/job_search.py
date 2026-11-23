"""LinkedIn job search: build filtered search URLs and harvest result cards.

Only the public /jobs/search/ UI is used, one page at a time, with a pause
between pages so the request rate stays low.
"""
from __future__ import annotations

import re
from typing import Iterator, List, Optional
from urllib.parse import urlencode

from playwright.sync_api import Page, TimeoutError as PWTimeout

from config import SearchSettings
from automation.browser import human_pause
from database.models import Job
from utils.logger import get_logger

log = get_logger(__name__)

SEARCH_BASE = "https://www.linkedin.com/jobs/search/"
# Public listing endpoint used for discovery when the signed-in UI withholds
# its result list. It returns a plain HTML card fragment, 10 jobs per call.
GUEST_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
GUEST_PER_PAGE = 10
RESULTS_PER_PAGE = 25

# Phrases LinkedIn shows when it is rate-limiting or blocking automated use.
RESTRICTION_MARKERS = (
    "you've reached the weekly application limit",
    "too many requests",
    "unusual activity",
    "temporarily restricted",
    "we restrict",
)

JOB_ID_PATTERNS = (
    re.compile(r"/jobs/view/(\d+)"),
    re.compile(r"currentJobId=(\d+)"),
)


class SearchRestricted(RuntimeError):
    """LinkedIn signalled that automated access is being limited - stop."""


def extract_job_id(href: str) -> Optional[str]:
    for pattern in JOB_ID_PATTERNS:
        match = pattern.search(href or "")
        if match:
            return match.group(1)
    return None


def build_search_url(
    settings: SearchSettings, keyword: str, location: str, start: int = 0
) -> str:
    """Compose a LinkedIn jobs search URL from the configured filters."""
    params = {
        "keywords": keyword,
        "location": location,
        "sortBy": settings.sort_by,          # DD = most recent
    }
    if settings.easy_apply_only:
        params["f_AL"] = "true"              # Easy Apply only
    if settings.job_types:
        params["f_JT"] = ",".join(settings.job_types)
    codes = settings.experience_codes
    if codes:
        params["f_E"] = ",".join(codes)
    if settings.date_posted:
        params["f_TPR"] = settings.date_posted
    if settings.remote_only:
        params["f_WT"] = "2"                 # 1 on-site, 2 remote, 3 hybrid
    if start:
        params["start"] = str(start)
    return SEARCH_BASE + "?" + urlencode(params)


class JobSearch:
    def __init__(self, page: Page, settings: SearchSettings) -> None:
        self.page = page
        self.settings = settings

    # -- guards ------------------------------------------------------------- #
    def _check_restrictions(self) -> None:
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            return
        for marker in RESTRICTION_MARKERS:
            if marker in body:
                raise SearchRestricted(
                    "LinkedIn reported a restriction ('" + marker + "'). Stopping this run."
                )

    # -- result harvesting -------------------------------------------------- #
    def _scroll_result_list(self) -> None:
        """The results pane lazy-loads; scroll it so every card renders."""
        selectors = (
            "div.scaffold-layout__list > div",
            "div.jobs-search-results-list",
            "ul.scaffold-layout__list-container",
        )
        for selector in selectors:
            try:
                container = self.page.locator(selector).first
                if not container.is_visible(timeout=3000):
                    continue
                for _ in range(6):
                    container.evaluate("el => el.scrollBy(0, el.clientHeight)")
                    human_pause(0.4, 0.9)
                return
            except Exception:
                continue
        # Fall back to scrolling the window itself.
        for _ in range(5):
            try:
                self.page.mouse.wheel(0, 1200)
            except Exception:
                break
            human_pause(0.4, 0.9)

    def _cards(self) -> List[Job]:
        """Read the visible result cards into lightweight Job objects."""
        jobs: List[Job] = []
        seen: set = set()

        link_selector = (
            "a.job-card-container__link, a.job-card-list__title, "
            "a.job-card-list__title--link, a[href*='/jobs/view/']"
        )
        links = self.page.locator(link_selector)
        count = min(links.count(), 60)

        for index in range(count):
            link = links.nth(index)
            try:
                href = link.get_attribute("href") or ""
                job_id = extract_job_id(href)
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)

                title = (link.get_attribute("aria-label") or link.inner_text() or "").strip()
                title = title.split("\n")[0].strip()

                # Walk up to the card to read company and location.
                company = location = ""
                try:
                    card = link.locator(
                        "xpath=ancestor::*[contains(@class,'job-card-container') "
                        "or contains(@class,'jobs-search-results__list-item')][1]"
                    ).first
                    text = card.inner_text(timeout=2000)
                    lines = [l.strip() for l in text.split("\n") if l.strip()]
                    # Typical card: title, company, location, metadata...
                    unique: List[str] = []
                    for line in lines:
                        if line not in unique:
                            unique.append(line)
                    if len(unique) >= 2:
                        company = unique[1]
                    if len(unique) >= 3:
                        location = unique[2]
                except Exception:
                    pass

                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        url="https://www.linkedin.com/jobs/view/" + job_id + "/",
                    )
                )
            except Exception as exc:
                log.debug("Skipping unreadable card %d: %s", index, exc)
                continue
        return jobs

    # -- guest listing fallback -------------------------------------------- #
    def _guest_cards(self, keyword: str, location: str, start: int) -> List[Job]:
        """Read one page of the public job listing endpoint.

        LinkedIn's signed-in jobs UI was rebuilt as a server-driven app whose
        result list never lands in the DOM (hashed class names, one job in the
        markup). The public listing endpoint still returns a plain HTML card
        list with a stable `urn:li:jobPosting:<id>` on every card, so we use it
        to DISCOVER ids and then open each job normally in the signed-in
        session, where Easy Apply lives.
        """
        url = GUEST_SEARCH + "?" + urlencode(
            self._guest_params(keyword, location, start)
        )
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PWTimeout:
            log.warning("Guest listing timed out")
            return []
        human_pause(1.0, 2.0)

        html = self.page.content()
        ids = re.findall(r'data-entity-urn="urn:li:jobPosting:(\d+)"', html)
        if not ids:
            return []

        jobs: List[Job] = []
        cards = self.page.locator("li div.base-card, div.base-search-card, li")
        for index, job_id in enumerate(ids):
            title = company = card_location = posted = ""
            try:
                card = cards.nth(index)
                for selector, slot in (
                    ("h3", "title"),
                    ("h4", "company"),
                    ("span.job-search-card__location", "location"),
                    ("time", "posted"),
                ):
                    node = card.locator(selector).first
                    if node.count() == 0:
                        continue
                    text = (node.inner_text(timeout=1200) or "").strip()
                    if slot == "title":
                        title = text
                    elif slot == "company":
                        company = text
                    elif slot == "location":
                        card_location = text
                    else:
                        posted = text
            except Exception as exc:
                log.debug("Guest card %d unreadable: %s", index, exc)

            jobs.append(
                Job(
                    job_id=job_id,
                    title=title,
                    company=company,
                    location=card_location,
                    posted=posted,
                    url="https://www.linkedin.com/jobs/view/" + job_id + "/",
                )
            )
        return jobs

    def _guest_params(self, keyword: str, location: str, start: int) -> dict:
        params = {"keywords": keyword, "location": location, "start": str(start)}
        if self.settings.easy_apply_only:
            params["f_AL"] = "true"
        if self.settings.job_types:
            params["f_JT"] = ",".join(self.settings.job_types)
        if self.settings.experience_codes:
            params["f_E"] = ",".join(self.settings.experience_codes)
        if self.settings.date_posted:
            params["f_TPR"] = self.settings.date_posted
        if self.settings.sort_by:
            params["sortBy"] = self.settings.sort_by
        if self.settings.remote_only:
            params["f_WT"] = "2"
        return params

    def search(self, keyword: str, location: str) -> Iterator[Job]:
        """Yield jobs for one keyword/location pair across configured pages."""
        seen: set = set()

        for page_index in range(max(1, self.settings.pages_per_search)):
            url = build_search_url(
                self.settings, keyword, location, start=page_index * RESULTS_PER_PAGE
            )
            log.info("Search page %d | '%s' in '%s'", page_index + 1, keyword, location)
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except PWTimeout:
                log.warning("Search page timed out, moving on")
                return

            self._check_restrictions()

            try:
                self.page.wait_for_selector(
                    "a[href*='/jobs/view/'], div.jobs-search-no-results-banner",
                    timeout=20000,
                )
            except PWTimeout:
                log.info("No results rendered for '%s' in '%s'", keyword, location)
                return

            self._scroll_result_list()
            cards = self._cards()

            # The rebuilt jobs UI renders at most the one open job, so fall
            # back to the public listing endpoint for discovery.
            if len(cards) < 2:
                log.info(
                    "Signed-in list yielded %d card(s) - using the public listing endpoint",
                    len(cards),
                )
                cards = self._guest_cards(
                    keyword, location, start=page_index * GUEST_PER_PAGE
                )

            log.info("Found %d cards on page %d", len(cards), page_index + 1)
            if not cards:
                return

            for job in cards:
                if job.job_id in seen:
                    continue
                seen.add(job.job_id)
                job.keyword = keyword
                job.search_location = location
                yield job

            human_pause(2.0, 4.0)

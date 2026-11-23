"""Job detail extraction.

Opens a job page and reads title, company, location, workplace/employment type,
seniority, the full description, whether Easy Apply is offered and whether the
user has already applied (LinkedIn's own "Applied" marker).
"""
from __future__ import annotations

import re
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PWTimeout

from automation.browser import human_pause
from automation.job_matcher import extract_required_experience
from database.models import Job
from utils.logger import get_logger

log = get_logger(__name__)

TITLE_SELECTORS = (
    "h1.job-details-jobs-unified-top-card__job-title",
    "div.job-details-jobs-unified-top-card__job-title h1",
    "h1.t-24",
    "h1",
)
COMPANY_SELECTORS = (
    "div.job-details-jobs-unified-top-card__company-name a",
    "div.job-details-jobs-unified-top-card__company-name",
    "a.topcard__org-name-link",
    "span.topcard__flavor",
)
LOCATION_SELECTORS = (
    "div.job-details-jobs-unified-top-card__primary-description-container span.tvm__text",
    "span.topcard__flavor--bullet",
    "div.job-details-jobs-unified-top-card__tertiary-description-container",
)
DESCRIPTION_SELECTORS = (
    "div.jobs-description__content",
    "div#job-details",
    "article.jobs-description__container",
    "div.jobs-box__html-content",
    "div.show-more-less-html__markup",
)
INSIGHT_SELECTORS = (
    "li.job-details-jobs-unified-top-card__job-insight",
    "div.job-details-jobs-unified-top-card__job-insight",
    "span.jobs-unified-top-card__job-insight",
    "div.job-details-fit-level-preferences",
)

WORKPLACE_WORDS = ("remote", "hybrid", "on-site", "onsite")
EMPLOYMENT_WORDS = ("full-time", "part-time", "contract", "internship", "temporary", "volunteer")
SENIORITY_WORDS = (
    "internship", "entry level", "associate", "mid-senior level", "director", "executive",
)


def _first_text(page: Page, selectors, timeout: int = 2500) -> str:
    for selector in selectors:
        try:
            node = page.locator(selector).first
            if node.count() == 0:
                continue
            text = (node.inner_text(timeout=timeout) or "").strip()
            if text:
                return re.sub(r"\s+", " ", text)
        except Exception:
            continue
    return ""


class JobParser:
    def __init__(self, page: Page) -> None:
        self.page = page

    def open(self, job: Job) -> bool:
        """Navigate to the job page. Returns False when it cannot be opened."""
        url = job.url or ("https://www.linkedin.com/jobs/view/" + job.job_id + "/")
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PWTimeout:
            log.warning("Timed out opening job %s", job.job_id)
            return False

        try:
            self.page.wait_for_selector(
                ", ".join(DESCRIPTION_SELECTORS) + ", h1, main", timeout=20000
            )
        except PWTimeout:
            log.warning("Job %s never rendered", job.job_id)
            return False
        # The rebuilt UI streams its content in after first paint.
        human_pause(2.5, 4.0)
        return True

    def _expand_description(self) -> None:
        """Click 'See more' so the whole description is in the DOM."""
        for selector in (
            "button.jobs-description__footer-button",
            "button[aria-label*='see more' i]",
            "button.show-more-less-html__button--more",
        ):
            try:
                button = self.page.locator(selector).first
                if button.count() and button.is_visible(timeout=1500):
                    button.click(timeout=3000)
                    human_pause(0.4, 0.9)
                    return
            except Exception:
                continue

    def has_easy_apply(self) -> bool:
        for selector in (
            "button.jobs-apply-button",
            "button[aria-label*='Easy Apply' i]",
            "div.jobs-apply-button--top-card button",
        ):
            try:
                button = self.page.locator(selector).first
                if button.count() == 0:
                    continue
                label = ((button.inner_text(timeout=2000) or "") + " " +
                         (button.get_attribute("aria-label") or "")).lower()
                if "easy apply" in label:
                    return True
            except Exception:
                continue
        return False

    def already_applied(self) -> bool:
        """LinkedIn marks previously applied jobs - respect that marker."""
        try:
            lines = [
                l.strip().lower()
                for l in (self.page.inner_text("main", timeout=5000) or "").splitlines()
                if l.strip()
            ][:25]
        except Exception:
            return False
        for line in lines:
            # "Applied 2 days ago", "You applied on ...", "Application submitted"
            if line.startswith("applied") or line.startswith("you applied"):
                return True
            if "application submitted" in line:
                return True
        return False

    # ------------------------------------------------------------------ #
    # rebuilt-UI parsing
    # ------------------------------------------------------------------ #
    def _parse_from_text(self, job: Job) -> bool:
        """Read the job out of <main>'s text.

        LinkedIn's current jobs UI ships hashed class names and no stable
        hooks, but the rendered text is reliably laid out as:

            0  Company
            1  Job title
            2  Location - posted - applicants
            3+ salary / On-site|Hybrid|Remote / Full-time / Easy Apply ...
               ...
               About the job
               <description>
        """
        try:
            raw = self.page.inner_text("main", timeout=8000) or ""
        except Exception:
            return False

        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if len(lines) < 4:
            return False

        if not job.company:
            job.company = lines[0][:120]
        job.title = lines[1][:160] if len(lines) > 1 else job.title

        # "Bengaluru, Karnataka, India - 2 hours ago - Over 100 applicants"
        meta = lines[2] if len(lines) > 2 else ""
        parts = [p.strip() for p in re.split(r"[·•]", meta) if p.strip()]
        if parts:
            job.location = parts[0][:120]
        if len(parts) > 1:
            job.posted = parts[1][:60]

        head = " | ".join(lines[:14]).lower()
        for word in WORKPLACE_WORDS:
            if word in head:
                job.workplace_type = word.title()
                break
        for word in EMPLOYMENT_WORDS:
            if word in head:
                job.employment_type = word.title()
                break
        for word in SENIORITY_WORDS:
            if word in head:
                job.seniority = word.title()
                break

        # Description starts after the "About the job" heading.
        description = ""
        for index, line in enumerate(lines):
            if line.lower().startswith("about the job") or line.lower() == "job description":
                description = "\n".join(lines[index + 1:])
                break
        job.description = (description or "\n".join(lines[3:]))[:20000]
        return bool(job.title and job.description)

    def parse(self, job: Job) -> Optional[Job]:
        """Fill `job` in place with everything on the detail page."""
        if not self.open(job):
            return None

        self._expand_description()

        job.title = _first_text(self.page, TITLE_SELECTORS) or job.title
        job.company = _first_text(self.page, COMPANY_SELECTORS) or job.company
        location_text = _first_text(self.page, LOCATION_SELECTORS)
        if location_text:
            job.location = location_text
        job.description = _first_text(self.page, DESCRIPTION_SELECTORS, timeout=6000)

        insights = " ".join(
            _first_text(self.page, (selector,)) for selector in INSIGHT_SELECTORS
        ).lower()
        combined = insights + " " + job.location.lower()

        for word in WORKPLACE_WORDS:
            if word in combined:
                job.workplace_type = word.title()
                break
        for word in EMPLOYMENT_WORDS:
            if word in insights:
                job.employment_type = word.title()
                break
        for word in SENIORITY_WORDS:
            if word in insights:
                job.seniority = word.title()
                break

        if not job.description or not job.title:
            # Current LinkedIn UI: no stable selectors, parse the text instead.
            if not self._parse_from_text(job):
                log.warning("Could not extract job %s from either layout", job.job_id)

        job.easy_apply = self.has_easy_apply()
        job.already_applied = self.already_applied()
        job.required_experience_years = extract_required_experience(job.description)

        log.info(
            "Parsed: %s @ %s | %s | easy_apply=%s | exp=%s",
            job.title, job.company, job.location, job.easy_apply,
            job.required_experience_years,
        )
        return job

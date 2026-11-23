"""Naukri.com search + apply.

Session comes from a cookies JSON file you export yourself from a logged-in
browser (see README "Naukri cookies"). The bot never logs in for you.

Naukri's apply flow has three shapes:

1. Instant apply      - one click, a success toast appears.
2. Chatbot drawer     - a conversational questionnaire opens; each question is
                        answered through the same QuestionHandler used for
                        LinkedIn, so unknown questions still stop the flow.
3. Company-site apply - redirects off Naukri. We never follow it; the job is
                        recorded as NEEDS_HUMAN with its URL.

Selectors are matched loosely (several fallbacks each) because Naukri ships
hashed CSS class names that change between releases.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote_plus

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from automation.browser import human_pause
from automation.easy_apply import ApplyOutcome
from automation.job_matcher import extract_required_experience
from config import SearchSettings
from database.models import ApplicationStatus, Job
from services.question_handler import Question, QuestionHandler
from utils.logger import get_logger

log = get_logger(__name__)

BASE = "https://www.naukri.com/"
JOB_ID_RE = re.compile(r"-(\d{6,})(?:\?|$)")

RESTRICTION_MARKERS = (
    "unusual activity",
    "too many requests",
    "access denied",
    "temporarily blocked",
)


def _slug(text: str) -> str:
    """Naukri's own slug spelling.

    A dot becomes '-dot-' ("react.js" -> "react-dot-js"). Getting this wrong
    makes Naukri 301 to the canonical slug and DROP every query parameter, so
    the experience / freshness filters are silently lost.
    """
    text = (text or "").strip().lower().replace(".", "-dot-")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def naukri_job_id(url: str) -> str:
    """Naukri job URLs end with a long numeric id."""
    match = JOB_ID_RE.search(url or "")
    if match:
        return match.group(1)
    return re.sub(r"[^a-zA-Z0-9]", "", (url or "").split("?")[0])[-24:]


def build_search_url(
    settings: SearchSettings, keyword: str, location: str, page_number: int = 1
) -> str:
    """Naukri search URL: /<keyword>-jobs-in-<location>-<page>?k=..&l=..&experience=.."""
    keyword_slug = _slug(keyword)
    city = location.split(",")[0].strip()
    # "Remote" is not a Naukri city: it is the work-from-home filter instead.
    remote = city.lower() in ("remote", "work from home", "wfh", "anywhere")
    location_slug = "" if remote else _slug(city)

    path = keyword_slug + "-jobs"
    if location_slug:
        path += "-in-" + location_slug

    params = [
        "k=" + quote_plus(keyword.lower()),
        "experience=" + str(settings.experience_min_years),
        # sort=f is Naukri's "freshness" (most recent) ordering. sort=date is
        # not a valid value and silently returns an empty result set.
        "sort=f" if settings.sort_by.upper() == "DD" else "sort=r",
    ]
    if location_slug:
        params.insert(1, "l=" + quote_plus(city.lower()))
    if settings.date_posted.startswith("r"):
        try:
            days = max(1, int(settings.date_posted[1:]) // 86400)
            params.append("jobAge=" + str(days))
        except ValueError:
            pass
    if settings.remote_only or remote:
        params.append("wfhType=2")   # 2 = work from home
    # NOTE: jobType=1 ("full time") is deliberately NOT sent - on Naukri it
    # collapses the result set to a handful of unrelated postings.
    if page_number > 1:
        # Pagination must be a query parameter: the "-2" slug suffix triggers
        # a redirect that strips the other filters.
        params.append("pageNo=" + str(page_number))

    return BASE + path + "?" + "&".join(params)


class NaukriSearch:
    CARD_SELECTORS = (
        "div.srp-jobtuple-wrapper",
        "article.jobTuple",
        "div.cust-job-tuple",
    )

    def __init__(self, page: Page, settings: SearchSettings) -> None:
        self.page = page
        self.settings = settings

    def _check_restrictions(self) -> None:
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            return
        for marker in RESTRICTION_MARKERS:
            if marker in body:
                raise RuntimeError("Naukri reported '" + marker + "' - stopping this run.")

    def _cards(self) -> List[Job]:
        jobs: List[Job] = []
        seen: set = set()

        for card_selector in self.CARD_SELECTORS:
            cards = self.page.locator(card_selector)
            count = cards.count()
            if count == 0:
                continue

            for index in range(min(count, 40)):
                card = cards.nth(index)
                try:
                    link = card.locator("a.title, a[class*='title']").first
                    if link.count() == 0:
                        continue
                    href = link.get_attribute("href") or ""
                    if not href:
                        continue
                    url = href.split("?")[0]
                    job_id = naukri_job_id(href)
                    if not job_id or job_id in seen:
                        continue
                    seen.add(job_id)

                    title = (link.inner_text(timeout=2000) or "").strip()
                    company = location = experience = ""
                    for selector, target in (
                        ("a.comp-name, a[class*='comp-name'], span.comp-name", "company"),
                        ("span.locWdth, span[class*='loc'], li.location", "location"),
                        ("span.expwdth, span[class*='exp'], li.experience", "experience"),
                    ):
                        try:
                            node = card.locator(selector).first
                            if node.count():
                                text = (node.inner_text(timeout=1200) or "").strip()
                                if target == "company":
                                    company = text
                                elif target == "location":
                                    location = text
                                else:
                                    experience = text
                        except Exception:
                            continue

                    job = Job(
                        job_id=job_id,
                        platform="naukri",
                        title=title,
                        company=company,
                        location=location,
                        url=url,
                        employment_type="Full-time" if "F" in self.settings.job_types else "",
                    )
                    if experience:
                        job.required_experience_years = extract_required_experience(
                            experience + " years experience"
                        )
                    jobs.append(job)
                except Exception as exc:
                    log.debug("Unreadable Naukri card %d: %s", index, exc)
                    continue
            break  # first selector that produced cards wins
        return jobs

    def search(self, keyword: str, location: str) -> Iterator[Job]:
        for page_number in range(1, max(1, self.settings.pages_per_search) + 1):
            url = build_search_url(self.settings, keyword, location, page_number)
            log.info("Naukri page %d | '%s' in '%s'", page_number, keyword, location)
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except PWTimeout:
                log.warning("Naukri search timed out")
                return

            self._check_restrictions()
            try:
                self.page.wait_for_selector(
                    ", ".join(self.CARD_SELECTORS) + ", div.styles_no-result__wrapper__UCBxs",
                    timeout=20000,
                )
            except PWTimeout:
                log.info("No Naukri results rendered for '%s'", keyword)
                return

            human_pause(1.0, 2.0)
            cards = self._cards()
            log.info("Found %d Naukri cards", len(cards))
            if not cards:
                return
            for job in cards:
                job.keyword = keyword
                job.search_location = location
                yield job
            human_pause(2.0, 4.0)


class NaukriParser:
    """Enrich a Naukri job with its full description and experience range."""

    def __init__(self, page: Page) -> None:
        self.page = page

    def parse(self, job: Job) -> Optional[Job]:
        try:
            self.page.goto(job.url, wait_until="domcontentloaded")
        except PWTimeout:
            log.warning("Timed out opening Naukri job %s", job.job_id)
            return None

        try:
            self.page.wait_for_selector("h1, section.styles_job-desc-container__txpYf", timeout=15000)
        except PWTimeout:
            return None
        human_pause(0.8, 1.5)

        def first_text(selectors) -> str:
            for selector in selectors:
                try:
                    node = self.page.locator(selector).first
                    if node.count():
                        text = (node.inner_text(timeout=2500) or "").strip()
                        if text:
                            return re.sub(r"\s+", " ", text)
                except Exception:
                    continue
            return ""

        job.title = first_text(("h1[class*='jd-header-title']", "h1")) or job.title
        job.company = first_text((
            "div[class*='jd-header-comp-name'] a",
            "div[class*='jd-header-comp-name']",
        )) or job.company
        job.location = first_text((
            "div[class*='jhc__loc'] span", "span[class*='loc']",
        )) or job.location
        experience = first_text(("div[class*='jhc__exp'] span", "div[class*='exp']"))
        job.description = first_text((
            "div[class*='JDC__dang-inner-html']",
            "section[class*='job-desc-container']",
            "div.dang-inner-html",
        ))
        job.employment_type = job.employment_type or "Full-time"

        job.required_experience_years = (
            extract_required_experience(experience + " years")
            or extract_required_experience(job.description)
        )
        log.info("Parsed Naukri: %s @ %s | exp=%s", job.title, job.company,
                 job.required_experience_years)
        return job


class NaukriApply:
    """Click Apply and, when it opens, drive the chatbot questionnaire."""

    MAX_CHAT_TURNS = 15

    def __init__(self, page: Page, question_handler: QuestionHandler, auto_submit: bool) -> None:
        self.page = page
        self.questions = question_handler
        self.auto_submit = auto_submit
        self.answered: List[Dict[str, Any]] = []

    # -- button discovery --------------------------------------------------- #
    def _apply_button(self) -> Optional[Locator]:
        for selector in (
            "button#apply-button",
            "button[class*='apply-button']",
            "span#apply-button",
        ):
            node = self.page.locator(selector).first
            try:
                if node.count() and node.is_visible(timeout=2500):
                    return node
            except Exception:
                continue
        return None

    def _company_site_button(self) -> Optional[Locator]:
        for selector in (
            "button[class*='company-site-button']",
            "button#company-site-button",
        ):
            node = self.page.locator(selector).first
            try:
                if node.count() and node.is_visible(timeout=1500):
                    return node
            except Exception:
                continue
        return None

    def _already_applied(self) -> bool:
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            return False
        return "you have already applied" in body or "applied on" in body

    def _success(self) -> str:
        for selector in (
            "div.apply-message",
            "span[class*='apply-message']",
            "div[class*='styles_success']",
            "div.toast-message",
        ):
            try:
                node = self.page.locator(selector).first
                if node.count() and node.is_visible(timeout=2000):
                    text = (node.inner_text(timeout=1500) or "").strip()
                    if text:
                        return text[:200]
            except Exception:
                continue
        try:
            body = (self.page.inner_text("body") or "").lower()
        except Exception:
            return ""
        if "successfully applied" in body or "application sent" in body:
            return "Successfully applied"
        return ""

    # -- chatbot ------------------------------------------------------------ #
    def _chatbot(self) -> Optional[Locator]:
        for selector in (
            "div[class*='chatbot_DrawerContentWrapper']",
            "div[class*='chatbot_Drawer']",
            "div.chatbot_MessageContainer",
        ):
            node = self.page.locator(selector).first
            try:
                if node.count() and node.is_visible(timeout=2500):
                    return node
            except Exception:
                continue
        return None

    def _last_question(self, chatbot: Locator) -> str:
        for selector in (
            "div[class*='botItem'] div[class*='botMsg']",
            "div[class*='botItem']",
            "li.botItem span",
        ):
            try:
                nodes = chatbot.locator(selector)
                count = nodes.count()
                if count:
                    text = (nodes.nth(count - 1).inner_text(timeout=2000) or "").strip()
                    if text:
                        return re.sub(r"\s+", " ", text)
            except Exception:
                continue
        return ""

    def _chip_options(self, chatbot: Locator) -> List[str]:
        options: List[str] = []
        for selector in (
            "div[class*='ssrc__radio-btn-container'] label",
            "div[class*='chatbot_Chip'] span",
            "div.singleselect-radiobutton-container label",
        ):
            try:
                nodes = chatbot.locator(selector)
                for index in range(min(nodes.count(), 12)):
                    text = (nodes.nth(index).inner_text(timeout=800) or "").strip()
                    if text and text not in options:
                        options.append(text)
                if options:
                    return options
            except Exception:
                continue
        return options

    def _send(self, chatbot: Locator) -> None:
        for selector in (
            "div[class*='sendMsg']",
            "div.send-message",
            "button[class*='send']",
        ):
            try:
                node = chatbot.locator(selector).first
                if node.count() and node.is_visible(timeout=1500):
                    node.click(timeout=3000)
                    return
            except Exception:
                continue
        try:
            self.page.keyboard.press("Enter")
        except Exception:
            pass

    def _answer_chat_step(self, chatbot: Locator) -> Optional[bool]:
        """Answer one chatbot question.

        Returns True when answered, False when the question is unanswerable,
        and None when no question is pending any more.
        """
        label = self._last_question(chatbot)
        if not label:
            return None

        options = self._chip_options(chatbot)
        field_type = "select" if options else "text"
        question = Question(label=label, field_type=field_type, options=options)
        answer = self.questions.answer(question)
        if answer is None:
            log.warning("Naukri chatbot asked something unsupported: %s", label)
            return False

        if options:
            clicked = False
            for selector in (
                "div[class*='ssrc__radio-btn-container'] label",
                "div[class*='chatbot_Chip'] span",
            ):
                try:
                    nodes = chatbot.locator(selector)
                    for index in range(nodes.count()):
                        node = nodes.nth(index)
                        text = (node.inner_text(timeout=800) or "").strip().lower()
                        if text == answer.value.strip().lower():
                            node.click(timeout=3000)
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    continue
            if not clicked:
                return False
        else:
            try:
                box = chatbot.locator(
                    "div[contenteditable='true'], textarea, input[type='text']"
                ).first
                box.click(timeout=3000)
                box.fill("")
                box.type(answer.value, delay=30)
            except Exception as exc:
                log.warning("Could not type chatbot answer: %s", exc)
                return False

        self.answered.append({
            "question": label, "field_type": field_type,
            "answer": answer.value, "source": answer.source,
        })
        self._send(chatbot)
        human_pause(1.0, 2.0)
        return True

    # -- main --------------------------------------------------------------- #
    def apply(self, job_label: str = "") -> ApplyOutcome:
        if self._already_applied():
            return ApplyOutcome(
                status=ApplicationStatus.SKIPPED_ALREADY_APPLIED,
                confirmation="Naukri reports this job as already applied",
            )

        if self._apply_button() is None and self._company_site_button() is not None:
            return ApplyOutcome(
                status=ApplicationStatus.NEEDS_HUMAN,
                error="Applies on the company website - finish it yourself",
            )

        button = self._apply_button()
        if button is None:
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                error="No Apply button found on the Naukri job page",
            )

        if not self.auto_submit:
            # Nothing is clicked in dry-run mode: on Naukri the first click can
            # itself submit the application.
            return ApplyOutcome(
                status=ApplicationStatus.PREPARED_NOT_SUBMITTED,
                confirmation="Apply button located; AUTO_SUBMIT is off so nothing was clicked",
            )

        try:
            button.scroll_into_view_if_needed(timeout=2000)
            button.click(timeout=6000)
        except Exception as exc:
            return ApplyOutcome(
                status=ApplicationStatus.FAILED, error="Apply click failed: " + str(exc)[:200]
            )
        human_pause(1.5, 2.5)

        confirmation = self._success()
        if confirmation:
            log.info("Naukri SUBMITTED (instant): %s", job_label)
            return ApplyOutcome(status=ApplicationStatus.SUBMITTED, confirmation=confirmation)

        chatbot = self._chatbot()
        if chatbot is None:
            confirmation = self._success()
            if confirmation:
                return ApplyOutcome(status=ApplicationStatus.SUBMITTED, confirmation=confirmation)
            return ApplyOutcome(
                status=ApplicationStatus.NEEDS_HUMAN,
                error="Apply clicked but no confirmation or questionnaire appeared",
                questions=self.answered,
            )

        for turn in range(self.MAX_CHAT_TURNS):
            chatbot = self._chatbot()
            if chatbot is None:
                break
            result = self._answer_chat_step(chatbot)
            if result is None:
                break
            if result is False:
                return ApplyOutcome(
                    status=ApplicationStatus.NEEDS_HUMAN,
                    error="Chatbot question could not be answered from the profile",
                    questions=self.answered,
                )
            confirmation = self._success()
            if confirmation:
                log.info("Naukri SUBMITTED after %d chat turns: %s", turn + 1, job_label)
                return ApplyOutcome(
                    status=ApplicationStatus.SUBMITTED,
                    confirmation=confirmation,
                    questions=self.answered,
                )

        confirmation = self._success()
        if confirmation:
            return ApplyOutcome(
                status=ApplicationStatus.SUBMITTED,
                confirmation=confirmation,
                questions=self.answered,
            )
        return ApplyOutcome(
            status=ApplicationStatus.NEEDS_HUMAN,
            error="Questionnaire did not finish within " + str(self.MAX_CHAT_TURNS) + " turns",
            questions=self.answered,
        )


if __name__ == "__main__":  # python -m automation.naukri
    from config import load_settings

    settings = load_settings()
    print(build_search_url(settings.search, "React.js Developer", "Gurugram, Haryana, India"))
    print(build_search_url(settings.search, "Node.js Developer", "Remote", page_number=2))
    assert naukri_job_id("https://www.naukri.com/job-listings-react-dev-acme-310125904567") \
        == "310125904567"
    print("naukri url builder self test OK")

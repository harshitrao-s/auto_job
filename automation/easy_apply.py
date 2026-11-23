"""LinkedIn Easy Apply workflow.

The modal is a multi-step wizard whose length is unknown up front, so this is
written as a bounded state machine:

    open modal
    loop (max MAX_STEPS):
        upload resume if this step asks for one
        answer every field on this step
        if anything is unanswerable -> discard the draft, report NEEDS_HUMAN
        validate -> if errors remain, discard and report
        click Next / Review / Submit
    Submit is only clicked when AUTO_SUBMIT is on and every step validated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from automation.browser import human_pause
from automation.form_handler import FormHandler
from database.models import ApplicationStatus
from services.question_handler import QuestionHandler
from utils.logger import get_logger

log = get_logger(__name__)

MAX_STEPS = 12

MODAL_SELECTORS = (
    # Current UI: a native <dialog> element, not a div with role="dialog".
    "dialog[open]",
    "dialog",
    "div[role='dialog']",
    "div[aria-modal='true']",
    "div.jobs-easy-apply-modal",
    "div[data-test-modal][role='dialog']",
    "div.artdeco-modal[role='dialog']",
)


@dataclass
class ApplyOutcome:
    status: ApplicationStatus
    confirmation: str = ""
    error: str = ""
    questions: List[Dict[str, Any]] = field(default_factory=list)
    unanswered: List[str] = field(default_factory=list)


class EasyApply:
    def __init__(
        self,
        page: Page,
        question_handler: QuestionHandler,
        resume_path: Path,
        auto_submit: bool,
    ) -> None:
        self.page = page
        self.question_handler = question_handler
        self.resume_path = Path(resume_path)
        self.auto_submit = auto_submit

    # ------------------------------------------------------------------ #
    # modal helpers
    # ------------------------------------------------------------------ #
    def _modal(self) -> Optional[Locator]:
        for selector in MODAL_SELECTORS:
            node = self.page.locator(selector).first
            try:
                if node.count() and node.is_visible(timeout=2000):
                    return node
            except Exception:
                continue
        return None

    def _open_modal(self) -> bool:
        for selector in (
            "button.jobs-apply-button",
            "button[aria-label*='Easy Apply' i]",
        ):
            try:
                button = self.page.locator(selector).first
                if button.count() == 0:
                    continue
                if not button.is_visible(timeout=2500):
                    continue
                button.scroll_into_view_if_needed(timeout=2000)
                button.click(timeout=5000)
                human_pause(1.2, 2.2)
                if self._modal():
                    return True
            except Exception as exc:
                log.debug("Easy Apply button click failed (%s): %s", selector, exc)
                continue
        return False

    def _button(self, modal: Locator, *patterns: str) -> Optional[Locator]:
        """Find a footer button whose label matches any of `patterns`."""
        buttons = modal.locator("button")
        for index in range(buttons.count()):
            node = buttons.nth(index)
            try:
                if not node.is_visible(timeout=600):
                    continue
                label = ((node.inner_text(timeout=600) or "") + " " +
                         (node.get_attribute("aria-label") or "")).strip().lower()
            except Exception:
                continue
            if any(pattern in label for pattern in patterns):
                return node
        return None

    def _discard(self) -> None:
        """Close the modal and discard the draft so nothing half-filled is left."""
        modal = self._modal()
        if not modal:
            return
        try:
            close = self._button(modal, "dismiss", "close")
            if close:
                close.click(timeout=3000)
            else:
                self.page.keyboard.press("Escape")
            human_pause(0.6, 1.2)

            # LinkedIn asks "Save this application?" -> choose Discard.
            discard = self.page.locator(
                "button[data-control-name='discard_application_confirm_btn'], "
                "button[data-test-dialog-secondary-btn]"
            ).first
            if discard.count() and discard.is_visible(timeout=2500):
                discard.click(timeout=3000)
            else:
                for index in range(self.page.locator("button").count()):
                    node = self.page.locator("button").nth(index)
                    try:
                        if "discard" in (node.inner_text(timeout=400) or "").lower():
                            node.click(timeout=2000)
                            break
                    except Exception:
                        continue
        except Exception as exc:
            log.debug("Discard cleanup failed: %s", exc)
        human_pause(0.5, 1.0)

    def _confirmation_text(self) -> str:
        for selector in (
            "dialog h2",
            "dialog h3",
            "div.artdeco-modal h2",
            "div.jobs-easy-apply-confirmation",
            "div[data-test-modal] h3",
            "p.artdeco-inline-feedback__message",
        ):
            try:
                node = self.page.locator(selector).first
                if node.count() and node.is_visible(timeout=1500):
                    text = (node.inner_text(timeout=1500) or "").strip()
                    if text:
                        return text[:200]
            except Exception:
                continue
        return "Application submitted"

    def _close_confirmation(self) -> None:
        try:
            for selector in (
                "button[aria-label*='Dismiss' i]",
                "button[aria-label*='Done' i]",
                "button.artdeco-modal__dismiss",
            ):
                node = self.page.locator(selector).first
                if node.count() and node.is_visible(timeout=1500):
                    node.click(timeout=3000)
                    return
            self.page.keyboard.press("Escape")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # main workflow
    # ------------------------------------------------------------------ #
    def apply(self, job_label: str = "") -> ApplyOutcome:
        form = FormHandler(self.page, self.question_handler)

        if not self._open_modal():
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                error="Easy Apply modal did not open",
                questions=form.answered,
            )

        for step in range(1, MAX_STEPS + 1):
            modal = self._modal()
            if not modal:
                # The modal vanished: either submitted or closed unexpectedly.
                log.warning("Modal disappeared at step %d", step)
                return ApplyOutcome(
                    status=ApplicationStatus.FAILED,
                    error="Modal closed unexpectedly at step " + str(step),
                    questions=form.answered,
                )

            log.info("Easy Apply step %d for %s", step, job_label)

            if not form.upload_resume(modal, self.resume_path):
                self._discard()
                return ApplyOutcome(
                    status=ApplicationStatus.FAILED,
                    error="Resume upload failed",
                    questions=form.answered,
                )

            complete, unknown = form.fill(modal)
            if not complete:
                log.warning("Unanswerable question(s): %s", "; ".join(unknown))
                self._discard()
                return ApplyOutcome(
                    status=ApplicationStatus.NEEDS_HUMAN,
                    error="Unanswered: " + "; ".join(unknown)[:400],
                    questions=form.answered,
                    unanswered=unknown,
                )

            errors = form.validation_errors(modal)
            if errors:
                log.warning("Validation errors: %s", "; ".join(errors))
                self._discard()
                return ApplyOutcome(
                    status=ApplicationStatus.NEEDS_HUMAN,
                    error="Validation: " + "; ".join(errors)[:400],
                    questions=form.answered,
                    unanswered=errors,
                )

            submit = self._button(modal, "submit application", "submit")
            if submit:
                if not self.auto_submit:
                    log.info("AUTO_SUBMIT is off - form is complete, discarding draft")
                    self._discard()
                    return ApplyOutcome(
                        status=ApplicationStatus.PREPARED_NOT_SUBMITTED,
                        confirmation="Form completed and validated; not submitted",
                        questions=form.answered,
                    )
                submit.click(timeout=8000)
                human_pause(2.0, 3.5)
                confirmation = self._confirmation_text()
                self._close_confirmation()
                log.info("SUBMITTED: %s", job_label)
                return ApplyOutcome(
                    status=ApplicationStatus.SUBMITTED,
                    confirmation=confirmation,
                    questions=form.answered,
                )

            advance = self._button(modal, "review", "next", "continue")
            if not advance:
                self._discard()
                return ApplyOutcome(
                    status=ApplicationStatus.FAILED,
                    error="No Next/Review/Submit button at step " + str(step),
                    questions=form.answered,
                )

            try:
                advance.click(timeout=8000)
            except PWTimeout:
                self._discard()
                return ApplyOutcome(
                    status=ApplicationStatus.FAILED,
                    error="Could not advance past step " + str(step),
                    questions=form.answered,
                )
            human_pause(1.2, 2.4)

        self._discard()
        return ApplyOutcome(
            status=ApplicationStatus.NEEDS_HUMAN,
            error="Form exceeded " + str(MAX_STEPS) + " steps",
            questions=form.answered,
        )

"""Generic form discovery, filling and validation.

Works on whatever container is handed to it (the Easy Apply modal, a Naukri
chatbot step, ...). The strategy is deliberately DOM-shape driven rather than
selector-driven, because LinkedIn renames its CSS classes often:

  * every <input>, <textarea> and <select> inside the container is a field
  * its question text is the associated <label>, else the nearest preceding
    legend/heading, else the aria-label/placeholder
  * radios and checkboxes are grouped by their fieldset

Nothing is typed into a field unless the QuestionHandler produced an answer
that is backed by profile.yaml or by the human.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Locator, TimeoutError as PWTimeout

from automation.browser import human_pause
from services.question_handler import Answer, Question, QuestionHandler
from utils.logger import get_logger

log = get_logger(__name__)

ERROR_SELECTORS = (
    "div.artdeco-inline-feedback--error",
    "p.fb-dash-form-element__error-text",
    "span.artdeco-inline-feedback__message",
    "div[role='alert']",
    "span.error",
)


@dataclass
class FormField:
    locator: Locator
    kind: str                    # text | textarea | number | select | radio | checkbox | file
    label: str
    options: List[str] = dc_field(default_factory=list)
    required: bool = True
    element_id: str = ""

    def to_question(self) -> Question:
        kind = self.kind
        if kind in ("radio", "checkbox"):
            question_type = "boolean" if set(
                o.strip().lower() for o in self.options
            ) <= {"yes", "no"} else "select"
        elif kind == "select":
            question_type = "select"
        elif kind == "number":
            question_type = "number"
        else:
            question_type = "text"
        return Question(
            label=self.label,
            field_type=question_type,
            options=self.options,
            required=self.required,
        )


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    # Strip LinkedIn's duplicated label/aria text and trailing required markers.
    return re.sub(r"\s*\*$", "", text)


class FormHandler:
    def __init__(self, page, question_handler: QuestionHandler) -> None:
        self.page = page
        self.questions = question_handler
        self.answered: List[Dict[str, Any]] = []
        self.unanswered: List[str] = []

    # ------------------------------------------------------------------ #
    # label resolution
    # ------------------------------------------------------------------ #
    def _label_for(self, element: Locator) -> str:
        """Find the human-readable question text for one input element."""
        try:
            element_id = element.get_attribute("id") or ""
        except Exception:
            element_id = ""

        if element_id:
            try:
                # CSS.escape equivalent for ids containing odd characters.
                label = self.page.locator("label[for='" + element_id.replace("'", "\\'") + "']").first
                if label.count():
                    text = _clean(label.inner_text(timeout=1500))
                    if text:
                        return text
            except Exception:
                pass

        for getter in ("aria-label", "placeholder", "name", "title"):
            try:
                value = element.get_attribute(getter)
                if value and len(value) > 2:
                    return _clean(value)
            except Exception:
                continue

        # Nearest enclosing group's heading / legend.
        for xpath in (
            "xpath=ancestor::fieldset[1]//legend",
            "xpath=ancestor::*[contains(@class,'form-element')][1]//label",
            "xpath=preceding::label[1]",
        ):
            try:
                node = element.locator(xpath).first
                if node.count():
                    text = _clean(node.inner_text(timeout=1500))
                    if text:
                        return text
            except Exception:
                continue
        return ""

    def _is_required(self, element: Locator) -> bool:
        try:
            if element.get_attribute("required") is not None:
                return True
            if (element.get_attribute("aria-required") or "").lower() == "true":
                return True
        except Exception:
            pass
        return True  # default to required: safer, we try to answer everything

    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    def discover(self, container: Locator) -> List[FormField]:
        fields: List[FormField] = []
        seen_groups: set = set()

        # --- selects ---
        selects = container.locator("select")
        for index in range(selects.count()):
            node = selects.nth(index)
            options = []
            try:
                raw = node.locator("option").all_inner_texts()
                options = [_clean(o) for o in raw if _clean(o) and
                           _clean(o).lower() not in ("select an option", "select", "-")]
            except Exception:
                pass
            fields.append(
                FormField(locator=node, kind="select", label=self._label_for(node),
                          options=options, required=self._is_required(node))
            )

        # --- radio groups (one field per fieldset) ---
        radios = container.locator("input[type='radio']")
        for index in range(radios.count()):
            node = radios.nth(index)
            try:
                group = node.get_attribute("name") or ""
            except Exception:
                group = ""
            if group and group in seen_groups:
                continue
            seen_groups.add(group)

            options: List[str] = []
            label = ""
            try:
                fieldset = node.locator("xpath=ancestor::fieldset[1]").first
                if fieldset.count():
                    legend = fieldset.locator("legend").first
                    if legend.count():
                        label = _clean(legend.inner_text(timeout=1500))
                    for option_label in fieldset.locator("label").all_inner_texts():
                        cleaned = _clean(option_label)
                        if cleaned and cleaned != label:
                            options.append(cleaned)
            except Exception:
                pass
            if not label:
                label = self._label_for(node)
            fields.append(
                FormField(locator=node, kind="radio", label=label,
                          options=options or ["Yes", "No"], element_id=group)
            )

        # --- checkboxes ---
        checkboxes = container.locator("input[type='checkbox']")
        for index in range(checkboxes.count()):
            node = checkboxes.nth(index)
            fields.append(
                FormField(locator=node, kind="checkbox", label=self._label_for(node),
                          options=["Yes", "No"], required=self._is_required(node))
            )

        # --- textareas ---
        textareas = container.locator("textarea")
        for index in range(textareas.count()):
            node = textareas.nth(index)
            fields.append(
                FormField(locator=node, kind="textarea", label=self._label_for(node),
                          required=self._is_required(node))
            )

        # --- text/number inputs ---
        inputs = container.locator(
            "input[type='text'], input[type='tel'], input[type='email'], "
            "input[type='number'], input:not([type])"
        )
        for index in range(inputs.count()):
            node = inputs.nth(index)
            try:
                input_type = (node.get_attribute("type") or "text").lower()
            except Exception:
                input_type = "text"
            kind = "number" if input_type == "number" else "text"
            fields.append(
                FormField(locator=node, kind=kind, label=self._label_for(node),
                          required=self._is_required(node))
            )

        named = [f for f in fields if f.label]
        log.debug("Discovered %d fields (%d labelled)", len(fields), len(named))
        return named

    # ------------------------------------------------------------------ #
    # filling
    # ------------------------------------------------------------------ #
    def _current_value(self, field: FormField) -> str:
        """Current value of a field; '' for radios/checkboxes and on error."""
        try:
            if field.kind in ("radio", "checkbox"):
                return ""
            if field.kind == "select":
                # input_value() returns the option's value attribute, which is
                # often an opaque id - read the selected label instead.
                label = field.locator.evaluate(
                    "el => el.selectedIndex >= 0 ? el.options[el.selectedIndex].text : ''"
                )
                return (label or "").strip()
            return field.locator.input_value(timeout=1500) or ""
        except Exception:
            return ""

    def _fill_text(self, field: FormField, value: str) -> bool:
        try:
            field.locator.scroll_into_view_if_needed(timeout=2000)
            field.locator.click(timeout=3000)
            field.locator.fill("")
            field.locator.type(value, delay=35)
            human_pause(0.2, 0.5)

            # Location/typeahead inputs show a dropdown that must be accepted.
            if any(word in field.label.lower() for word in ("city", "location")):
                try:
                    option = self.page.locator(
                        "div.basic-typeahead__triggered-content div[role='option'], "
                        "ul[role='listbox'] li"
                    ).first
                    if option.count() and option.is_visible(timeout=2000):
                        option.click(timeout=2000)
                except Exception:
                    pass
            return True
        except Exception as exc:
            log.warning("Could not type into '%s': %s", field.label, exc)
            return False

    def _fill_select(self, field: FormField, value: str) -> bool:
        try:
            field.locator.scroll_into_view_if_needed(timeout=2000)
            field.locator.select_option(label=value, timeout=3000)
            return True
        except Exception:
            try:
                field.locator.select_option(value=value, timeout=3000)
                return True
            except Exception as exc:
                log.warning("Could not select '%s' for '%s': %s", value, field.label, exc)
                return False

    def _fill_radio(self, field: FormField, value: str) -> bool:
        """Click the radio whose label text matches the answer."""
        try:
            fieldset = field.locator.locator("xpath=ancestor::fieldset[1]").first
            scope = fieldset if fieldset.count() else self.page
            wanted = value.strip().lower()

            labels = scope.locator("label")
            for index in range(labels.count()):
                label = labels.nth(index)
                text = _clean(label.inner_text(timeout=1000)).lower()
                if text == wanted or (text and text.startswith(wanted)):
                    label.scroll_into_view_if_needed(timeout=2000)
                    label.click(timeout=3000)
                    return True

            # Fall back to checking the input directly by value attribute.
            option = scope.locator("input[type='radio'][value='" + value + "']").first
            if option.count():
                option.check(timeout=3000)
                return True
        except Exception as exc:
            log.warning("Could not select radio '%s' for '%s': %s", value, field.label, exc)
        return False

    def _fill_checkbox(self, field: FormField, value: str) -> bool:
        try:
            should_check = value.strip().lower() in ("yes", "true", "1", "on")
            if should_check:
                field.locator.check(timeout=3000)
            else:
                field.locator.uncheck(timeout=3000)
            return True
        except Exception as exc:
            log.debug("Could not toggle checkbox '%s': %s", field.label, exc)
            return False

    def fill(self, container: Locator) -> Tuple[bool, List[str]]:
        """Answer every field in `container`.

        Returns (all_answered, unknown_question_labels). When a question cannot
        be answered from the profile or by the human, it is reported and the
        caller must abandon the application rather than guess.
        """
        unknown: List[str] = []
        fields = self.discover(container)

        for field in fields:
            if not field.label:
                continue

            label_lower = field.label.lower()

            # Resume chooser: LinkedIn lists previously uploaded files as radio
            # cards labelled with the filename. upload_resume() has already
            # attached (and thereby selected) the right file, so these are not
            # questions and must not be sent to the human.
            if field.kind in ("radio", "checkbox") and (
                re.search(r"\.(pdf|docx?|rtf)\b", label_lower)
                or label_lower.startswith("resume")
                or "choose resume" in label_lower
            ):
                log.debug("Skipping resume chooser field: %s", field.label)
                continue

            # Opt-in checkboxes (follow company, marketing) are left untouched.
            if field.kind == "checkbox" and any(
                word in label_lower
                for word in ("follow", "receive", "subscribe", "notify me", "updates from")
            ):
                continue

            existing = self._current_value(field)
            placeholder = existing.strip().lower() in (
                "", "select an option", "select", "-", "choose",
            )
            if not placeholder and field.kind in ("text", "textarea", "number", "select"):
                # LinkedIn pre-fills name/email/phone from the profile - keep it.
                self.answered.append({
                    "question": field.label, "field_type": field.kind,
                    "answer": existing, "source": "prefilled",
                })
                continue

            answer: Optional[Answer] = self.questions.answer(field.to_question())
            if answer is None:
                log.warning("No supported answer for: %s", field.label)
                unknown.append(field.label)
                continue

            ok = False
            if field.kind in ("text", "textarea", "number"):
                ok = self._fill_text(field, answer.value)
            elif field.kind == "select":
                ok = self._fill_select(field, answer.value)
            elif field.kind == "radio":
                ok = self._fill_radio(field, answer.value)
            elif field.kind == "checkbox":
                ok = self._fill_checkbox(field, answer.value)

            if ok:
                self.answered.append({
                    "question": field.label, "field_type": field.kind,
                    "answer": answer.value, "source": answer.source,
                })
            else:
                unknown.append(field.label)
            human_pause(0.2, 0.6)

        self.unanswered.extend(unknown)
        return (not unknown), unknown

    # ------------------------------------------------------------------ #
    # resume upload
    # ------------------------------------------------------------------ #
    def upload_resume(self, container: Locator, resume_path: Path) -> bool:
        """Attach the resume if this step offers an upload input.

        Returns True when a resume is attached OR an existing one is already
        selected; False only when an upload was expected but failed.
        """
        file_inputs = container.locator("input[type='file']")
        count = file_inputs.count()
        if count == 0:
            return True

        for index in range(count):
            node = file_inputs.nth(index)
            try:
                accept = (node.get_attribute("accept") or "").lower()
                name = ((node.get_attribute("name") or "") + " " +
                        (node.get_attribute("id") or "")).lower()
                if "cover" in name:          # cover letter upload - optional
                    continue
                if accept and not any(ext in accept for ext in ("pdf", "doc", "msword")):
                    continue
                node.set_input_files(str(resume_path), timeout=15000)
                log.info("Uploaded resume: %s", Path(resume_path).name)
                human_pause(1.5, 3.0)
                return True
            except Exception as exc:
                log.warning("Resume upload failed: %s", exc)
                return False
        return True

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #
    def validation_errors(self, container: Locator) -> List[str]:
        """Collect inline error messages plus still-empty required fields."""
        errors: List[str] = []
        for selector in ERROR_SELECTORS:
            try:
                nodes = container.locator(selector)
                for index in range(min(nodes.count(), 10)):
                    text = _clean(nodes.nth(index).inner_text(timeout=1000))
                    if text and text not in errors:
                        errors.append(text)
            except Exception:
                continue

        for field in self.discover(container):
            if field.kind in ("text", "textarea", "number") and field.required:
                if not self._current_value(field).strip():
                    message = "empty required field: " + field.label
                    if message not in errors:
                        errors.append(message)
        return errors

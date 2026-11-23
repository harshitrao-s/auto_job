"""Screening-question answering.

Resolution order for every question, strictest first:

    1. learned answers  (data/answers.json - previously confirmed by the human)
    2. rule bank        (profile.yaml -> answer_rules, regex driven)
    3. human prompt     (terminal, only when INTERACTIVE=true)
    4. UNKNOWN          -> the application is abandoned, never guessed

Nothing here fabricates experience, salary or personal data: every value is
either read from profile.yaml or typed by the human.

    python -m services.question_handler
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

UNKNOWN = object()  # sentinel: we genuinely do not know the answer


@dataclass
class Question:
    """A field discovered inside an Easy Apply form."""

    label: str
    field_type: str                    # text | number | boolean | select | radio | checkbox
    options: List[str]                 # for select/radio
    required: bool = True
    current_value: str = ""

    @property
    def key(self) -> str:
        """Stable, normalised key used for the learned-answer cache."""
        normalised = re.sub(r"\s+", " ", self.label.strip().lower())
        normalised = re.sub(r"[^a-z0-9 +/().-]", "", normalised)
        return normalised[:200]


@dataclass
class Answer:
    value: str
    source: str                        # rule | learned | human | profile
    confident: bool = True


def _skill_aliases(name: str) -> List[str]:
    name = name.lower().strip()
    base = {
        "react": ["react", "react.js", "reactjs"],
        "node": ["node", "node.js", "nodejs"],
        "next.js": ["next.js", "nextjs"],
        "javascript": ["javascript", "js"],
    }
    for group in base.values():
        if name in group:
            return group
    return [name]


class QuestionHandler:
    def __init__(
        self,
        profile: Dict[str, Any],
        answers_path: Path,
        interactive: bool = True,
    ) -> None:
        self.profile = profile or {}
        self.answers_path = Path(answers_path)
        self.interactive = interactive
        self.rules = self.profile.get("answer_rules", []) or []
        self.learned: Dict[str, str] = self._load_learned()
        self.asked_this_run: List[str] = []

    @classmethod
    def from_settings(cls, settings) -> "QuestionHandler":
        return cls(
            profile=settings.profile,
            answers_path=settings.paths.answers,
            interactive=settings.automation.interactive,
        )

    # -- learned answer cache ---------------------------------------------- #
    def _load_learned(self) -> Dict[str, str]:
        if not self.answers_path.exists():
            return {}
        try:
            with self.answers_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read learned answers (%s) - starting empty", exc)
            return {}

    def _save_learned(self) -> None:
        try:
            self.answers_path.parent.mkdir(parents=True, exist_ok=True)
            with self.answers_path.open("w", encoding="utf-8") as handle:
                json.dump(self.learned, handle, indent=2, sort_keys=True)
        except OSError as exc:
            log.warning("Could not persist learned answers: %s", exc)

    def remember(self, question: Question, value: str) -> None:
        self.learned[question.key] = value
        self._save_learned()

    # -- profile lookups ---------------------------------------------------- #
    def _profile_get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.profile
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _years_for_skill_in(self, label: str) -> Optional[float]:
        """'How many years of experience do you have with React?' -> 3.

        Picks the longest profile skill whose name (or alias) appears in the
        question, so 'react native' never silently answers for 'react'.
        """
        skills: Dict[str, Any] = self._profile_get("skills", {}) or {}
        label_l = label.lower()
        best: Optional[str] = None
        for skill in skills:
            for alias in _skill_aliases(skill):
                pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
                if re.search(pattern, label_l):
                    if best is None or len(alias) > len(best):
                        best = skill
        if best is None:
            return None
        try:
            return float(skills[best])
        except (TypeError, ValueError):
            return None

    def _resolve_rule_value(self, raw: Any, question: Question) -> Any:
        """Turn a rule's `value` into a concrete answer, or UNKNOWN."""
        if isinstance(raw, bool):
            return "Yes" if raw else "No"

        if isinstance(raw, str) and raw.startswith("profile:"):
            value = self._profile_get(raw.split(":", 1)[1])
            if value is None or str(value).strip() == "" or "<FILL ME>" in str(value):
                return UNKNOWN
            return value

        if raw == "auto_experience":
            years = self._years_for_skill_in(question.label)
            if years is None:
                # Generic "years of experience" question -> total experience.
                total = self._profile_get("professional.total_experience_years")
                return total if total is not None else UNKNOWN
            return years

        if raw is None:
            return UNKNOWN
        return raw

    # -- option matching ---------------------------------------------------- #
    @staticmethod
    def _match_option(value: str, options: List[str]) -> Optional[str]:
        """Map an answer onto one of the offered select/radio options."""
        if not options:
            return value
        wanted = str(value).strip().lower()
        for option in options:
            if option.strip().lower() == wanted:
                return option
        # Yes/No variants ("Yes, I am authorized").
        for option in options:
            lowered = option.strip().lower()
            if wanted in ("yes", "no") and lowered.startswith(wanted):
                return option
        for option in options:
            if wanted and wanted in option.strip().lower():
                return option
        return None

    # -- main entry point --------------------------------------------------- #
    def answer(self, question: Question) -> Optional[Answer]:
        """Return an Answer, or None when the question must go to a human."""
        # 1. previously confirmed answer
        if question.key in self.learned:
            value = self.learned[question.key]
            matched = self._match_option(value, question.options)
            if matched is not None:
                return Answer(value=str(matched), source="learned")

        # 2. rule bank
        for rule in self.rules:
            pattern = str(rule.get("match", ""))
            if not pattern:
                continue
            try:
                if not re.search(pattern, question.label.lower()):
                    continue
            except re.error as exc:
                log.warning("Bad regex in answer_rules (%s): %s", pattern, exc)
                continue

            resolved = self._resolve_rule_value(rule.get("value"), question)
            if resolved is UNKNOWN:
                log.info("Rule matched but profile has no value for: %s", question.label)
                break  # fall through to the human
            if isinstance(resolved, float) and resolved.is_integer():
                resolved = int(resolved)
            matched = self._match_option(str(resolved), question.options)
            if matched is None:
                log.info(
                    "Answer '%s' does not fit the offered options %s for: %s",
                    resolved, question.options, question.label,
                )
                break
            return Answer(value=str(matched), source="rule")

        # 3. a direct skill-years question that no rule covered
        if question.field_type == "number":
            years = self._years_for_skill_in(question.label)
            if years is not None and "year" in question.label.lower():
                return Answer(value=str(int(years)), source="profile")

        # 4. ask the human
        return self.ask_human(question)

    # -- human in the loop -------------------------------------------------- #
    def ask_human(self, question: Question) -> Optional[Answer]:
        if not self.interactive:
            log.warning("Unknown question (non-interactive): %s", question.label)
            self.asked_this_run.append(question.label)
            return None

        print("\n" + "=" * 72)
        print("UNKNOWN SCREENING QUESTION - human input needed")
        print("=" * 72)
        print("Question : " + question.label)
        print("Type     : " + question.field_type)
        if question.options:
            print("Options  : " + " | ".join(question.options))
        print("Enter an answer, or press Enter alone to SKIP this application.")
        try:
            raw = input("Answer   > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if not raw:
            self.asked_this_run.append(question.label)
            return None

        matched = self._match_option(raw, question.options)
        if matched is None:
            print("That answer does not match any available option - skipping this job.")
            self.asked_this_run.append(question.label)
            return None

        try:
            reuse = input("Remember this answer for identical questions? [Y/n] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            reuse = "n"
        if reuse in ("", "y", "yes"):
            self.remember(question, str(matched))

        return Answer(value=str(matched), source="human")


if __name__ == "__main__":  # python -m services.question_handler
    import tempfile

    import yaml

    profile_path = Path(__file__).resolve().parent.parent / "config" / "profile.yaml"
    with profile_path.open("r", encoding="utf-8") as handle:
        profile = yaml.safe_load(handle)

    tmp = Path(tempfile.gettempdir()) / "qh_selftest.json"
    tmp.unlink(missing_ok=True)
    handler = QuestionHandler(profile, tmp, interactive=False)

    cases = [
        Question("How many years of work experience do you have with React?", "number", []),
        Question("How many years of work experience do you have with Kubernetes?", "number", []),
        Question("What is your notice period in days?", "number", []),
        Question("Are you legally authorized to work in India?", "radio", ["Yes", "No"]),
        Question("Will you now or in the future require sponsorship?", "radio", ["Yes", "No"]),
        Question("What is your current company?", "text", []),
        Question("What is your expected CTC?", "number", []),          # null -> unknown
        Question("Describe your favourite colour in 500 words", "text", []),  # unknown
    ]
    for question in cases:
        answer = handler.answer(question)
        rendered = (answer.value + "  [" + answer.source + "]") if answer else "NEEDS HUMAN"
        print(question.label[:58].ljust(60), "->", rendered)

    assert handler.answer(cases[0]).value == "3"
    assert handler.answer(cases[1]).value == "1"
    assert handler.answer(cases[2]).value == "30"
    assert handler.answer(cases[3]).value == "Yes"
    assert handler.answer(cases[4]).value == "No"
    assert handler.answer(cases[5]).value == "Booming Bulls"
    assert handler.answer(cases[6]) is None, "unset salary must NOT be invented"
    assert handler.answer(cases[7]) is None, "unknown question must go to a human"
    tmp.unlink(missing_ok=True)
    print("\nquestion_handler self test OK")

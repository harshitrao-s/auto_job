"""Resume selection and verification.

Responsibilities
----------------
* Validate that the configured resume exists, is a supported type and is small
  enough for LinkedIn (2 MB limit for Easy Apply uploads).
* Optionally pick a role-specific resume variant (e.g. `resume_frontend.pdf`)
  when one exists next to the default resume.
* Extract the resume's text so personal details can be cross-checked against
  profile.yaml - the resume stays the source of truth, and a mismatch is
  reported rather than silently overwritten.

    python -m services.resume_manager
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

log = get_logger(__name__)

MAX_RESUME_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".doc", ".docx"}

# Variant suffix -> keywords that should steer a job towards it.
VARIANT_HINTS = {
    "frontend": ("frontend", "front end", "front-end", "react", "next.js", "ui "),
    "fullstack": ("full stack", "fullstack", "mern", "backend", "node"),
    "backend": ("backend", "back end", "node.js", "api developer"),
}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{9}")


class ResumeError(RuntimeError):
    pass


class ResumeManager:
    def __init__(self, default_resume: Path) -> None:
        self.default_resume = Path(default_resume)
        self._text_cache: Optional[str] = None

    # -- validation --------------------------------------------------------- #
    def validate(self) -> None:
        path = self.default_resume
        if not path.exists():
            raise ResumeError("Resume not found: " + str(path))
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ResumeError(
                "Unsupported resume type '" + path.suffix + "'. Use PDF, DOC or DOCX."
            )
        size = path.stat().st_size
        if size == 0:
            raise ResumeError("Resume file is empty: " + str(path))
        if size > MAX_RESUME_BYTES:
            raise ResumeError(
                "Resume is " + str(round(size / 1024 / 1024, 2)) +
                " MB; LinkedIn Easy Apply rejects anything over 2 MB."
            )
        log.info("Resume OK: %s (%s KB)", path.name, round(size / 1024))

    # -- variant selection -------------------------------------------------- #
    def available_variants(self) -> Dict[str, Path]:
        """Find `resume_<variant>.pdf` files sitting next to the default one."""
        variants: Dict[str, Path] = {}
        stem = self.default_resume.stem
        for candidate in self.default_resume.parent.glob(stem + "_*"):
            if candidate.suffix.lower() in ALLOWED_SUFFIXES:
                variants[candidate.stem[len(stem) + 1:].lower()] = candidate
        return variants

    def pick_for_job(self, job_title: str = "", job_description: str = "") -> Path:
        """Choose the most appropriate resume file for a given job."""
        variants = self.available_variants()
        if not variants:
            return self.default_resume

        haystack = (job_title + " " + job_description[:3000]).lower()
        best_name, best_hits = None, 0
        for name, path in variants.items():
            hints = VARIANT_HINTS.get(name, (name,))
            hits = sum(1 for hint in hints if hint in haystack)
            if hits > best_hits:
                best_name, best_hits = name, hits

        if best_name:
            log.debug("Using resume variant '%s' for '%s'", best_name, job_title)
            return variants[best_name]
        return self.default_resume

    # -- text extraction ---------------------------------------------------- #
    def text(self) -> str:
        """Extract resume text (PDF only; other formats return '')."""
        if self._text_cache is not None:
            return self._text_cache

        text = ""
        if self.default_resume.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader  # optional dependency

                reader = PdfReader(str(self.default_resume))
                text = "\n".join((page.extract_text() or "") for page in reader.pages)
            except ImportError:
                log.warning("pypdf not installed - skipping resume text extraction")
            except Exception as exc:  # corrupt/encrypted PDF
                log.warning("Could not read resume text: %s", exc)
        self._text_cache = text
        return text

    def detected_contacts(self) -> Dict[str, str]:
        """Pull email/phone out of the resume for cross-checking."""
        text = self.text()
        email = EMAIL_RE.search(text)
        phone = PHONE_RE.search(text.replace(" ", " "))
        return {
            "email": email.group(0) if email else "",
            "phone": phone.group(0) if phone else "",
        }

    def cross_check(self, profile_email: str, profile_phone: str) -> List[str]:
        """Warn when profile.yaml disagrees with the resume (never auto-fix)."""
        warnings: List[str] = []
        found = self.detected_contacts()
        if found["email"] and profile_email and found["email"].lower() != profile_email.lower():
            warnings.append(
                "Resume email (" + found["email"] + ") differs from profile.yaml ("
                + profile_email + ")"
            )
        if found["phone"] and profile_phone:
            digits_resume = re.sub(r"\D", "", found["phone"])[-10:]
            digits_profile = re.sub(r"\D", "", profile_phone)[-10:]
            if digits_resume and digits_profile and digits_resume != digits_profile:
                warnings.append(
                    "Resume phone (" + found["phone"] + ") differs from profile.yaml ("
                    + profile_phone + ")"
                )
        return warnings


if __name__ == "__main__":  # python -m services.resume_manager
    from config import load_settings

    settings = load_settings()
    manager = ResumeManager(settings.paths.resume)
    try:
        manager.validate()
    except ResumeError as exc:
        print("Resume problem:", exc)
    else:
        print("Variants:", list(manager.available_variants()))
        print("Contacts in resume:", manager.detected_contacts())
        print("Chosen for a React job:", manager.pick_for_job("React Developer", "react next.js"))
        for warning in manager.cross_check(
            settings.profile_get("personal.email", ""),
            settings.profile_get("personal.phone", ""),
        ):
            print("WARNING:", warning)

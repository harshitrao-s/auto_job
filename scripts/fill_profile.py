"""Fill the <FILL ME> fields in config/profile.yaml from your resume.

Reads resume/resume.pdf, pulls out name / email / phone / LinkedIn / GitHub,
SHOWS you what it found, and only writes after you confirm each value. Nothing
is invented: a field it cannot find stays blank for you to type in.

    python scripts/fill_profile.py
    python scripts/fill_profile.py --show      # just print what it detected
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402
from services.resume_manager import ResumeManager  # noqa: E402

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}")
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w%-]+/?", re.I)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?", re.I)

# Words that mean a line is a heading, not a person's name.
NOT_A_NAME = (
    "resume", "curriculum", "vitae", "profile", "summary", "developer",
    "engineer", "contact", "objective", "experience", "skills",
)


def detect_name(text: str) -> Optional[str]:
    """The candidate's name is almost always the first real line of a resume."""
    for line in (text or "").splitlines()[:12]:
        candidate = line.strip()
        if not (4 <= len(candidate) <= 40):
            continue
        lowered = candidate.lower()
        if any(word in lowered for word in NOT_A_NAME):
            continue
        if EMAIL_RE.search(candidate) or re.search(r"\d", candidate):
            continue
        words = candidate.split()
        if 2 <= len(words) <= 4 and all(w[:1].isalpha() for w in words):
            return " ".join(w.capitalize() for w in words)
    return None


def detect(text: str) -> Dict[str, str]:
    email = EMAIL_RE.search(text)
    phone = PHONE_RE.search(text)
    linkedin = LINKEDIN_RE.search(text)
    github = GITHUB_RE.search(text)
    name = detect_name(text) or ""
    parts = name.split()
    return {
        "first_name": parts[0] if parts else "",
        "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
        "email": email.group(0) if email else "",
        "phone": re.sub(r"[\s-]", "", phone.group(0)) if phone else "",
        "linkedin": ("https://" + linkedin.group(0).lstrip("htps:/")) if linkedin else "",
        "github": ("https://" + github.group(0).lstrip("htps:/")) if github else "",
    }


def confirm(label: str, detected: str, current: str) -> str:
    """Show the detected value and let the user accept or replace it."""
    shown = detected or "(not found)"
    print("\n  " + label)
    print("    from resume : " + shown)
    if current and "<FILL ME>" not in current:
        print("    in profile  : " + current)
    try:
        typed = input("    Enter to accept, or type the correct value > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return typed or detected


def write_profile(path: Path, values: Dict[str, str]) -> int:
    """Replace values in profile.yaml line-wise so comments/order survive."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    in_personal = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("personal:"):
            in_personal = True
            continue
        if in_personal and line and not line[0].isspace() and stripped:
            break  # left the personal: block
        if not in_personal:
            continue

        for key, value in values.items():
            if not value:
                continue
            if stripped.startswith(key + ":"):
                indent = line[: len(line) - len(line.lstrip())]
                lines[index] = indent + key + ': "' + value.replace('"', "'") + '"\n'
                changed += 1

    path.write_text("".join(lines), encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill profile.yaml from your resume")
    parser.add_argument("--show", action="store_true", help="only print what was detected")
    args = parser.parse_args()

    settings = load_settings()
    resume_path = settings.paths.resume

    if not resume_path.exists():
        print("\nResume not found at: " + str(resume_path))
        print("Put your resume there first (PDF, under 2 MB), then re-run this.\n")
        return 2

    manager = ResumeManager(resume_path)
    text = manager.text()
    if not text.strip():
        print("\nCould not read any text from " + resume_path.name + ".")
        print("It is probably a scanned image. Fill config/profile.yaml by hand instead.\n")
        return 2

    found = detect(text)

    print("\n" + "=" * 70)
    print("DETECTED FROM " + resume_path.name)
    print("=" * 70)
    for key, value in found.items():
        print("  " + key.ljust(12) + ": " + (value or "(not found)"))

    if args.show:
        print("")
        return 0

    print("\nConfirm each value (Enter = accept, or type the correct one):")
    confirmed: Dict[str, str] = {}
    for key in ("first_name", "last_name", "email", "phone", "linkedin", "github"):
        current = str(settings.profile_get("personal." + key, "") or "")
        confirmed[key] = confirm(key.replace("_", " ").title(), found.get(key, ""), current)

    missing = [k for k in ("first_name", "last_name", "email", "phone") if not confirmed[k]]
    if missing:
        print("\nStill empty: " + ", ".join(missing))
        print("These are required - re-run and type them in.\n")
        return 2

    changed = write_profile(settings.paths.profile, confirmed)
    print("\nUpdated " + str(changed) + " fields in " + str(settings.paths.profile))

    problems = load_settings().validate()
    if problems:
        print("\nStill to fix:")
        for problem in problems:
            print("  - " + problem)
        print("")
        return 1

    print("\nAll set. Next:  python main.py --platform naukri --dry-run --max 3\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

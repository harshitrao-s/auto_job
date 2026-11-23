"""Central configuration.

Everything tunable lives here and is sourced from `.env` (automation controls)
and `config/profile.yaml` (who the candidate is).  Nothing is hardcoded in the
automation modules, and no credential is ever stored.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------- #
# env helpers
# --------------------------------------------------------------------------- #


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or value.strip() == "" else value.strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    return _str(key, str(default)).lower() in {"1", "true", "yes", "y", "on"}


def _list(key: str, default: str, sep: str = ",") -> List[str]:
    raw = _str(key, default)
    return [item.strip() for item in raw.split(sep) if item.strip()]


def _path(key: str, default: str) -> Path:
    raw = _str(key, default)
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (BASE_DIR / candidate)


# --------------------------------------------------------------------------- #
# settings objects
# --------------------------------------------------------------------------- #


@dataclass
class SearchSettings:
    keywords: List[str]
    locations: List[str]
    remote_only: bool
    experience_min_years: int
    experience_max_years: int
    job_types: List[str]          # LinkedIn codes: F full-time, C contract, P part-time
    easy_apply_only: bool
    sort_by: str                  # DD = most recent, R = most relevant
    date_posted: str              # r86400 / r604800 / "" for any time
    pages_per_search: int

    @property
    def experience_codes(self) -> List[str]:
        """Map a years range onto LinkedIn's f_E experience-level codes.

        1 internship | 2 entry level | 3 associate | 4 mid-senior | 5 director
        2-4 years of experience realistically sits across entry/associate/
        mid-senior, so we include every level the configured range overlaps.
        """
        codes: List[str] = []
        if self.experience_min_years <= 1:
            codes.append("2")
        if self.experience_min_years <= 4:
            codes.append("3")
        if self.experience_max_years >= 3:
            codes.append("4")
        return codes or ["3"]


@dataclass
class AutomationSettings:
    min_match_score: int
    max_applications_per_session: int
    max_jobs_to_process: int
    auto_submit: bool
    headless: bool
    interactive: bool
    max_consecutive_failures: int
    min_action_delay: float
    max_action_delay: float
    between_jobs_delay: float
    between_searches_delay: float


@dataclass
class BrowserSettings:
    user_data_dir: Path
    profile_directory: str
    channel: str
    headless: bool
    slow_mo_ms: int
    nav_timeout_ms: int
    screenshot_on_error: bool
    screenshot_dir: Path


@dataclass
class Paths:
    resume: Path
    profile: Path
    database: Path
    answers: Path
    logs: Path
    output: Path                      # CSV exports, written into date folders
    linkedin_cookies: Path
    naukri_cookies: Path


@dataclass
class Settings:
    platforms: List[str]              # linkedin and/or naukri
    search: SearchSettings
    automation: AutomationSettings
    browser: BrowserSettings
    paths: Paths
    log_level: str
    profile: Dict[str, Any] = field(default_factory=dict)

    # -- profile lookup ---------------------------------------------------- #
    def profile_get(self, dotted: str, default: Any = None) -> Any:
        """Read 'professional.current_company' style paths out of profile.yaml."""
        node: Any = self.profile
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def validate(self) -> List[str]:
        """Return a list of blocking problems (empty list == good to go)."""
        problems: List[str] = []

        if not self.paths.resume.exists():
            problems.append("Resume not found at " + str(self.paths.resume))
        if not self.paths.profile.exists():
            problems.append("Profile not found at " + str(self.paths.profile))
        if not self.search.keywords:
            problems.append("No search keywords configured (KEYWORDS)")
        if not self.search.locations:
            problems.append("No locations configured (LOCATIONS)")

        # Unfilled placeholders would mean guessing personal data - refuse.
        required = [
            "personal.first_name",
            "personal.last_name",
            "personal.email",
            "personal.phone",
        ]
        for dotted in required:
            value = self.profile_get(dotted)
            if not value or "<FILL ME>" in str(value):
                problems.append("profile.yaml -> " + dotted + " is not filled in")
        return problems


def load_settings() -> Settings:
    profile_path = _path("PROFILE_PATH", "config/profile.yaml")
    profile: Dict[str, Any] = {}
    if profile_path.exists():
        with profile_path.open("r", encoding="utf-8") as handle:
            profile = yaml.safe_load(handle) or {}

    user_data_dir = _str("CHROME_USER_DATA_DIR")
    browser = BrowserSettings(
        user_data_dir=Path(user_data_dir).expanduser()
        if user_data_dir
        else BASE_DIR / "browser_profile",
        profile_directory=_str("CHROME_PROFILE_DIRECTORY", "Default"),
        channel=_str("BROWSER_CHANNEL", "chrome"),
        headless=_bool("HEADLESS", False),
        slow_mo_ms=_int("SLOW_MO_MS", 120),
        nav_timeout_ms=_int("NAV_TIMEOUT_MS", 45000),
        screenshot_on_error=_bool("SCREENSHOT_ON_ERROR", True),
        screenshot_dir=BASE_DIR / "screenshots",
    )

    settings = Settings(
        platforms=[p.lower() for p in _list("PLATFORMS", "linkedin")],
        search=SearchSettings(
            keywords=_list("KEYWORDS", "Frontend Developer,React.js Developer"),
            locations=_list("LOCATIONS", "India", sep="|"),
            remote_only=_bool("REMOTE_ONLY", False),
            experience_min_years=_int("EXPERIENCE_MIN_YEARS", 2),
            experience_max_years=_int("EXPERIENCE_MAX_YEARS", 4),
            job_types=_list("JOB_TYPES", "F"),
            easy_apply_only=_bool("EASY_APPLY_ONLY", True),
            sort_by=_str("SORT_BY", "DD"),
            date_posted=_str("DATE_POSTED", "r604800"),
            pages_per_search=_int("PAGES_PER_SEARCH", 2),
        ),
        automation=AutomationSettings(
            min_match_score=_int("MIN_MATCH_SCORE", 60),
            max_applications_per_session=_int("MAX_APPLICATIONS_PER_SESSION", 10),
            max_jobs_to_process=_int("MAX_JOBS_TO_PROCESS", 60),
            auto_submit=_bool("AUTO_SUBMIT", False),
            headless=browser.headless,
            interactive=_bool("INTERACTIVE", True),
            max_consecutive_failures=_int("MAX_CONSECUTIVE_FAILURES", 3),
            min_action_delay=_float("MIN_ACTION_DELAY", 1.5),
            max_action_delay=_float("MAX_ACTION_DELAY", 4.0),
            between_jobs_delay=_float("BETWEEN_JOBS_DELAY", 8.0),
            between_searches_delay=_float("BETWEEN_SEARCHES_DELAY", 15.0),
        ),
        browser=browser,
        paths=Paths(
            resume=_path("RESUME_PATH", "resume/resume.pdf"),
            profile=profile_path,
            database=_path("DB_PATH", "data/applications.sqlite3"),
            answers=_path("ANSWERS_PATH", "data/answers.json"),
            logs=BASE_DIR / "logs",
            output=_path("OUTPUT_DIR", "output"),
            linkedin_cookies=_path("LINKEDIN_COOKIES_PATH", "cookies/linkedin_cookies.json"),
            naukri_cookies=_path("NAUKRI_COOKIES_PATH", "cookies/naukri_cookies.json"),
        ),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        profile=profile,
    )

    for directory in (
        settings.paths.logs,
        settings.paths.database.parent,
        settings.paths.output,
        settings.paths.linkedin_cookies.parent,
        settings.browser.screenshot_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


if __name__ == "__main__":  # python config.py -> quick sanity check
    s = load_settings()
    print("Keywords :", s.search.keywords)
    print("Locations:", s.search.locations)
    print("Exp codes:", s.search.experience_codes)
    print("Platforms:", s.platforms)
    print("Resume   :", s.paths.resume)
    print("Output   :", s.paths.output)
    print("Problems :", s.validate() or "none")

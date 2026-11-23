"""Domain models and the SQLite schema.

Plain dataclasses keep the automation layer free of SQL and make each module
independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ApplicationStatus(str, Enum):
    """Terminal state of a single job in a run."""

    DISCOVERED = "DISCOVERED"           # seen in search results, not processed yet
    SKIPPED_LOW_SCORE = "SKIPPED_LOW_SCORE"
    SKIPPED_BLOCKED = "SKIPPED_BLOCKED"   # blocked title/company keyword
    SKIPPED_NO_EASY_APPLY = "SKIPPED_NO_EASY_APPLY"
    SKIPPED_ALREADY_APPLIED = "SKIPPED_ALREADY_APPLIED"  # LinkedIn says applied
    DUPLICATE = "DUPLICATE"             # already in our own database
    NEEDS_HUMAN = "NEEDS_HUMAN"         # unknown question / unsupported form
    PREPARED_NOT_SUBMITTED = "PREPARED_NOT_SUBMITTED"  # AUTO_SUBMIT off
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


# Statuses that must block a second attempt on a later run.
TERMINAL_STATUSES = {
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.SKIPPED_ALREADY_APPLIED,
    ApplicationStatus.SKIPPED_BLOCKED,
    ApplicationStatus.SKIPPED_LOW_SCORE,
    ApplicationStatus.SKIPPED_NO_EASY_APPLY,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    """A job listing, as discovered and then enriched by the parser."""

    job_id: str
    platform: str = "linkedin"
    title: str = ""
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    workplace_type: str = ""          # On-site / Hybrid / Remote
    employment_type: str = ""         # Full-time / Contract / ...
    seniority: str = ""
    posted: str = ""
    easy_apply: bool = False
    already_applied: bool = False
    required_experience_years: Optional[float] = None
    keyword: str = ""                 # search keyword that surfaced it
    search_location: str = ""

    def fingerprint(self) -> str:
        """Secondary identity used when the numeric job id is unavailable."""
        return "|".join(
            part.strip().lower()
            for part in (self.company, self.title, self.location)
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MatchResult:
    score: int
    reasons: List[str] = field(default_factory=list)
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class ApplicationRecord:
    """What we persist for every job we touched."""

    job_id: str
    title: str
    company: str
    location: str
    url: str
    description: str
    match_score: int
    match_reasons: str
    status: ApplicationStatus
    resume_used: str = ""
    confirmation: str = ""
    error: str = ""
    questions_json: str = "[]"
    applied_at: str = field(default_factory=utc_now)


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id              TEXT PRIMARY KEY,
    platform            TEXT NOT NULL DEFAULT 'linkedin',
    fingerprint         TEXT,
    title               TEXT NOT NULL DEFAULT '',
    company             TEXT NOT NULL DEFAULT '',
    location            TEXT NOT NULL DEFAULT '',
    url                 TEXT NOT NULL DEFAULT '',
    description         TEXT NOT NULL DEFAULT '',
    workplace_type      TEXT NOT NULL DEFAULT '',
    employment_type     TEXT NOT NULL DEFAULT '',
    seniority           TEXT NOT NULL DEFAULT '',
    posted              TEXT NOT NULL DEFAULT '',
    easy_apply          INTEGER NOT NULL DEFAULT 0,
    required_experience REAL,
    keyword             TEXT NOT NULL DEFAULT '',
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);

CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'linkedin',
    fingerprint     TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL DEFAULT '',
    company         TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    match_score     INTEGER NOT NULL DEFAULT 0,
    match_reasons   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    resume_used     TEXT NOT NULL DEFAULT '',
    confirmation    TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    questions_json  TEXT NOT NULL DEFAULT '[]',
    applied_at      TEXT NOT NULL,
    -- One authoritative row per job: a re-processed job updates in place.
    UNIQUE(job_id) ON CONFLICT REPLACE
);

CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

-- Every screening question we have ever seen, with the answer finally used.
CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    question    TEXT NOT NULL,
    field_type  TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',   -- rule | learned | human | profile
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    jobs_found      INTEGER NOT NULL DEFAULT 0,
    relevant        INTEGER NOT NULL DEFAULT 0,
    submitted       INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    duplicates      INTEGER NOT NULL DEFAULT 0,
    needs_human     INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    notes           TEXT NOT NULL DEFAULT ''
);
"""

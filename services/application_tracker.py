"""Session statistics and the end-of-run terminal summary."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from database.db import Database
from database.models import ApplicationRecord, ApplicationStatus, Job, MatchResult
from utils.logger import get_logger

log = get_logger(__name__)

LINE = "=" * 78


@dataclass
class SessionStats:
    jobs_found: int = 0
    relevant: int = 0
    submitted: int = 0
    prepared: int = 0
    skipped: int = 0
    duplicates: int = 0
    needs_human: int = 0
    failed: int = 0
    skipped_breakdown: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, int]:
        return {
            "jobs_found": self.jobs_found,
            "relevant": self.relevant,
            "submitted": self.submitted,
            "prepared": self.prepared,
            "skipped": self.skipped,
            "duplicates": self.duplicates,
            "needs_human": self.needs_human,
            "failed": self.failed,
        }


class ApplicationTracker:
    """Single place where an outcome is both persisted and counted."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.stats = SessionStats()
        self.run_id: Optional[int] = None
        self.manual_jobs: List[str] = []
        self.submitted_jobs: List[str] = []
        self.failed_jobs: List[str] = []
        # Everything touched this run, kept in memory for the CSV export.
        self.rows: List[Dict[str, Any]] = []

    def start(self) -> None:
        self.run_id = self.db.start_run()

    # -- recording ---------------------------------------------------------- #
    def record(
        self,
        job: Job,
        status: ApplicationStatus,
        match: Optional[MatchResult] = None,
        resume_used: str = "",
        confirmation: str = "",
        error: str = "",
        questions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        questions = questions or []
        record = ApplicationRecord(
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            description=job.description,
            match_score=match.score if match else 0,
            match_reasons=match.summary if match else "",
            status=status,
            resume_used=resume_used,
            confirmation=confirmation,
            error=error,
            questions_json=json.dumps(questions, ensure_ascii=False),
        )
        self.db.record_application(
            record, fingerprint=job.fingerprint(), platform=job.platform
        )
        if questions:
            self.db.log_questions(job, questions)
        self._count(job, status)

        self.rows.append({
            "applied_at": record.applied_at,
            "platform": getattr(job, "platform", "linkedin"),
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "workplace_type": job.workplace_type,
            "employment_type": job.employment_type,
            "seniority": job.seniority,
            "posted": job.posted,
            "required_experience_years": job.required_experience_years or "",
            "search_keyword": job.keyword,
            "search_location": job.search_location,
            "match_score": record.match_score,
            "match_reasons": record.match_reasons,
            "status": status.value,
            "resume_used": resume_used,
            "confirmation": confirmation,
            "error": error,
            "questions_answered": " | ".join(
                str(q.get("question", "")) + " = " + str(q.get("answer", ""))
                for q in questions
            ),
            "url": job.url,
            "description": (job.description or "").replace("\n", " ")[:4000],
        })

    def _count(self, job: Job, status: ApplicationStatus) -> None:
        label = job.title + " @ " + job.company
        if status is ApplicationStatus.SUBMITTED:
            self.stats.submitted += 1
            self.submitted_jobs.append(label)
        elif status is ApplicationStatus.PREPARED_NOT_SUBMITTED:
            self.stats.prepared += 1
        elif status is ApplicationStatus.NEEDS_HUMAN:
            self.stats.needs_human += 1
            self.manual_jobs.append(label + "  ->  " + job.url)
        elif status is ApplicationStatus.FAILED:
            self.stats.failed += 1
            self.failed_jobs.append(label)
        elif status is ApplicationStatus.DUPLICATE:
            self.stats.duplicates += 1
        else:
            self.stats.skipped += 1
            key = status.value
            self.stats.skipped_breakdown[key] = self.stats.skipped_breakdown.get(key, 0) + 1

    def count_duplicate(self) -> None:
        """A duplicate is counted but deliberately not re-written to the DB."""
        self.stats.duplicates += 1

    def finish(self, notes: str = "") -> None:
        if self.run_id is not None:
            self.db.finish_run(self.run_id, self.stats.as_dict(), notes=notes)

    # -- summary ------------------------------------------------------------ #
    def print_summary(self, auto_submit: bool) -> None:
        s = self.stats
        print("\n" + LINE)
        print("SESSION SUMMARY")
        print(LINE)
        rows = [
            ("Total jobs found", s.jobs_found),
            ("Relevant jobs (>= min score)", s.relevant),
            ("Applications submitted", s.submitted),
            ("Prepared, not submitted (AUTO_SUBMIT off)", s.prepared),
            ("Jobs skipped", s.skipped),
            ("Duplicate jobs", s.duplicates),
            ("Needing manual intervention", s.needs_human),
            ("Failed applications", s.failed),
        ]
        for label, value in rows:
            print("  " + label.ljust(46) + str(value).rjust(5))

        if s.skipped_breakdown:
            print("\n  Skip reasons:")
            for reason, count in sorted(s.skipped_breakdown.items(), key=lambda kv: -kv[1]):
                print("    - " + reason.ljust(30) + str(count).rjust(4))

        if self.submitted_jobs:
            print("\n  Submitted:")
            for item in self.submitted_jobs:
                print("    + " + item)

        if self.manual_jobs:
            print("\n  Needs your attention (open these and finish manually):")
            for item in self.manual_jobs:
                print("    ! " + item)

        if self.failed_jobs:
            print("\n  Failed:")
            for item in self.failed_jobs:
                print("    x " + item)

        if not auto_submit and s.prepared:
            print("\n  NOTE: AUTO_SUBMIT=false - forms were filled and validated but")
            print("        never submitted. Set AUTO_SUBMIT=true in .env to send them.")

        print("\n  Lifetime submitted applications: " + str(self.db.total_submitted()))
        print(LINE + "\n")

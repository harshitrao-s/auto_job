"""SQLite persistence layer (connection, schema, duplicate detection, stats)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from database.models import (
    SCHEMA,
    TERMINAL_STATUSES,
    ApplicationRecord,
    ApplicationStatus,
    Job,
    utc_now,
)
from utils.logger import get_logger

log = get_logger(__name__)


class Database:
    """Thin, dependency-free wrapper around sqlite3."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        log.debug("Database ready at %s", self.path)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        wanted = {
            "jobs": [("platform", "TEXT NOT NULL DEFAULT 'linkedin'")],
            "applications": [("platform", "TEXT NOT NULL DEFAULT 'linkedin'")],
        }
        for table, columns in wanted.items():
            existing = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(" + table + ")")
            }
            for name, ddl in columns:
                if name not in existing:
                    self._conn.execute(
                        "ALTER TABLE " + table + " ADD COLUMN " + name + " " + ddl
                    )
                    log.info("Migrated %s: added column %s", table, name)

    # -- plumbing ---------------------------------------------------------- #
    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - best effort on shutdown
            pass

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- jobs -------------------------------------------------------------- #
    def upsert_job(self, job: Job) -> None:
        now = utc_now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs (job_id, platform, fingerprint, title, company, location, url,
                                  description, workplace_type, employment_type, seniority,
                                  posted, easy_apply, required_experience, keyword,
                                  first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    location=excluded.location,
                    url=excluded.url,
                    description=CASE WHEN length(excluded.description) > length(jobs.description)
                                     THEN excluded.description ELSE jobs.description END,
                    workplace_type=excluded.workplace_type,
                    employment_type=excluded.employment_type,
                    seniority=excluded.seniority,
                    posted=excluded.posted,
                    easy_apply=excluded.easy_apply,
                    required_experience=excluded.required_experience,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    job.job_id,
                    job.platform,
                    job.fingerprint(),
                    job.title,
                    job.company,
                    job.location,
                    job.url,
                    job.description,
                    job.workplace_type,
                    job.employment_type,
                    job.seniority,
                    job.posted,
                    int(job.easy_apply),
                    job.required_experience_years,
                    job.keyword,
                    now,
                    now,
                ),
            )

    # -- duplicate detection ----------------------------------------------- #
    def is_duplicate(self, job: Job) -> Optional[str]:
        """Return the previous status if this job was already processed.

        Identity is checked on the LinkedIn job id first (authoritative), then
        on the canonical job URL, then on a company+title+location fingerprint
        which catches the same role re-posted under a new id.
        """
        cur = self._conn.execute(
            "SELECT status FROM applications WHERE job_id = ?", (job.job_id,)
        )
        row = cur.fetchone()
        if row and row["status"] in {s.value for s in TERMINAL_STATUSES}:
            return row["status"]
        if row:
            # Non-terminal (FAILED / NEEDS_HUMAN / PREPARED): allow a retry.
            return None

        if job.url:
            cur = self._conn.execute(
                "SELECT status FROM applications WHERE url = ? AND status = ?",
                (job.url, ApplicationStatus.SUBMITTED.value),
            )
            row = cur.fetchone()
            if row:
                return row["status"]

        fingerprint = job.fingerprint()
        if fingerprint.strip("|"):
            cur = self._conn.execute(
                "SELECT status FROM applications WHERE fingerprint = ? AND status = ?",
                (fingerprint, ApplicationStatus.SUBMITTED.value),
            )
            row = cur.fetchone()
            if row:
                return row["status"]
        return None

    def already_seen_ids(self) -> set:
        cur = self._conn.execute("SELECT job_id FROM applications")
        return {row["job_id"] for row in cur.fetchall()}

    # -- applications ------------------------------------------------------ #
    def record_application(
        self,
        record: ApplicationRecord,
        fingerprint: str = "",
        platform: str = "linkedin",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO applications
                    (job_id, platform, fingerprint, title, company, location, url, description,
                     match_score, match_reasons, status, resume_used, confirmation,
                     error, questions_json, applied_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.job_id,
                    platform,
                    fingerprint,
                    record.title,
                    record.company,
                    record.location,
                    record.url,
                    record.description[:20000],
                    record.match_score,
                    record.match_reasons,
                    record.status.value,
                    record.resume_used,
                    record.confirmation,
                    record.error,
                    record.questions_json,
                    record.applied_at,
                ),
            )
        log.debug("Recorded %s for %s @ %s", record.status.value, record.title, record.company)

    def log_questions(self, job: Job, answers: Iterable[Dict[str, Any]]) -> None:
        rows = [
            (
                job.job_id,
                job.company,
                item.get("question", ""),
                item.get("field_type", ""),
                str(item.get("answer", "")),
                item.get("source", ""),
                utc_now(),
            )
            for item in answers
        ]
        if not rows:
            return
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO questions
                   (job_id, company, question, field_type, answer, source, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )

    # -- runs -------------------------------------------------------------- #
    def start_run(self) -> int:
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (utc_now(),))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: Dict[str, int], notes: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE runs SET finished_at=?, jobs_found=?, relevant=?, submitted=?,
                                   skipped=?, duplicates=?, needs_human=?, failed=?, notes=?
                   WHERE id=?""",
                (
                    utc_now(),
                    stats.get("jobs_found", 0),
                    stats.get("relevant", 0),
                    stats.get("submitted", 0),
                    stats.get("skipped", 0),
                    stats.get("duplicates", 0),
                    stats.get("needs_human", 0),
                    stats.get("failed", 0),
                    notes,
                    run_id,
                ),
            )

    # -- reporting --------------------------------------------------------- #
    def status_counts(self) -> Dict[str, int]:
        cur = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        )
        return {row["status"]: row["n"] for row in cur.fetchall()}

    def recent_applications(self, limit: int = 20) -> List[sqlite3.Row]:
        cur = self._conn.execute(
            """SELECT title, company, location, match_score, status, applied_at, url
               FROM applications ORDER BY applied_at DESC LIMIT ?""",
            (limit,),
        )
        return cur.fetchall()

    def total_submitted(self) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE status = ?",
            (ApplicationStatus.SUBMITTED.value,),
        )
        return int(cur.fetchone()["n"])


if __name__ == "__main__":  # python -m database.db -> self test on a temp file
    import tempfile

    tmp = Path(tempfile.gettempdir()) / "job_db_selftest.sqlite3"
    tmp.unlink(missing_ok=True)
    with Database(tmp) as db:
        job = Job(job_id="123", title="React Developer", company="Acme", location="Gurugram")
        db.upsert_job(job)
        assert db.is_duplicate(job) is None, "fresh job must not be a duplicate"
        db.record_application(
            ApplicationRecord(
                job_id="123",
                title=job.title,
                company=job.company,
                location=job.location,
                url="https://example.com/jobs/123",
                description="",
                match_score=88,
                match_reasons="skills matched",
                status=ApplicationStatus.SUBMITTED,
            ),
            fingerprint=job.fingerprint(),
        )
        assert db.is_duplicate(job) == "SUBMITTED"
        reposted = Job(job_id="999", title="React Developer", company="Acme", location="Gurugram")
        assert db.is_duplicate(reposted) == "SUBMITTED", "fingerprint dedupe failed"
        print("database self test OK ->", db.status_counts())
    tmp.unlink(missing_ok=True)

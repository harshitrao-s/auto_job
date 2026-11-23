"""CSV export of everything a run touched.

Files land in date-wise folders so each day's work is self-contained:

    output/2026-11-23/applications_142530.csv   <- this run only
    output/2026-11-23/all_applications.csv      <- full lifetime dump (--export-all)

Written with the csv module and utf-8-sig so Excel opens Hindi/Unicode text
and long descriptions correctly.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from database.db import Database
from utils.logger import get_logger

log = get_logger(__name__)

COLUMNS: Sequence[str] = (
    "applied_at",
    "platform",
    "job_id",
    "title",
    "company",
    "location",
    "workplace_type",
    "employment_type",
    "seniority",
    "posted",
    "required_experience_years",
    "search_keyword",
    "search_location",
    "match_score",
    "match_reasons",
    "status",
    "resume_used",
    "confirmation",
    "error",
    "questions_answered",
    "url",
    "description",
)


def dated_dir(root: Path, when: Optional[datetime] = None) -> Path:
    """output/<YYYY-MM-DD>/ , created on demand."""
    when = when or datetime.now()
    path = Path(root) / when.strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
            count += 1
    return count


def export_session(rows: List[Dict[str, Any]], output_root: Path) -> Optional[Path]:
    """Write the rows this run produced into today's folder."""
    if not rows:
        log.info("Nothing to export - no jobs were processed")
        return None
    folder = dated_dir(output_root)
    path = folder / ("applications_" + datetime.now().strftime("%H%M%S") + ".csv")
    written = _write(path, rows)
    log.info("Exported %d rows -> %s", written, path)
    return path


def export_all(db: Database, output_root: Path) -> Optional[Path]:
    """Dump the entire applications table (every run, every platform)."""
    cursor = db._conn.execute(
        """SELECT applied_at, platform, job_id, title, company, location, url,
                  description, match_score, match_reasons, status, resume_used,
                  confirmation, error, questions_json
           FROM applications ORDER BY applied_at DESC"""
    )
    rows: List[Dict[str, Any]] = []
    for record in cursor.fetchall():
        item = dict(record)
        item["description"] = (item.get("description") or "").replace("\n", " ")[:4000]
        item["questions_answered"] = item.pop("questions_json", "")
        rows.append(item)

    if not rows:
        log.info("Database is empty - nothing to dump")
        return None

    folder = dated_dir(output_root)
    path = folder / "all_applications.csv"
    written = _write(path, rows)
    log.info("Exported full history: %d rows -> %s", written, path)
    return path


if __name__ == "__main__":  # python -m services.exporter
    import tempfile

    sample = [{
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "platform": "linkedin",
        "job_id": "123",
        "title": "React Developer",
        "company": "Acme",
        "location": "Gurugram",
        "match_score": 91,
        "status": "SUBMITTED",
        "url": "https://www.linkedin.com/jobs/view/123/",
    }]
    out = export_session(sample, Path(tempfile.gettempdir()) / "job_export_selftest")
    print("wrote:", out)
    print(out.read_text(encoding="utf-8-sig").splitlines()[1][:80])
    print("exporter self test OK")

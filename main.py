"""Automated job application bot - single entry point.

    python main.py                      # run with .env settings
    python main.py --platform naukri    # one platform only
    python main.py --dry-run            # never submit, just fill + validate
    python main.py --max 5 --min-score 70
    python main.py --export-all         # also dump the full history to CSV
    python main.py --summary            # print stored stats and exit

Pipeline: browser -> session -> search -> dedupe -> parse -> score -> apply
-> record -> CSV export -> summary.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

from config import Settings, load_settings
from utils.logger import get_logger, setup_logging

log = get_logger("main")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automated LinkedIn / Naukri job application bot",
    )
    parser.add_argument("--platform", choices=["linkedin", "naukri", "both"],
                        help="override PLATFORMS from .env")
    parser.add_argument("--max", type=int, dest="max_applications",
                        help="maximum applications this session")
    parser.add_argument("--min-score", type=int, dest="min_score",
                        help="minimum relevance score (0-100)")
    parser.add_argument("--keywords", help="comma separated search keywords override")
    parser.add_argument("--locations", help="pipe separated locations override")
    parser.add_argument("--dry-run", action="store_true",
                        help="force AUTO_SUBMIT off for this run")
    parser.add_argument("--submit", action="store_true",
                        help="force AUTO_SUBMIT on for this run")
    parser.add_argument("--headless", action="store_true", help="run without a visible window")
    parser.add_argument("--non-interactive", action="store_true",
                        help="never prompt; unknown questions are skipped")
    parser.add_argument("--export-all", action="store_true",
                        help="also export the full application history to CSV")
    parser.add_argument("--summary", action="store_true",
                        help="print stored statistics and exit")
    return parser.parse_args(argv)


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.platform:
        settings.platforms = ["linkedin", "naukri"] if args.platform == "both" else [args.platform]
    if args.max_applications is not None:
        settings.automation.max_applications_per_session = args.max_applications
    if args.min_score is not None:
        settings.automation.min_match_score = args.min_score
    if args.keywords:
        settings.search.keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if args.locations:
        settings.search.locations = [l.strip() for l in args.locations.split("|") if l.strip()]
    if args.dry_run:
        settings.automation.auto_submit = False
    if args.submit:
        settings.automation.auto_submit = True
    if args.headless:
        settings.browser.headless = True
        settings.automation.headless = True
    if args.non_interactive:
        settings.automation.interactive = False
    return settings


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def run(settings: Settings, export_all_history: bool = False) -> int:
    # Imports are local so --summary works even without Playwright installed.
    from playwright.sync_api import Error as PlaywrightError

    from automation.authentication import (
        AuthenticationRequired,
        SecurityCheckpoint,
        ensure_logged_in,
    )
    from automation.browser import BrowserManager, human_pause
    from automation.easy_apply import EasyApply
    from automation.job_matcher import JobMatcher
    from automation.job_parser import JobParser
    from automation.job_search import JobSearch, SearchRestricted
    from automation.naukri import NaukriApply, NaukriParser, NaukriSearch
    from database.db import Database
    from database.models import ApplicationStatus, Job
    from services.application_tracker import ApplicationTracker
    from services.exporter import export_all, export_session
    from services.question_handler import QuestionHandler
    from services.resume_manager import ResumeError, ResumeManager

    automation = settings.automation

    # -- preflight ---------------------------------------------------------- #
    problems = settings.validate()
    if problems:
        print("\nCannot start - fix these first:")
        for problem in problems:
            print("  - " + problem)
        print("\nSee README.md 'Setup'.")
        return 2

    resume_manager = ResumeManager(settings.paths.resume)
    try:
        resume_manager.validate()
    except ResumeError as exc:
        print("\nResume problem: " + str(exc))
        return 2

    for warning in resume_manager.cross_check(
        settings.profile_get("personal.email", ""),
        settings.profile_get("personal.phone", ""),
    ):
        log.warning("%s", warning)

    matcher = JobMatcher.from_settings(settings)
    question_handler = QuestionHandler.from_settings(settings)

    db = Database(settings.paths.database)
    tracker = ApplicationTracker(db)
    tracker.start()

    log.info(
        "Session start | platforms=%s | auto_submit=%s | min_score=%d | max_apps=%d",
        ",".join(settings.platforms), automation.auto_submit,
        automation.min_match_score, automation.max_applications_per_session,
    )
    if not automation.auto_submit:
        log.warning("AUTO_SUBMIT is OFF - forms are filled and validated but never sent")

    browser = BrowserManager(settings.browser)
    consecutive_failures = 0
    notes = ""

    try:
        page = browser.start()

        for platform in settings.platforms:
            if tracker.stats.submitted >= automation.max_applications_per_session:
                break

            cookies_path = (
                settings.paths.naukri_cookies if platform == "naukri"
                else settings.paths.linkedin_cookies
            )
            try:
                ensure_logged_in(
                    page,
                    platform=platform,
                    interactive=automation.interactive,
                    cookies_path=cookies_path,
                )
            except (AuthenticationRequired, SecurityCheckpoint) as exc:
                log.error("%s: %s", platform, exc)
                notes += platform + ": " + str(exc) + " | "
                continue

            searcher = (NaukriSearch(page, settings.search) if platform == "naukri"
                        else JobSearch(page, settings.search))
            if platform == "naukri" and settings.browser.headless:
                log.warning(
                    "Naukri's edge WAF serves 'Access Denied' to headless browsers - "
                    "run without --headless for this platform"
                )
            parser = NaukriParser(page) if platform == "naukri" else JobParser(page)

            processed = 0
            stop_platform = False

            for keyword in settings.search.keywords:
                if stop_platform:
                    break
                for location in settings.search.locations:
                    if stop_platform:
                        break
                    try:
                        discovered = list(searcher.search(keyword, location))
                    except SearchRestricted as exc:
                        log.error("%s", exc)
                        notes += str(exc) + " | "
                        stop_platform = True
                        break
                    except Exception as exc:
                        log.exception("Search failed for '%s' in '%s': %s", keyword, location, exc)
                        continue

                    tracker.stats.jobs_found += len(discovered)

                    for job in discovered:
                        job.platform = platform

                        if tracker.stats.submitted >= automation.max_applications_per_session:
                            log.info("Reached the per-session application limit")
                            stop_platform = True
                            break
                        if processed >= automation.max_jobs_to_process:
                            log.info("Reached MAX_JOBS_TO_PROCESS for %s", platform)
                            stop_platform = True
                            break
                        if consecutive_failures >= automation.max_consecutive_failures:
                            log.error(
                                "%d consecutive failures - stopping to avoid hammering the site",
                                consecutive_failures,
                            )
                            notes += "circuit breaker tripped | "
                            stop_platform = True
                            break

                        processed += 1
                        label = job.title + " @ " + job.company

                        # 1. duplicate check (before any page load)
                        previous = db.is_duplicate(job)
                        if previous:
                            log.info("Duplicate (%s): %s", previous, label)
                            tracker.count_duplicate()
                            continue

                        # 2. cheap title block before spending a page load
                        blocked = matcher.is_blocked(job)
                        if blocked and job.title:
                            log.info("Blocked (%s): %s", blocked, label)
                            tracker.record(job, ApplicationStatus.SKIPPED_BLOCKED,
                                           match=matcher.score(job))
                            continue

                        # 3. open and parse the detail page
                        try:
                            parsed = parser.parse(job)
                        except Exception as exc:
                            log.exception("Parsing failed for %s: %s", label, exc)
                            parsed = None
                        if parsed is None:
                            consecutive_failures += 1
                            tracker.record(job, ApplicationStatus.FAILED,
                                           error="Could not open or parse the job page")
                            continue
                        job = parsed
                        job.platform = platform
                        db.upsert_job(job)
                        label = job.title + " @ " + job.company

                        # 4. LinkedIn's own "already applied" marker
                        if getattr(job, "already_applied", False):
                            log.info("LinkedIn says already applied: %s", label)
                            tracker.record(job, ApplicationStatus.SKIPPED_ALREADY_APPLIED)
                            continue

                        # 5. Easy Apply availability (LinkedIn only)
                        if platform == "linkedin" and not job.easy_apply:
                            log.info("No Easy Apply: %s", label)
                            tracker.record(job, ApplicationStatus.SKIPPED_NO_EASY_APPLY)
                            continue

                        # 6. relevance scoring
                        match = matcher.score(job)
                        if match.score < automation.min_match_score:
                            log.info("Score %d < %d, skipping: %s",
                                     match.score, automation.min_match_score, label)
                            tracker.record(job, ApplicationStatus.SKIPPED_LOW_SCORE, match=match)
                            human_pause(1.0, 2.0)
                            continue

                        tracker.stats.relevant += 1
                        log.info("MATCH %d/100 - applying to %s", match.score, label)

                        # 7. apply
                        resume = resume_manager.pick_for_job(job.title, job.description)
                        try:
                            if platform == "naukri":
                                worker = NaukriApply(page, question_handler,
                                                     automation.auto_submit)
                            else:
                                worker = EasyApply(page, question_handler, resume,
                                                   automation.auto_submit)
                            outcome = worker.apply(label)
                        except Exception as exc:
                            log.exception("Apply crashed for %s: %s", label, exc)
                            browser.screenshot("crash_" + job.job_id)
                            consecutive_failures += 1
                            tracker.record(job, ApplicationStatus.FAILED,
                                           match=match, resume_used=resume.name,
                                           error=str(exc)[:400])
                            human_pause(automation.between_jobs_delay,
                                        automation.between_jobs_delay + 4)
                            continue

                        if outcome.status is ApplicationStatus.FAILED:
                            consecutive_failures += 1
                            browser.screenshot("failed_" + job.job_id)
                        else:
                            consecutive_failures = 0

                        tracker.record(
                            job,
                            outcome.status,
                            match=match,
                            resume_used=resume.name,
                            confirmation=outcome.confirmation,
                            error=outcome.error,
                            questions=outcome.questions,
                        )
                        log.info("%s: %s", outcome.status.value, label)

                        # 8. conservative pacing between applications
                        human_pause(automation.between_jobs_delay,
                                    automation.between_jobs_delay + 5)

                    human_pause(automation.between_searches_delay,
                                automation.between_searches_delay + 5)

    except KeyboardInterrupt:
        print("\nInterrupted - saving what we have...")
        notes += "interrupted by user | "
    except PlaywrightError as exc:
        log.exception("Browser error: %s", exc)
        notes += "browser error: " + str(exc)[:200] + " | "
    finally:
        tracker.finish(notes=notes)
        try:
            browser.stop()
        except Exception:
            pass

        csv_path = export_session(tracker.rows, settings.paths.output)
        history_path = export_all(db, settings.paths.output) if export_all_history else None

        tracker.print_summary(auto_submit=automation.auto_submit)
        if csv_path:
            print("  CSV (this run)  : " + str(csv_path))
        if history_path:
            print("  CSV (all time)  : " + str(history_path))
        print("  Database        : " + str(settings.paths.database))
        print("")
        db.close()

    return 0


def print_summary_only(settings: Settings) -> int:
    from database.db import Database

    db = Database(settings.paths.database)
    counts = db.status_counts()
    print("\nStored application statistics")
    print("=" * 50)
    if not counts:
        print("  (no applications recorded yet)")
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("  " + status.ljust(30) + str(count).rjust(5))
    print("\nMost recent:")
    for row in db.recent_applications(limit=15):
        print("  " + str(row["applied_at"])[:16] + "  " +
              str(row["match_score"]).rjust(3) + "  " +
              str(row["status"]).ljust(24) + " " +
              str(row["title"])[:40] + " @ " + str(row["company"])[:25])
    print("")
    db.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    settings = apply_overrides(load_settings(), args)
    setup_logging(settings.paths.logs, settings.log_level)

    if args.summary:
        return print_summary_only(settings)

    started = time.time()
    code = run(settings, export_all_history=args.export_all)
    log.info("Finished in %.1f minutes", (time.time() - started) / 60)
    return code


if __name__ == "__main__":
    sys.exit(main())

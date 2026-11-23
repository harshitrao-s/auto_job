"""Offline unit tests - no browser, no network.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from automation.job_matcher import JobMatcher, extract_required_experience
from automation.job_search import build_search_url, extract_job_id
from automation.naukri import build_search_url as naukri_url, naukri_job_id
from config import load_settings
from database.db import Database
from database.models import ApplicationRecord, ApplicationStatus, Job
from services.exporter import export_session
from services.question_handler import Question, QuestionHandler

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "config" / "profile.yaml").read_text(encoding="utf-8"))


class TestExperienceExtraction(unittest.TestCase):
    def test_ranges_and_minimums(self):
        self.assertEqual(extract_required_experience("2-4 years of experience"), 2)
        self.assertEqual(extract_required_experience("Minimum 5 years experience"), 5)
        self.assertEqual(extract_required_experience("3+ yrs in React"), 3)
        self.assertEqual(extract_required_experience("at least 1 year"), 1)

    def test_no_number_means_unknown(self):
        self.assertIsNone(extract_required_experience("We value curiosity"))
        self.assertIsNone(extract_required_experience(""))


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.matcher = JobMatcher(
            skills=PROFILE["skills"],
            target_titles=PROFILE["target_titles"],
            blocked_title_keywords=PROFILE["blocked_title_keywords"],
            blocked_company_keywords=[],
            preferred_locations=PROFILE["preferred_locations"],
            candidate_experience_years=3,
            experience_min=2,
            experience_max=4,
        )

    def test_strong_match_scores_high(self):
        job = Job(
            job_id="1",
            title="React.js Developer",
            company="Acme",
            location="Gurugram, Haryana, India",
            employment_type="Full-time",
            description="2-4 years with React, Next.js, TypeScript, Node.js, Redux and MongoDB.",
        )
        self.assertGreaterEqual(self.matcher.score(job).score, 85)

    def test_senior_mismatch_scores_low(self):
        job = Job(
            job_id="2",
            title="Software Engineer",
            company="Beta",
            location="Bengaluru, India",
            employment_type="Full-time",
            description="10+ years of Java, Spring Boot, Kafka and Oracle required.",
        )
        self.assertLess(self.matcher.score(job).score, 60)

    def test_blocked_titles_score_zero(self):
        for title in ("Engineering Manager", "QA Tester", "Android Developer Intern"):
            job = Job(job_id="x", title=title, company="C", location="Remote")
            self.assertEqual(self.matcher.score(job).score, 0, title)

    def test_skill_aliases_are_equivalent(self):
        job = Job(
            job_id="3",
            title="Frontend Developer",
            company="Acme",
            location="Remote",
            employment_type="Full-time",
            description="Strong ReactJS and NodeJS skills, 3 years experience.",
        )
        result = self.matcher.score(job)
        self.assertNotIn("react", result.missing_skills)
        self.assertGreater(result.score, 70)

    def test_experience_ceiling_blocks_senior_roles(self):
        """EXPERIENCE_MAX_YEARS is a hard limit on both platforms."""
        for years, description in (
            (5, "5+ years of React and Node.js experience"),
            (6, "6-9 years building React applications"),
            (8, "Minimum 8 years experience with React"),
        ):
            job = Job(job_id="x", title="React Developer", company="C",
                      location="Gurugram", employment_type="Full-time",
                      description=description)
            result = self.matcher.score(job)
            self.assertEqual(result.score, 0, description)
            self.assertIn("above your", result.reasons[0])

    def test_in_range_experience_passes(self):
        for description in (
            "2-4 years of experience with React, Next.js and Node.js",
            "3+ years with React and Next.js",
            "4 years experience in React, Redux and Node.js",
        ):
            job = Job(job_id="x", title="React Developer", company="C",
                      location="Gurugram", employment_type="Full-time",
                      description=description)
            self.assertGreater(self.matcher.score(job).score, 80, description)

    def test_naukri_card_experience_range_is_accepted(self):
        """A Naukri card reading '2-5 Yrs' demands a minimum of 2 - in range."""
        job = Job(job_id="x", platform="naukri", title="React Developer",
                  company="C", location="Gurugram", employment_type="Full-time",
                  description="React, Next.js, Node.js, TypeScript, Redux")
        job.required_experience_years = extract_required_experience("2-5 Yrs years")
        self.assertEqual(job.required_experience_years, 2)
        self.assertGreater(self.matcher.score(job).score, 80)

    def test_score_is_bounded(self):
        job = Job(job_id="4", title="React Developer", company="A", location="Gurugram",
                  employment_type="Full-time",
                  description=" ".join(PROFILE["skills"].keys()) + " 1 year experience")
        score = self.matcher.score(job).score
        self.assertTrue(0 <= score <= 100)


class TestQuestionHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "answers.json"
        self.handler = QuestionHandler(PROFILE, self.tmp, interactive=False)

    def test_skill_years_from_profile(self):
        answer = self.handler.answer(
            Question("How many years of work experience do you have with React?", "number", [])
        )
        self.assertEqual(answer.value, "3")

    def test_generic_experience_uses_total(self):
        answer = self.handler.answer(
            Question("How many years of total professional experience do you have?", "number", [])
        )
        self.assertEqual(answer.value, "3")

    def test_yes_no_mapped_to_offered_option(self):
        answer = self.handler.answer(
            Question("Are you legally authorized to work in India?", "radio",
                     ["Yes, I am authorized", "No"])
        )
        self.assertEqual(answer.value, "Yes, I am authorized")

    def test_unset_salary_is_never_invented(self):
        self.assertIsNone(
            self.handler.answer(Question("What is your expected CTC?", "number", []))
        )

    def test_unknown_question_needs_human(self):
        self.assertIsNone(
            self.handler.answer(Question("Describe your spirit animal", "text", []))
        )

    def test_learned_answers_persist(self):
        question = Question("What is your preferred shift?", "text", [])
        self.handler.remember(question, "Day shift")
        reloaded = QuestionHandler(PROFILE, self.tmp, interactive=False)
        answer = reloaded.answer(question)
        self.assertIsNotNone(answer)
        self.assertEqual(answer.value, "Day shift")
        self.assertEqual(answer.source, "learned")

    def test_react_native_does_not_answer_for_react(self):
        # 'react native' is not in the profile, so the longest alias match wins
        # only when the skill itself exists; otherwise the total is used.
        answer = self.handler.answer(
            Question("How many years of experience do you have with Kubernetes?", "number", [])
        )
        self.assertEqual(answer.value, "1")


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "test.sqlite3"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()

    def _submit(self, job: Job):
        self.db.record_application(
            ApplicationRecord(
                job_id=job.job_id, title=job.title, company=job.company,
                location=job.location, url=job.url, description="",
                match_score=90, match_reasons="", status=ApplicationStatus.SUBMITTED,
            ),
            fingerprint=job.fingerprint(),
            platform=job.platform,
        )

    def test_same_job_id_is_duplicate(self):
        job = Job(job_id="1", title="React Dev", company="Acme", location="Gurugram")
        self.assertIsNone(self.db.is_duplicate(job))
        self._submit(job)
        self.assertEqual(self.db.is_duplicate(job), "SUBMITTED")

    def test_reposted_job_detected_by_fingerprint(self):
        self._submit(Job(job_id="1", title="React Dev", company="Acme", location="Gurugram"))
        repost = Job(job_id="2", title="React Dev", company="Acme", location="Gurugram")
        self.assertEqual(self.db.is_duplicate(repost), "SUBMITTED")

    def test_failed_jobs_may_be_retried(self):
        job = Job(job_id="7", title="Node Dev", company="Beta", location="Delhi")
        self.db.record_application(
            ApplicationRecord(
                job_id=job.job_id, title=job.title, company=job.company,
                location=job.location, url="", description="", match_score=70,
                match_reasons="", status=ApplicationStatus.FAILED,
            ),
            fingerprint=job.fingerprint(),
        )
        self.assertIsNone(self.db.is_duplicate(job), "a failed attempt should be retryable")

    def test_run_lifecycle(self):
        run_id = self.db.start_run()
        self.db.finish_run(run_id, {"jobs_found": 5, "submitted": 2}, notes="ok")
        row = self.db._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["jobs_found"], 5)
        self.assertEqual(row["submitted"], 2)


class TestSearchUrls(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings().search

    def test_linkedin_url_has_filters(self):
        url = build_search_url(self.settings, "React Developer", "Gurugram, Haryana, India")
        self.assertIn("f_AL=true", url)       # Easy Apply
        self.assertIn("sortBy=DD", url)       # most recent
        self.assertIn("f_JT=F", url)          # full-time
        self.assertIn("f_E=", url)            # experience levels

    def test_linkedin_pagination(self):
        url = build_search_url(self.settings, "React", "India", start=25)
        self.assertIn("start=25", url)

    def test_job_id_extraction(self):
        self.assertEqual(extract_job_id("/jobs/view/3912345678/"), "3912345678")
        self.assertEqual(extract_job_id("/jobs/search/?currentJobId=4012345678"), "4012345678")
        self.assertIsNone(extract_job_id("/feed/"))

    def test_naukri_url_and_id(self):
        url = naukri_url(self.settings, "React.js Developer", "Gurugram, Haryana, India")
        # A dot must be spelled '-dot-' or Naukri redirects and drops the filters.
        self.assertIn("react-dot-js-developer-jobs-in-gurugram", url)
        self.assertIn("k=react.js+developer", url)
        self.assertIn("sort=f", url)          # sort=date returns nothing
        self.assertNotIn("jobType", url)      # collapses the result set

    def test_naukri_pagination_is_a_query_param(self):
        url = naukri_url(self.settings, "React Developer", "Delhi, India", page_number=3)
        self.assertIn("pageNo=3", url)
        self.assertNotIn("-jobs-in-delhi-3", url)

    def test_naukri_remote_uses_wfh_filter(self):
        url = naukri_url(self.settings, "React Developer", "Remote")
        self.assertIn("wfhType=2", url)
        self.assertNotIn("jobs-in-remote", url)
        self.assertEqual(
            naukri_job_id("https://www.naukri.com/job-listings-react-dev-acme-310125904567"),
            "310125904567",
        )


class TestExporter(unittest.TestCase):
    def test_csv_written_into_date_folder(self):
        root = Path(tempfile.mkdtemp())
        rows = [{
            "applied_at": "2026-11-23T10:00:00", "platform": "naukri", "job_id": "1",
            "title": "React Developer", "company": "Acme", "location": "Gurugram",
            "match_score": 88, "status": "SUBMITTED",
            "url": "https://www.naukri.com/job-1",
        }]
        path = export_session(rows, root)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertRegex(path.parent.name, r"^\d{4}-\d{2}-\d{2}$")
        content = path.read_text(encoding="utf-8-sig")
        self.assertIn("React Developer", content)
        self.assertIn("match_score", content.splitlines()[0])

    def test_empty_session_exports_nothing(self):
        self.assertIsNone(export_session([], Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main(verbosity=2)

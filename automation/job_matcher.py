"""Job relevance scoring (0-100).

Pure logic, no browser involved, so it can be unit tested on its own:

    python -m automation.job_matcher

Weights
-------
skills       40  - how much of the job's demanded stack the candidate has
title        25  - is this the kind of role we want at all
experience   20  - does the demanded years range include the candidate
location     10  - is it in a preferred city / remote
type          5  - full-time vs contract/internship
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from database.models import Job, MatchResult
from utils.logger import get_logger

log = get_logger(__name__)

WEIGHT_SKILLS = 40
WEIGHT_TITLE = 25
WEIGHT_EXPERIENCE = 20
WEIGHT_LOCATION = 10
WEIGHT_TYPE = 5

# Skills that, when demanded, matter more than a long tail of buzzwords.
CORE_SKILLS = {
    "react", "react.js", "next.js", "javascript", "typescript",
    "node", "node.js", "redux", "frontend", "full stack",
}

# The stack the candidate is actually hired for. A posting that mentions none
# of these is not a React/Next role however many generic keywords it shares.
SIGNATURE_SKILLS = ("react", "next.js", "node", "redux")

# Rival frameworks. A posting built on one of these, with no mention of the
# candidate's signature stack, is a mismatch even when HTML/CSS/JS/Git match.
COMPETING_FRAMEWORKS = (
    "angular", "vue", "svelte", "ember", "jquery",
    "laravel", "django", "flask", "spring boot", "rails",
    ".net", "asp.net", "php", "golang", "ruby",
    "react native", "flutter", "android", "ios",
)
COMPETING_PENALTY = 22

# Different spellings of the same technology, so "react.js" in a posting is
# satisfied by "react" in the profile (and vice versa).
ALIASES = {
    "react": {"react", "react.js", "reactjs"},
    "next.js": {"next.js", "nextjs", "next js"},
    "node": {"node", "node.js", "nodejs"},
    "javascript": {"javascript", "js", "es6"},
    "typescript": {"typescript", "ts"},
    "frontend": {"frontend", "front-end", "front end", "ui"},
    "full stack": {"full stack", "fullstack", "full-stack", "mern"},
    "redux": {"redux", "redux toolkit", "rtk"},
    "tailwind": {"tailwind", "tailwind css", "tailwindcss"},
    "scss": {"scss", "sass"},
}


def _alias_group(skill: str) -> set:
    """All spellings equivalent to `skill` (including itself)."""
    skill = skill.lower()
    for group in ALIASES.values():
        if skill in group:
            return group
    return {skill}


# "3+ years", "3 to 5 years", "minimum 2 yrs", "2-4 years of experience"
EXPERIENCE_PATTERNS = [
    re.compile(r"(\d{1,2})\s*[-–to]{1,3}\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(?:minimum|min\.?|at least|over)\s*(\d{1,2})\s*\+?\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2})\s*\+\s*(?:years|yrs|year)", re.I),
    re.compile(r"(\d{1,2})\s*(?:years|yrs|year)[\s\w]{0,20}experience", re.I),
]


def extract_required_experience(text: str) -> Optional[float]:
    """Best-effort minimum-years extraction from a job description.

    Returns the *minimum* demanded years, or None when the text says nothing.
    """
    if not text:
        return None
    sample = text[:6000]
    for pattern in EXPERIENCE_PATTERNS:
        match = pattern.search(sample)
        if match:
            groups = [g for g in match.groups() if g]
            try:
                values = [float(g) for g in groups]
            except ValueError:
                continue
            if values and 0 <= min(values) <= 25:
                return min(values)
    return None


def _skill_mentioned(skill: str, haystack: str) -> bool:
    """Word-boundary-ish containment that survives dots in 'node.js'."""
    escaped = re.escape(skill.lower())
    return re.search(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", haystack) is not None


class JobMatcher:
    def __init__(
        self,
        skills: Dict[str, float],
        target_titles: Iterable[str],
        blocked_title_keywords: Iterable[str],
        blocked_company_keywords: Iterable[str],
        preferred_locations: Iterable[str],
        candidate_experience_years: float,
        experience_min: float = 2,
        experience_max: float = 4,
    ) -> None:
        self.skills = {k.lower(): float(v) for k, v in (skills or {}).items()}
        self.target_titles = [t.lower() for t in target_titles or []]
        self.blocked_titles = [t.lower() for t in blocked_title_keywords or []]
        self.blocked_companies = [c.lower() for c in blocked_company_keywords or []]
        self.preferred_locations = [l.lower() for l in preferred_locations or []]
        self.candidate_experience = float(candidate_experience_years or 0)
        self.experience_min = experience_min
        self.experience_max = experience_max

    @classmethod
    def from_settings(cls, settings) -> "JobMatcher":
        return cls(
            skills=settings.profile_get("skills", {}) or {},
            target_titles=settings.profile_get("target_titles", []) or [],
            blocked_title_keywords=settings.profile_get("blocked_title_keywords", []) or [],
            blocked_company_keywords=settings.profile_get("blocked_company_keywords", []) or [],
            preferred_locations=settings.profile_get("preferred_locations", []) or [],
            candidate_experience_years=settings.profile_get(
                "professional.total_experience_years", 0
            ),
            experience_min=settings.search.experience_min_years,
            experience_max=settings.search.experience_max_years,
        )

    # -- hard blocks -------------------------------------------------------- #
    def exceeds_experience_range(self, job: Job) -> Optional[str]:
        """Reject postings that demand more years than the configured ceiling.

        EXPERIENCE_MAX_YEARS is a hard limit, not a preference: a role asking
        for more than that is out of range however well the stack matches.
        A tolerance of 0 keeps "5+ years" out of a 2-4 year search.
        """
        required = job.required_experience_years
        if required is None:
            required = extract_required_experience(job.description or "")
        if required is None:
            return None
        if required > self.experience_max:
            return ("requires " + str(required) + "y, above your "
                    + str(self.experience_min) + "-" + str(self.experience_max) + "y range")
        return None

    def is_blocked(self, job: Job) -> Optional[str]:
        title = (job.title or "").lower()
        company = (job.company or "").lower()
        for keyword in self.blocked_titles:
            if keyword in title:
                return "title contains blocked keyword '" + keyword + "'"
        for keyword in self.blocked_companies:
            if keyword and keyword in company:
                return "company is blocklisted ('" + keyword + "')"
        return None

    # -- component scores --------------------------------------------------- #
    def _score_skills(self, job: Job) -> Tuple[float, List[str], List[str]]:
        haystack = ((job.description or "") + " " + (job.title or "")).lower()
        if not haystack.strip():
            return WEIGHT_SKILLS * 0.5, [], []  # unknown -> neutral, don't punish

        known = [s for s in self.skills if self.skills[s] > 0]
        matched = [s for s in known if any(_skill_mentioned(a, haystack) for a in _alias_group(s))]
        # Core skills the posting demands but the candidate does not have in
        # any spelling (react / react.js / reactjs all count as the same skill).
        missing = [
            s for s in CORE_SKILLS
            if any(_skill_mentioned(a, haystack) for a in _alias_group(s))
            and not any(self.skills.get(a, 0) > 0 for a in _alias_group(s))
        ]

        if not matched:
            return 0.0, [], missing

        core_hits = len([s for s in matched if s in CORE_SKILLS])
        # Saturating curve: 6 skill hits is already an excellent overlap.
        breadth = min(len(matched) / 6.0, 1.0)
        depth = min(core_hits / 3.0, 1.0)
        ratio = 0.5 * breadth + 0.5 * depth
        return WEIGHT_SKILLS * ratio, sorted(matched), sorted(missing)

    def _score_title(self, job: Job) -> float:
        title = (job.title or "").lower()
        if not title:
            return WEIGHT_TITLE * 0.4
        for target in self.target_titles:
            if target in title:
                return float(WEIGHT_TITLE)
        # Partial credit for engineering-ish titles we did not list explicitly.
        for token in ("developer", "engineer", "programmer"):
            if token in title:
                return WEIGHT_TITLE * 0.6
        return 0.0

    def _score_experience(self, job: Job) -> Tuple[float, str]:
        required = job.required_experience_years
        if required is None:
            required = extract_required_experience(job.description or "")
        if required is None:
            return WEIGHT_EXPERIENCE * 0.7, "experience not stated"

        candidate = self.candidate_experience
        if required <= candidate:
            return float(WEIGHT_EXPERIENCE), "requires " + str(required) + "y, have " + str(candidate) + "y"
        gap = required - candidate
        if gap <= 1:
            return WEIGHT_EXPERIENCE * 0.6, "slightly under (" + str(required) + "y asked)"
        if gap <= 2:
            return WEIGHT_EXPERIENCE * 0.25, "under by " + str(gap) + "y"
        return 0.0, "needs " + str(required) + "y, far above profile"

    def _framework_mismatch(self, job: Job) -> Optional[str]:
        """Detect 'generic keywords match, but it is an Angular job' cases.

        Returns a reason when the posting is built on a rival framework and
        never mentions React / Next.js / Node / Redux.
        """
        haystack = ((job.title or "") + " " + (job.description or "")).lower()
        if not haystack.strip():
            return None
        if any(_skill_mentioned(skill, haystack) for skill in SIGNATURE_SKILLS):
            return None
        found = [f for f in COMPETING_FRAMEWORKS if _skill_mentioned(f, haystack)]
        if found:
            return "built on " + ", ".join(found[:3]) + " with no React/Next/Node"
        return None

    def _score_location(self, job: Job) -> float:
        location = ((job.location or "") + " " + (job.workplace_type or "")).lower()
        if not location.strip():
            return WEIGHT_LOCATION * 0.5
        for preferred in self.preferred_locations:
            if preferred in location:
                return float(WEIGHT_LOCATION)
        return 0.0

    def _score_type(self, job: Job) -> float:
        employment = (job.employment_type or "").lower()
        if not employment:
            return WEIGHT_TYPE * 0.6
        if "full" in employment:
            return float(WEIGHT_TYPE)
        if "contract" in employment or "part" in employment:
            return WEIGHT_TYPE * 0.3
        if "intern" in employment:
            return 0.0
        return WEIGHT_TYPE * 0.5

    # -- public API --------------------------------------------------------- #
    def score(self, job: Job) -> MatchResult:
        blocked = self.is_blocked(job)
        if blocked:
            return MatchResult(score=0, reasons=["BLOCKED: " + blocked])

        too_senior = self.exceeds_experience_range(job)
        if too_senior:
            return MatchResult(score=0, reasons=["BLOCKED: " + too_senior])

        reasons: List[str] = []
        skill_score, matched, missing = self._score_skills(job)
        title_score = self._score_title(job)
        exp_score, exp_note = self._score_experience(job)
        loc_score = self._score_location(job)
        type_score = self._score_type(job)

        total = skill_score + title_score + exp_score + loc_score + type_score

        mismatch = self._framework_mismatch(job)
        if mismatch:
            total -= COMPETING_PENALTY
            reasons.append("PENALTY -" + str(COMPETING_PENALTY) + ": " + mismatch)

        score = int(round(max(0.0, min(100.0, total))))

        reasons.append("skills " + str(round(skill_score)) + "/" + str(WEIGHT_SKILLS) +
                       (" (" + ", ".join(matched[:8]) + ")" if matched else " (none found)"))
        reasons.append("title " + str(round(title_score)) + "/" + str(WEIGHT_TITLE))
        reasons.append("experience " + str(round(exp_score)) + "/" + str(WEIGHT_EXPERIENCE) +
                       " - " + exp_note)
        reasons.append("location " + str(round(loc_score)) + "/" + str(WEIGHT_LOCATION))
        reasons.append("type " + str(round(type_score)) + "/" + str(WEIGHT_TYPE))
        if missing:
            reasons.append("missing core: " + ", ".join(sorted(missing)))

        return MatchResult(
            score=score,
            reasons=reasons,
            matched_skills=matched,
            missing_skills=missing,
        )


if __name__ == "__main__":  # python -m automation.job_matcher
    matcher = JobMatcher(
        skills={"react": 3, "next.js": 2, "javascript": 3, "node.js": 2, "typescript": 2,
                "docker": 1, "mongodb": 2},
        target_titles=["frontend developer", "react developer", "full stack developer"],
        blocked_title_keywords=["intern", "manager", "java developer"],
        blocked_company_keywords=[],
        preferred_locations=["gurugram", "delhi", "remote", "india"],
        candidate_experience_years=3,
    )

    good = Job(
        job_id="1",
        title="Frontend Developer (React.js)",
        company="Acme",
        location="Gurugram, Haryana, India",
        employment_type="Full-time",
        description="We need 2-4 years experience with React, Next.js, TypeScript, "
                    "JavaScript, Node.js and MongoDB. Docker knowledge is a plus.",
    )
    senior = Job(
        job_id="2",
        title="Software Engineer",
        company="Beta",
        location="Bengaluru, India",
        employment_type="Full-time",
        description="Looking for 8+ years of experience in Java, Spring Boot and Kafka.",
    )
    blocked = Job(job_id="3", title="Engineering Manager", company="Gamma", location="Remote")

    for job in (good, senior, blocked):
        result = matcher.score(job)
        print(str(result.score).rjust(3), "|", job.title)
        for reason in result.reasons:
            print("      -", reason)

    assert matcher.score(good).score >= 85, "strong match should score high"
    assert matcher.score(senior).score < 60, "far-off senior role should score low"
    assert matcher.score(blocked).score == 0, "blocked title must score 0"
    assert extract_required_experience("Minimum 5 years experience required") == 5
    assert extract_required_experience("2-4 years of experience") == 2
    assert extract_required_experience("great team, no numbers here") is None
    print("\njob_matcher self test OK")

# Automated Job Application Bot

Backend-only Python automation that searches **LinkedIn Easy Apply** and **Naukri.com**,
scores each job against your profile, fills the application, and records everything in
SQLite plus a date-wise CSV. Terminal only — no UI, no dashboard.

```
python main.py
```

---

## 1. How it works

```
main.py
  └─ browser (Playwright, persistent Chrome profile, visible window)
       ├─ authentication   session from your Chrome profile OR your exported cookie JSON
       ├─ job_search       builds filtered search URLs, harvests result cards
       ├─ job_parser       opens each job, reads title/company/location/description
       ├─ job_matcher      scores 0-100 against profile.yaml
       ├─ easy_apply       LinkedIn modal state machine  ┐
       │    └─ form_handler  discovers + fills + validates every field
       ├─ naukri           Naukri search / parse / apply ┘ (chatbot questionnaire)
       └─ application_tracker → SQLite + CSV + terminal summary
```

**Design decisions worth knowing:**

| Decision | Why |
|---|---|
| Persistent Chrome profile / cookie JSON | The bot never handles your password. It reuses a session **you** created. |
| Easy Apply treated as a bounded state machine | The modal has an unknown number of steps; each loop fills → validates → advances, capped at 12 steps. |
| Unknown question ⇒ stop, never guess | Any field not backed by `profile.yaml` prompts you in the terminal; if you skip it, the draft is **discarded** and the job is marked `NEEDS_HUMAN`. |
| `AUTO_SUBMIT=false` by default | First runs fill and validate everything without sending anything. |
| Circuit breaker | 3 consecutive failures stops the run instead of hammering the site. |
| Dedupe on job id → URL → company+title+location | Catches the same role re-posted under a new id. |

---

## 2. Install

```powershell
cd C:\Users\HP\Desktop\auto_job
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python -m playwright install chromium
```

---

## 3. Setup (required before the first run)

### a. Resume

Put your resume at `resume/resume.pdf` (PDF/DOC/DOCX, under 2 MB — LinkedIn's limit).

Optional role-specific variants are picked automatically when the job matches:

```
resume/resume.pdf              <- default
resume/resume_frontend.pdf     <- used for frontend/React jobs
resume/resume_fullstack.pdf    <- used for full stack/Node jobs
```

### b. Personal details — `config/profile.yaml`

Easiest way, once the resume is in place:

```powershell
python scripts/fill_profile.py
```

It reads the resume, shows you the name / email / phone / LinkedIn it found, and writes
them only after you confirm each one. `--show` prints the detections without writing.

Or replace every `<FILL ME>` by hand. Either way the bot refuses to start while
placeholders remain, so it can never invent your personal data.

Also worth setting:

```yaml
professional:
  notice_period_days: 30
  current_ctc_lpa: null      # null  -> the bot ASKS you instead of guessing
  expected_ctc_lpa: null     # put a number here to answer salary questions automatically
```

Everything else in that file is already filled with your stack (React, Next.js, Node.js,
TypeScript, Redux Toolkit, MySQL, MongoDB, Docker, Kubernetes, Tailwind, SCSS, Git)
and 3 years of experience at Booming Bulls.

### c. Sessions

**LinkedIn — Chrome profile (recommended)**

```dotenv
CHROME_USER_DATA_DIR=C:\Users\HP\AppData\Local\Google\Chrome\User Data
CHROME_PROFILE_DIRECTORY=Default
```

Chrome must be **fully closed** when the bot runs (Windows locks the profile).
Leave `CHROME_USER_DATA_DIR` empty to use a dedicated `./browser_profile` instead —
you log in once in the window the bot opens, and it is remembered afterwards.

**Naukri — cookie JSON (what you asked for)**

*Easiest — no extension needed, works like incognito:*

```powershell
python scripts/save_cookies.py naukri
```

A clean browser window opens (nothing saved to your Chrome profile). Log in there
yourself, press Enter in the terminal, and `cookies/naukri_cookies.json` is written
for you — including HttpOnly cookies that a DevTools console snippet cannot read.

*Or with the extension:*

1. Install the **Cookie-Editor** Chrome extension.
2. Log in to naukri.com in your own browser.
3. Cookie-Editor → **Export → Export as JSON** → copy.
4. Paste into `cookies/naukri_cookies.json`.

> In **incognito** windows Chrome disables extensions by default. To use Cookie-Editor
> there: `chrome://extensions` → Cookie-Editor → **Details** → turn on **Allow in Incognito**.
> The script above avoids this entirely.

```dotenv
PLATFORMS=linkedin,naukri
NAUKRI_COOKIES_PATH=cookies/naukri_cookies.json
```

Both the Cookie-Editor array format and Playwright's `storage_state` format are accepted.
Cookies are your live login session: `cookies/*.json` is gitignored — don't share it, and
re-export when the bot tells you the session is gone.

The browser window is **visible by default** (`HEADLESS=false`), so you can watch every
application happen.

---

## 4. Run

```powershell
python main.py                       # uses .env
python main.py --dry-run             # fill + validate, submit nothing
python main.py --submit --max 5      # actually submit, cap at 5
python main.py --platform naukri     # one platform only
python main.py --min-score 70
python main.py --keywords "React.js Developer,Next.js Developer"
python main.py --export-all          # also dump lifetime history to CSV
python main.py --summary             # stats only, no browser
```

**Recommended first run:** `python main.py --dry-run --max 3` — watch what it fills,
answer any terminal prompts, then check the CSV before enabling `--submit`.

---

## 5. Configuration (`.env`)

| Key | Meaning |
|---|---|
| `PLATFORMS` | `linkedin`, `naukri`, or both |
| `KEYWORDS` | comma separated search terms |
| `LOCATIONS` | **pipe** separated (locations contain commas) |
| `EXPERIENCE_MIN_YEARS` / `MAX` | mapped to LinkedIn's experience-level filter |
| `JOB_TYPES` | `F` full-time, `C` contract, `P` part-time |
| `EASY_APPLY_ONLY` | LinkedIn `f_AL` filter |
| `SORT_BY` | `DD` most recent, `R` most relevant |
| `DATE_POSTED` | `r86400` 24h, `r604800` 7 days, empty = any |
| `PAGES_PER_SEARCH` | result pages per keyword×location |
| `MIN_MATCH_SCORE` | 0-100 threshold to apply |
| `MAX_APPLICATIONS_PER_SESSION` | hard stop for submissions |
| `MAX_JOBS_TO_PROCESS` | hard stop for pages opened |
| `AUTO_SUBMIT` | **false = nothing is ever submitted** |
| `HEADLESS` | false = visible window |
| `INTERACTIVE` | true = ask you about unknown questions |
| `MAX_CONSECUTIVE_FAILURES` | circuit breaker |
| `*_DELAY` | request pacing, in seconds |
| `OUTPUT_DIR` | CSV root (date folders are created inside) |

---

## 6. Output

```
output/2026-11-23/applications_142530.csv   <- this run
output/2026-11-23/all_applications.csv      <- with --export-all
logs/2026-11-23/automation.log              <- rotating structured log
data/applications.sqlite3                   <- jobs, applications, questions, runs
data/answers.json                           <- answers you confirmed, reused next time
screenshots/                                <- captured on failures
```

CSV columns: `applied_at, platform, job_id, title, company, location, workplace_type,
employment_type, seniority, posted, required_experience_years, search_keyword,
search_location, match_score, match_reasons, status, resume_used, confirmation, error,
questions_answered, url, description`.

Written as UTF-8 with BOM so Excel opens it correctly.

### Status values

| Status | Meaning |
|---|---|
| `SUBMITTED` | sent, with LinkedIn/Naukri's confirmation text |
| `PREPARED_NOT_SUBMITTED` | form completed and validated, `AUTO_SUBMIT` off |
| `NEEDS_HUMAN` | unknown question, validation error, or company-site redirect |
| `DUPLICATE` | already processed in an earlier run |
| `SKIPPED_LOW_SCORE` / `SKIPPED_BLOCKED` / `SKIPPED_NO_EASY_APPLY` / `SKIPPED_ALREADY_APPLIED` | skip reasons |
| `FAILED` | crash or page error (screenshot saved) |

---

## 7. Scoring

| Component | Weight | Rule |
|---|---|---|
| Skills | 40 | breadth of profile skills found + depth on core skills (React/Next/Node/TS/JS). Aliases (`react` = `reactjs` = `react.js`) count as one skill. |
| Title | 25 | full credit for a `target_titles` hit, 60% for a generic developer/engineer title |
| Experience | 20 | full when the demanded minimum ≤ your years; partial within 2 years; 0 beyond |

**Hard ceiling:** a posting demanding more than `EXPERIENCE_MAX_YEARS` is scored **0** and skipped outright on both LinkedIn and Naukri, however well the stack matches. With `EXPERIENCE_MIN_YEARS=2` / `EXPERIENCE_MAX_YEARS=4`, a "5+ years" role never gets applied to.
| Location | 10 | full for a `preferred_locations` hit |
| Employment type | 5 | full-time full, contract/part-time 30%, internship 0 |

A `blocked_title_keywords` hit scores **0** immediately (manager, intern, QA, Java, etc.).
Tune the lists in `profile.yaml` — that is the main lever on what gets applied to.

---

## 8. Test each module on its own

```powershell
python -m unittest discover -s tests -t . -v   # 24 offline tests

python config.py                     # config + validation
python -m database.db                # schema + duplicate detection
python -m automation.job_matcher     # scoring
python -m services.question_handler  # answer resolution
python -m services.resume_manager    # resume checks
python -m services.exporter          # CSV writing
python -m automation.naukri          # Naukri URL builder
```

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not launch channel 'chrome'` | Chrome is open (close it) or not installed — it falls back to bundled Chromium automatically |
| Asked to sign in every run | You are using the dedicated profile; log in once in that window, or set `CHROME_USER_DATA_DIR` |
| Naukri asks to sign in | Cookies expired — re-export `cookies/naukri_cookies.json` |
| Everything is `SKIPPED_LOW_SCORE` | Lower `MIN_MATCH_SCORE` or widen `skills` / `target_titles` |
| Many `NEEDS_HUMAN` | Run interactively once and answer the prompts — answers are remembered in `data/answers.json` |
| Nothing submits | `AUTO_SUBMIT=false` is the default; use `--submit` |
| Naukri: `Access Denied` | Naukri's edge WAF rejects **headless** browsers. Run without `--headless` (the default). |
| Naukri dry-run shows `PREPARED_NOT_SUBMITTED` but nothing was filled | On Naukri the first click *is* the application, so dry-run deliberately locates the Apply button without clicking. Use `--submit` for a real application. |
| Security checkpoint | Complete it yourself in the window, then re-run. The bot never tries to work around it. |

---

## 10. Boundaries

- No password automation, no CAPTCHA/2FA solving, no security-check bypass — the run
  stops and hands control to you.
- No invented experience, salary, qualifications or personal details. Every answer comes
  from `profile.yaml`/your resume or from you, or the application is abandoned.
- Conservative pacing and per-session caps; the run stops when a platform signals that
  automated access is restricted.
- Automated applying can be against a platform's terms of service. You are running this on
  your own account, at your own discretion — keep volumes low and review the CSV.

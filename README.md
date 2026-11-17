# AutoApply

An AI job-application agent you run yourself. It watches company career pages, scores each
new role against your profile, rewrites your résumé for the role, fills the employer's own
application form in a real browser, and submits it after you approve.

It follows the same four-stage design as hosted auto-apply products (find → prep → apply →
track), rebuilt from their public documentation. No code from any of them is used.

## What works today

| Stage | What it does |
| --- | --- |
| Find | Polls the public job feeds of Greenhouse, Lever and Ashby boards listed in `data/companies.yaml`. Paste any job link from those three to add it directly. |
| Match | 0-100 score from title fit, skill coverage and location. Hard filters for excluded title words and years of experience. No LLM cost. |
| Prep | Claude rewrites your résumé for the job and drafts a cover letter. A fact check restores every employer, title, date and degree from your profile and drops skills you didn't list. You see a diff before anything is sent. |
| Apply | Playwright opens the form, reads every field (text, dropdowns, radios, checkboxes, custom comboboxes, yes/no buttons, uploads), answers from your profile, and asks the LLM only for open-ended questions. Pauses with a screenshot for your review, then submits. |
| Track | Every application keeps a receipt: each field and the value sent, the résumé PDF, the confirmation screenshot. Stages: applied, interviewing, offer, rejected, ghosted. |

Auto Apply (`autoapply auto`) queues the best new matches above your threshold, up to a daily cap.

## Setup

```bash
git clone <this repo> && cd autoapply
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium        # skip if Google Chrome is installed
cp .env.example .env               # add GEMINI_API_KEY (free) or ANTHROPIC_API_KEY
autoapply init                     # creates data/profile.yaml and data/companies.yaml
```

Fill in `data/profile.yaml`. Everything the agent says about you comes from that file, so
be complete: work authorization, sponsorship, salary expectation, and an `about_me`
paragraph for "why this company" questions. Then:

```bash
autoapply check                    # validates the profile
autoapply llm-test                 # one real call to confirm your API key works
autoapply add-titles               # target ~60 role titles (SWE, AI/ML, solutions, support, data, infra, IT)
autoapply companies-update         # add the built-in company list (175 boards) to data/companies.yaml
autoapply discover                 # pull and score jobs
autoapply serve                    # dashboard at http://127.0.0.1:8000
```

## Daily use

Dashboard: **Matches** → Apply. The application moves through:

1. **Review résumé**: diff of the tailored version, editable cover letter.
2. **Review form**: every field and its answer, editable, with a screenshot.
3. **Submitted**: receipt and confirmation screenshot.

Or from the terminal:

```bash
autoapply jobs                     # best unapplied matches
autoapply apply 123                # by job id, or paste a job URL
autoapply approve 7                # approve whatever application 7 is waiting on
autoapply auto                     # discover + queue top matches + process
autoapply status
```

To run hands-free, set `AUTOAPPLY_AUTO_APPROVE_RESUME=true` and
`AUTOAPPLY_REVIEW_BEFORE_SUBMIT=false` in `.env`, then schedule `autoapply auto` (cron, or
Task Scheduler on Windows). Start with reviews on until you trust the answers.

## Settings (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `GEMINI_API_KEY` | (none) | Free option, no card: get a key at https://aistudio.google.com/apikey. On the free tier Google may use your prompts (your résumé text) to improve its products. |
| `ANTHROPIC_API_KEY` | (none) | Paid option. Set one of the two keys; it powers tailoring, cover letters and open-ended answers. Without either, the base résumé is used and unknown questions are left for you. |
| `AUTOAPPLY_LLM_PROVIDER` | auto | `gemini` or `anthropic`. Only needed if both keys are set. |
| `AUTOAPPLY_MODEL` | per provider | `gemini-3.5-flash-lite` (about 500 free requests/day) or `claude-sonnet-5-5`. |
| `AUTOAPPLY_HEADLESS` | `false` | Show the browser. Keep it visible so you can solve a CAPTCHA when one appears. |
| `AUTOAPPLY_REVIEW_BEFORE_SUBMIT` | `true` | Pause on the filled form. |
| `AUTOAPPLY_AUTO_APPROVE_RESUME` | `false` | Skip the résumé diff review. |
| `AUTOAPPLY_MATCH_THRESHOLD` | `70` | Minimum score for Auto Apply. |
| `AUTOAPPLY_DAILY_CAP` | `20` | Max submissions per day. |
| `AUTOAPPLY_MAX_AGE_HOURS` | `72` | Auto Apply only considers roles newer than this. |
| `IMAP_HOST` / `IMAP_USER` / `IMAP_PASSWORD` | (none) | Mailbox for emailed verification codes (Gmail: `imap.gmail.com` + an app password). |
| `DATABASE_URL` | SQLite in `data/` | Any SQLAlchemy URL, e.g. Postgres. |

## How answers are chosen

Per field, first hit wins:

1. **approved**: a value you edited in the review screen
2. **rule**: mapped from your profile (name, contact, links, work authorization, sponsorship, EEO, school, current employer, consent boxes)
3. **saved**: a choice or short fact you approved on an earlier form with the same question
4. **llm**: open-ended questions, answered only from your profile; if the profile doesn't cover it, the field is left blank

A required field with no answer pauses the application instead of submitting.

## Limits

- **Tested against a local fixture form, not yet against live employer forms.** Expect to tune selectors in `apply/adapters.py` on the first real Greenhouse, Lever and Ashby runs.
- **CAPTCHAs are not bypassed.** With the browser visible, the engine waits for you to solve one; headless, the application is marked "needs you".
- **No account-wall ATSes yet** (Workday, iCIMS, Taleo). The form reader is generic, so adding one is an adapter plus a login step.
- The company slugs in the seed list were checked against the live job feeds in November 2026; `autoapply discover` reports any that later fail.
- Some employers' terms restrict automated applications. That's your call per employer; the daily cap and review gates default to conservative.

## Layout

```
autoapply/
  discovery/        job feeds + pasted-link parsing
  matching.py       scoring
  tailor.py         résumé rewrite, fact check, diff, PDF
  apply/forms.py    generic form reader and field filler
  apply/answers.py  rule / saved / LLM answer resolution
  apply/adapters.py per-ATS selectors and success detection
  engine.py         the pipeline and its review gates
  inbox.py          IMAP verification codes
  api.py, web/      dashboard
  cli.py
tests/              11 tests, including a full fill-review-submit run on a fixture form
```

Run tests with `pytest`.

# Job Application Agent

AI-assisted job-hunt pipeline: reads job-alert emails (LinkedIn etc.), filters
to remote roles, scores them against your profile, and drafts tailored cover
letters and emails for **your review** before anything is sent.

## Setup (one-time)

### 1. Python environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Environment variables

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in:
- `ANTHROPIC_API_KEY` — get one at https://console.anthropic.com/
- `USER_EMAIL` — your Gmail address
- `USER_FULL_NAME` — used in cover letters and signatures

### 3. Drop your resumes in `resumes/`

Multiple PDFs are fine — the parser merges them.

### 4. Generate the unified profile

```powershell
python -m agent.profile_loader
```

This writes `profile.yaml` (gitignored). The rest of the pipeline reads from
this file. Re-run any time you add or update a resume.

### 5. Authorize Gmail

```powershell
python -m agent.gmail_client
```

Opens a browser the first time. Saves `token.json` (gitignored) so subsequent
runs don't re-prompt.

## Project layout

```
agent/
  gmail_client.py       Gmail OAuth + read + send
  profile_loader.py     resume PDFs -> profile.yaml (Claude API)
  ...                   (more pipeline stages added in phases 2 + 3)
resumes/                drop your CV PDFs here (gitignored)
templates/              Flask Jinja templates (phase 3)
output/                 generated cover letters / resume PDFs (gitignored)
```

## Phase status

- [x] Phase 1: Gmail OAuth + resume profile parser
- [x] Phase 2: email classifier, job extractor, remote filter, scorer, content + PDF generators
- [x] Phase 3: Flask review UI, SQLite logging, send-on-approval

## Running the app

```powershell
.\.venv\Scripts\Activate.ps1
python app.py
```

Open http://127.0.0.1:5000.

Workflow:
1. Click **Fetch & score** to pull LinkedIn job alerts and let Claude rank them
2. Click any row to review one job — match analysis, draft email, draft cover letter, PDFs
3. **Approve** to whitelist for sending; **Reject** to dismiss
4. Enter a recipient address and click **Send application** to email the drafts + PDFs via your Gmail account
5. Sent jobs are logged with the Gmail message id

The CLI pipeline still works for headless batch runs:
```powershell
python -m agent.pipeline --max-emails 5 --threshold 50 --top-n 3
```
Either entry point writes into the same `jobs.db`.

## Repeat-fetch optimization

The pipeline remembers every email it has already extracted (in a `processed_emails` table). On a repeat fetch it skips those emails entirely — no extraction call to Claude, no scoring. So clicking **Fetch & score** twice in a row only costs API credits if Gmail surfaces new messages since the last run.

## Strict rules (from the spec)

- Never auto-apply without explicit user approval
- Never fabricate skills or experience
- Only process job-alert emails; only keep remote roles
- Secrets live in `.env` / `credentials.json` / `token.json` — never commit them

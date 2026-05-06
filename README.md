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
- [ ] Phase 2: email classifier, job extractor, remote filter, scorer, content + PDF generators
- [ ] Phase 3: Flask review UI, SQLite logging, send-on-approval

## Strict rules (from the spec)

- Never auto-apply without explicit user approval
- Never fabricate skills or experience
- Only process job-alert emails; only keep remote roles
- Secrets live in `.env` / `credentials.json` / `token.json` — never commit them

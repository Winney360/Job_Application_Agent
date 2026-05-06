"""Classify an email AND extract jobs from it in a single Claude call.

LinkedIn job-alert emails typically contain 5-25 jobs. This module turns one
email body into a list of structured Job records. It also flags the email
type so we can ignore newsletters, marketing, and other noise upstream.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import Literal

import anthropic
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from agent.gmail_client import FetchedEmail

DEFAULT_MODEL = "claude-opus-4-7"
MAX_BODY_CHARS = 60_000  # truncate huge promotional emails before sending to Claude


class EmailKind(str, Enum):
    JOB_ALERT = "job_alert"
    NEWSLETTER = "newsletter"
    MARKETING = "marketing"
    OTHER = "other"


class Job(BaseModel):
    company: str
    role: str
    location: str = Field(default="", description="Verbatim location string from the listing.")
    remote: bool = Field(
        description="True only if the listing explicitly says remote, work-from-home, or fully remote."
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Concrete technical skills mentioned (e.g. React, Python, AWS). Skip soft skills.",
    )
    experience: str = Field(
        default="",
        description="Required experience as written, e.g. '2+ years', 'Senior', 'Entry-level'. Empty if not stated.",
    )
    job_url: str = Field(
        default="",
        description="The application/job-detail URL if present. Prefer the deepest link, not a tracking redirect summary.",
    )
    posted: str = Field(
        default="",
        description="Posting date or relative phrase like '3 days ago' if visible.",
    )
    apply_email: str = Field(
        default="",
        description=(
            "Email address for applying, ONLY if the listing explicitly says something "
            "like 'apply to recruiter@company.com', 'send your CV to ...', or shows a "
            "contact email. Leave empty if the listing only points to a URL."
        ),
    )


class ExtractionResult(BaseModel):
    kind: EmailKind
    source: Literal["linkedin", "indeed", "wellfound", "glassdoor", "other"] = "other"
    jobs: list[Job] = Field(default_factory=list)


SYSTEM_PROMPT = """You are an email parser specialized in job-alert emails.

Given a single email, do BOTH of these:
1. Classify the email as one of: job_alert, newsletter, marketing, other.
   - job_alert: contains a list of specific job postings (LinkedIn "jobs picked for you", Indeed "new jobs", etc.)
   - newsletter: industry news, blog digests
   - marketing: promotions, sales pitches, course ads
   - other: anything else
2. If kind == job_alert, extract every distinct job posting in the body.

Rules for extraction:
- Be exhaustive: pull EVERY job, not just the first few.
- Set remote=true ONLY if the listing explicitly mentions remote / work from home / fully remote / WFH. "Hybrid", "flexible", or unclear locations are NOT remote.
- Skills must be concrete tech (React, Node.js, AWS, Python, GraphQL, etc.). Skip soft skills.
- For job_url, prefer the canonical job-detail URL. If only a tracking redirect is present, use that.
- For apply_email, extract ONLY if a contact email is explicitly written in the listing. Never invent or guess from the company domain.
- Do not invent fields. If a value is missing, use an empty string."""


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # Preserve link URLs inline so the model can extract job_url
    for a in soup.find_all("a", href=True):
        a.append(f" <{a['href']}>")
    text = soup.get_text(separator="\n")
    # Collapse runs of whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _email_to_text(email: FetchedEmail) -> str:
    if email.body_text and len(email.body_text) > 200:
        body = email.body_text
    elif email.body_html:
        body = _html_to_text(email.body_html)
    else:
        body = email.body_text or email.snippet
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n[truncated]"
    return body


def extract(email: FetchedEmail, *, model: str | None = None) -> ExtractionResult:
    client = anthropic.Anthropic()
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    body = _email_to_text(email)
    user_content = (
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.date}\n"
        f"---\n{body}"
    )

    response = client.messages.parse(
        model=model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=ExtractionResult,
    )
    return response.parsed_output


if __name__ == "__main__":
    # Smoke test against the most recent LinkedIn-looking email.
    from dotenv import load_dotenv

    from agent.gmail_client import fetch_messages

    load_dotenv()
    query = "from:linkedin.com (subject:jobs OR subject:opportunities) newer_than:14d"
    print(f"Searching: {query}")
    found = list(fetch_messages(query, max_results=1))
    if not found:
        print("No matching emails. Try widening the query.")
        raise SystemExit(0)
    email = found[0]
    print(f"\nEmail: {email.subject}")
    print(f"From:  {email.sender}\n")
    result = extract(email)
    print(f"Kind:   {result.kind.value}")
    print(f"Source: {result.source}")
    print(f"Jobs:   {len(result.jobs)}")
    for j in result.jobs:
        flag = "REMOTE" if j.remote else "      "
        print(f"  [{flag}] {j.role} @ {j.company} — {j.location}")

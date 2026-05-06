"""Keep only remote jobs and deduplicate across emails.

Dedupe key: (normalized company, normalized role, job_url). The first two
catch repostings without a URL; the URL catches the same listing under a
slightly renamed role.
"""
from __future__ import annotations

import re

from agent.extractor import Job

REMOTE_HINTS = (
    "remote",
    "work from home",
    "wfh",
    "fully remote",
    "anywhere",
    "distributed",
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _looks_remote(job: Job) -> bool:
    if job.remote:
        return True
    location = job.location.lower()
    return any(hint in location for hint in REMOTE_HINTS)


def filter_remote(jobs: list[Job]) -> list[Job]:
    return [j for j in jobs if _looks_remote(j)]


def dedupe(jobs: list[Job]) -> list[Job]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Job] = []
    for j in jobs:
        key = (_normalize(j.company), _normalize(j.role), j.job_url.strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(j)
    return out


def filter_and_dedupe(jobs: list[Job]) -> list[Job]:
    return dedupe(filter_remote(jobs))

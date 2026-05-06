"""Score remote jobs against the user's parsed profile.

Batched: a single Claude call scores up to N jobs at once. The user profile
goes in the (cached) system prompt so repeated batches are cheap on tokens.
"""
from __future__ import annotations

import json
import os

import anthropic
from pydantic import BaseModel, Field

from agent.extractor import Job
from agent.profile_loader import Profile

DEFAULT_MODEL = "claude-opus-4-7"
MAX_BATCH = 25  # safety: split large batches across calls


class JobScore(BaseModel):
    index: int = Field(description="Zero-based index of the job within the batch.")
    score: int = Field(ge=0, le=100, description="0-100 fit score.")
    matched_skills: list[str] = Field(
        default_factory=list,
        description="Profile skills that overlap with the job's requirements.",
    )
    missing_skills: list[str] = Field(
        default_factory=list,
        description="Job-required skills the profile does NOT have.",
    )
    reason: str = Field(description="Two-sentence explanation tied to concrete profile details.")


class BatchScores(BaseModel):
    scores: list[JobScore]


SYSTEM_PROMPT = """You are a candid technical recruiter scoring how well a candidate fits each remote job.

Scoring rubric (0-100):
- 80-100: Strong match. Target role, required skills present, experience level fits.
- 60-79:  Good match. Most key skills present; minor gaps.
- 40-59:  Partial. Some overlap but a meaningful gap (skills, seniority, or domain).
- 20-39:  Weak. Single shared skill or adjacent stack only.
- 0-19:   Not a fit. Wrong stack/seniority.

Rules:
- NEVER fabricate or assume skills not in the candidate profile.
- Penalize listings that demand seniority the profile does not show.
- Reward listings whose stack matches the candidate's strongest projects.
- `reason` must reference SPECIFIC items from the profile (e.g. "MERN projects align with the Node.js/MongoDB requirement")."""


def _profile_block(profile: Profile) -> str:
    """Compact, deterministic profile snapshot for the system prompt."""
    return (
        "CANDIDATE PROFILE\n"
        f"Headline: {profile.headline}\n"
        f"Target roles: {', '.join(profile.target_roles)}\n"
        f"Skills: {', '.join(profile.skills)}\n"
        + "Projects:\n"
        + "\n".join(
            f"- {p.name} ({', '.join(p.tech)}): {p.description}" for p in profile.projects
        )
        + "\nExperience:\n"
        + "\n".join(
            f"- {e.role} @ {e.company} ({e.duration})" for e in profile.experience
        )
    )


def _jobs_block(jobs: list[Job]) -> str:
    rows = []
    for i, j in enumerate(jobs):
        rows.append(
            f"[{i}] {j.role} @ {j.company}\n"
            f"    skills: {', '.join(j.skills) or '—'}\n"
            f"    experience: {j.experience or '—'}\n"
            f"    location: {j.location or '—'}"
        )
    return "JOBS TO SCORE\n" + "\n\n".join(rows)


def score_jobs(
    jobs: list[Job],
    profile: Profile,
    *,
    model: str | None = None,
) -> list[JobScore]:
    if not jobs:
        return []

    client = anthropic.Anthropic()
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    out: list[JobScore] = []
    for start in range(0, len(jobs), MAX_BATCH):
        batch = jobs[start : start + MAX_BATCH]
        response = client.messages.parse(
            model=model,
            max_tokens=4000,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT},
                {
                    "type": "text",
                    "text": _profile_block(profile),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[{"role": "user", "content": _jobs_block(batch)}],
            output_format=BatchScores,
        )
        # Re-base indices to the global jobs list
        for s in response.parsed_output.scores:
            s.index += start
            out.append(s)
    # Sort by global index so callers can zip with the input list
    out.sort(key=lambda s: s.index)
    return out


def score_and_rank(
    jobs: list[Job],
    profile: Profile,
    *,
    threshold: int = 0,
) -> list[tuple[Job, JobScore]]:
    """Return (job, score) pairs above the threshold, sorted by score desc."""
    scores = score_jobs(jobs, profile)
    pairs = [(jobs[s.index], s) for s in scores if s.score >= threshold]
    pairs.sort(key=lambda pair: pair[1].score, reverse=True)
    return pairs


if __name__ == "__main__":
    from dotenv import load_dotenv

    from agent.profile_loader import load_profile

    load_dotenv()
    profile = load_profile()
    sample = [
        Job(
            company="Acme Corp",
            role="Senior Backend Engineer (Go)",
            location="Remote",
            remote=True,
            skills=["Go", "Kubernetes", "PostgreSQL"],
            experience="5+ years",
        ),
        Job(
            company="Beta Labs",
            role="Frontend Developer Intern",
            location="Remote",
            remote=True,
            skills=["React", "TypeScript", "Tailwind CSS"],
            experience="0-1 year",
        ),
    ]
    for job, score in score_and_rank(sample, profile):
        print(f"[{score.score:>3}]  {job.role} @ {job.company}")
        print(f"       matched: {', '.join(score.matched_skills) or '—'}")
        print(f"       missing: {', '.join(score.missing_skills) or '—'}")
        print(f"       {score.reason}\n")

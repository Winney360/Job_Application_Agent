"""Generate tailored application materials for a single job.

One Claude call returns:
  - resume_summary  (3-4 line headline+pitch tailored to the role)
  - cover_letter    (concise, one page, ATS-friendly)
  - email_subject
  - email_body      (short, polite, references the role)

Strict: NEVER fabricate skills/experience not in the user profile.
"""
from __future__ import annotations

import os

import anthropic
from pydantic import BaseModel, Field

from agent.extractor import Job
from agent.profile_loader import Profile

DEFAULT_MODEL = "claude-opus-4-7"


class ApplicationMaterials(BaseModel):
    resume_summary: str = Field(
        description="3-4 line professional summary tailored to this role. Plain text. No markdown."
    )
    cover_letter: str = Field(
        description=(
            "Full cover letter, ~250-350 words, plain text, no markdown. "
            "Address to 'Dear Hiring Manager' if no contact name is known. "
            "Sign off with the candidate's full name."
        )
    )
    email_subject: str = Field(
        description="Email subject line, e.g. 'Application: <Role> — <Candidate Name>'."
    )
    email_body: str = Field(
        description="Short email body (~120-180 words) referencing the role and noting attachments."
    )


SYSTEM_PROMPT = """You write tailored job applications for ONE candidate at a time, given their full profile and a job description.

NON-NEGOTIABLE RULES:
- NEVER invent skills, projects, employers, certifications, or experience that are not in the candidate profile.
- NEVER claim years of experience the profile does not support.
- If a job requirement is missing from the profile, do not lie — emphasize adjacent strengths and willingness to learn.
- Keep tone professional, warm, and direct. No flowery language, no buzzword stuffing.
- Cover letter must read like a human wrote it — concrete examples from the candidate's actual projects/experience.
- ATS-friendly: plain text only, no special characters, no markdown, no emoji.
- Use the candidate's full name in signatures."""


def _profile_for_prompt(profile: Profile) -> str:
    parts = [
        f"Name: {profile.full_name}",
        f"Email: {profile.email}",
    ]
    if profile.location:
        parts.append(f"Location: {profile.location}")
    if profile.links:
        parts.append(f"Links: {', '.join(profile.links)}")
    parts.append(f"Headline: {profile.headline}")
    parts.append(f"Skills: {', '.join(profile.skills)}")
    if profile.experience:
        parts.append("Experience:")
        for e in profile.experience:
            parts.append(f"  - {e.role} @ {e.company} ({e.duration})")
            for h in e.highlights[:5]:
                parts.append(f"      • {h}")
    if profile.projects:
        parts.append("Projects:")
        for p in profile.projects:
            tech = f" [{', '.join(p.tech)}]" if p.tech else ""
            parts.append(f"  - {p.name}{tech}: {p.description}")
    if profile.education:
        parts.append("Education: " + "; ".join(profile.education))
    return "\n".join(parts)


def _job_for_prompt(job: Job) -> str:
    parts = [
        f"Company: {job.company}",
        f"Role: {job.role}",
        f"Location: {job.location or '—'}  (remote: {job.remote})",
        f"Required skills: {', '.join(job.skills) or '—'}",
        f"Experience required: {job.experience or '—'}",
    ]
    if job.job_url:
        parts.append(f"Job URL: {job.job_url}")
    return "\n".join(parts)


def generate_materials(
    job: Job,
    profile: Profile,
    *,
    model: str | None = None,
) -> ApplicationMaterials:
    client = anthropic.Anthropic()
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    user_message = (
        "CANDIDATE PROFILE\n"
        f"{_profile_for_prompt(profile)}\n\n"
        "JOB POSTING\n"
        f"{_job_for_prompt(job)}\n\n"
        "Produce the four fields per the schema. Use ONLY facts from the candidate profile."
    )

    response = client.messages.parse(
        model=model,
        max_tokens=3000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
        output_format=ApplicationMaterials,
    )
    return response.parsed_output


if __name__ == "__main__":
    from dotenv import load_dotenv

    from agent.profile_loader import load_profile

    load_dotenv()
    profile = load_profile()
    job = Job(
        company="Beta Labs",
        role="Frontend Developer Intern",
        location="Remote",
        remote=True,
        skills=["React", "TypeScript", "Tailwind CSS"],
        experience="0-1 year",
        job_url="https://example.com/jobs/123",
    )
    materials = generate_materials(job, profile)
    print("=== RESUME SUMMARY ===\n", materials.resume_summary, "\n")
    print("=== EMAIL SUBJECT ===\n", materials.email_subject, "\n")
    print("=== EMAIL BODY ===\n", materials.email_body, "\n")
    print("=== COVER LETTER ===\n", materials.cover_letter)

"""Parse resume PDFs into a unified user profile.

Reads every PDF in resumes/, sends them to Claude with a Pydantic schema, and
writes the merged result to profile.yaml. Run this once after dropping new
resumes in; the rest of the pipeline reads profile.yaml.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

import anthropic
import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
RESUMES_DIR = ROOT / "resumes"
PROFILE_PATH = ROOT / "profile.yaml"

DEFAULT_MODEL = "claude-opus-4-7"


class Project(BaseModel):
    name: str
    description: str = Field(description="One-sentence summary of what was built and the impact.")
    tech: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    role: str
    company: str
    duration: str = Field(description="Free-text duration, e.g. 'Jan 2024 - Present'.")
    highlights: list[str] = Field(
        default_factory=list,
        description="Up to 5 bullet points, each starting with a strong action verb.",
    )


class Profile(BaseModel):
    full_name: str
    email: str
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(
        default_factory=list,
        description="Public profile URLs only (LinkedIn, GitHub, portfolio).",
    )
    headline: str = Field(
        description="One-line professional summary suitable for the top of a resume."
    )
    skills: list[str] = Field(
        description="Deduplicated technical skills, normalized (e.g. 'JavaScript' not 'JS')."
    )
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(
        default_factory=list,
        description="Roles this candidate is best positioned for, inferred from the resumes.",
    )


def _pdf_blocks(pdf_paths: list[Path]) -> list[dict]:
    blocks: list[dict] = []
    for path in pdf_paths:
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        blocks.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            "title": path.name,
        })
    return blocks


SYSTEM_PROMPT = (
    "You are a careful resume parser. Read the attached resume PDFs (which may be "
    "different versions of the same person's CV targeting different roles) and "
    "produce ONE unified, deduplicated profile. Rules:\n"
    "- Never invent skills, experience, projects, or dates that aren't in the documents.\n"
    "- Merge duplicate entries across resumes; prefer the most detailed wording.\n"
    "- Normalize skill names (e.g. 'React.js' -> 'React', 'Tailwind CSS' -> 'Tailwind CSS').\n"
    "- Infer 'target_roles' from the resume titles/headlines and tech stacks present.\n"
    "- Keep email, phone, location only if explicitly written in the documents."
)


def parse_resumes(
    *,
    resumes_dir: Path = RESUMES_DIR,
    model: str | None = None,
) -> Profile:
    pdfs = sorted(resumes_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDFs found in {resumes_dir}")

    client = anthropic.Anthropic()
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    response = client.messages.parse(
        model=model,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                *_pdf_blocks(pdfs),
                {
                    "type": "text",
                    "text": (
                        "Parse these resumes into the unified Profile schema. "
                        "Return a single merged profile."
                    ),
                },
            ],
        }],
        output_format=Profile,
    )
    return response.parsed_output


def save_profile(profile: Profile, path: Path = PROFILE_PATH) -> None:
    data = profile.model_dump(exclude_none=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    if not path.exists():
        raise FileNotFoundError(
            f"{path.name} not found. Run `python -m agent.profile_loader` to generate it."
        )
    return Profile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    print(f"Parsing resumes in {RESUMES_DIR}...")
    profile = parse_resumes()
    save_profile(profile)
    print(f"Wrote {PROFILE_PATH.relative_to(ROOT)}")
    print(f"  Name:         {profile.full_name}")
    print(f"  Email:        {profile.email}")
    print(f"  Skills:       {len(profile.skills)} items")
    print(f"  Experience:   {len(profile.experience)} roles")
    print(f"  Projects:     {len(profile.projects)} projects")
    print(f"  Target roles: {', '.join(profile.target_roles)}")

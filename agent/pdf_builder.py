"""Render application PDFs with ReportLab.

Two outputs per job:
  - <slug>_cover_letter.pdf
  - <slug>_resume.pdf

Resume is built from the user's structured profile + a tailored summary.
Cover letter is just the generated text on letterhead.
"""
from __future__ import annotations

import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from agent.extractor import Job
from agent.generator import ApplicationMaterials
from agent.profile_loader import Profile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return re.sub(r"_+", "_", s).strip("_") or "untitled"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "Name",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=20,
            leading=24,
            spaceAfter=2,
            alignment=TA_LEFT,
        ),
        "contact": ParagraphStyle(
            "Contact",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=10,
            textColor=colors.HexColor("#444444"),
            spaceAfter=10,
        ),
        "section": ParagraphStyle(
            "Section",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=colors.HexColor("#1a1a1a"),
            spaceBefore=10,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            spaceAfter=6,
            alignment=TA_JUSTIFY,
        ),
        "bullet": ParagraphStyle(
            "Bullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=14,
            leftIndent=14,
            bulletIndent=2,
            spaceAfter=2,
        ),
        "label": ParagraphStyle(
            "Label",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=14,
            spaceAfter=2,
        ),
    }


def _hr() -> HRFlowable:
    return HRFlowable(
        width="100%",
        thickness=0.5,
        color=colors.HexColor("#cccccc"),
        spaceBefore=2,
        spaceAfter=4,
    )


def _esc(text: str) -> str:
    """Escape text for ReportLab Paragraph (it parses HTML-like markup)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _doc(path: Path) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        str(path),
        pagesize=LETTER,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
        title=path.stem,
    )


def _header(profile: Profile, styles: dict[str, ParagraphStyle]) -> list:
    contact_bits = [profile.email]
    if profile.phone:
        contact_bits.append(profile.phone)
    if profile.location:
        contact_bits.append(profile.location)
    contact_bits.extend(profile.links)
    return [
        Paragraph(_esc(profile.full_name), styles["name"]),
        Paragraph(_esc(" | ".join(contact_bits)), styles["contact"]),
    ]


def build_resume_pdf(
    profile: Profile,
    materials: ApplicationMaterials,
    *,
    out_path: Path,
) -> Path:
    styles = _styles()
    story: list = _header(profile, styles)

    story.append(Paragraph("Summary", styles["section"]))
    story.append(_hr())
    story.append(Paragraph(_esc(materials.resume_summary), styles["body"]))

    if profile.skills:
        story.append(Paragraph("Skills", styles["section"]))
        story.append(_hr())
        story.append(Paragraph(_esc(", ".join(profile.skills)), styles["body"]))

    if profile.experience:
        story.append(Paragraph("Experience", styles["section"]))
        story.append(_hr())
        for e in profile.experience:
            story.append(
                Paragraph(
                    f"<b>{_esc(e.role)}</b> &nbsp;—&nbsp; {_esc(e.company)} &nbsp; "
                    f"<font color='#666666'>{_esc(e.duration)}</font>",
                    styles["label"],
                )
            )
            for h in e.highlights:
                story.append(Paragraph("• " + _esc(h), styles["bullet"]))
            story.append(Spacer(1, 4))

    if profile.projects:
        story.append(Paragraph("Projects", styles["section"]))
        story.append(_hr())
        for p in profile.projects:
            tech = f" <font color='#666666'>[{_esc(', '.join(p.tech))}]</font>" if p.tech else ""
            story.append(Paragraph(f"<b>{_esc(p.name)}</b>{tech}", styles["label"]))
            story.append(Paragraph(_esc(p.description), styles["body"]))

    if profile.education:
        story.append(Paragraph("Education", styles["section"]))
        story.append(_hr())
        for ed in profile.education:
            story.append(Paragraph("• " + _esc(ed), styles["bullet"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _doc(out_path).build(story)
    return out_path


def build_cover_letter_pdf(
    profile: Profile,
    materials: ApplicationMaterials,
    *,
    out_path: Path,
) -> Path:
    styles = _styles()
    story: list = _header(profile, styles)
    story.append(Spacer(1, 10))
    for paragraph in materials.cover_letter.split("\n\n"):
        text = paragraph.strip()
        if not text:
            continue
        # Convert single newlines to <br/> so signature blocks render
        rendered = "<br/>".join(_esc(line) for line in text.split("\n"))
        story.append(Paragraph(rendered, styles["body"]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _doc(out_path).build(story)
    return out_path


def build_application_pdfs(
    profile: Profile,
    job: Job,
    materials: ApplicationMaterials,
    *,
    output_dir: Path = OUTPUT_DIR,
) -> dict[str, Path]:
    base = _slug(f"{job.company}_{job.role}")
    resume_path = output_dir / f"{base}_resume.pdf"
    cover_path = output_dir / f"{base}_cover_letter.pdf"
    return {
        "resume": build_resume_pdf(profile, materials, out_path=resume_path),
        "cover_letter": build_cover_letter_pdf(profile, materials, out_path=cover_path),
    }


if __name__ == "__main__":
    from dotenv import load_dotenv

    from agent.generator import ApplicationMaterials
    from agent.profile_loader import load_profile

    load_dotenv()
    profile = load_profile()
    job = Job(
        company="Demo Co",
        role="Frontend Developer",
        location="Remote",
        remote=True,
        skills=["React", "TypeScript"],
    )
    materials = ApplicationMaterials(
        resume_summary=(
            "Full-stack developer with hands-on React, Node.js, and MongoDB experience "
            "from 4 shipped projects. Comfortable across the MERN stack and eager to grow "
            "in a remote, collaborative team."
        ),
        cover_letter=(
            "Dear Hiring Manager,\n\nI'm writing to express interest in the Frontend "
            "Developer role at Demo Co...\n\nSincerely,\n" + profile.full_name
        ),
        email_subject=f"Application: Frontend Developer — {profile.full_name}",
        email_body="Please see attached.",
    )
    paths = build_application_pdfs(profile, job, materials)
    for name, path in paths.items():
        print(f"{name}: {path}")

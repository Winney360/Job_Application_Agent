"""End-to-end pipeline: Gmail -> jobs -> remote -> scored -> drafts -> PDFs.

Run as a script for the MVP CLI flow. Phase 3 will replace the CLI with a
Flask UI; the functions here stay reusable.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from agent.db import (
    already_processed,
    attach_materials,
    connect,
    init_db,
    mark_processed,
    upsert_scored_job,
)
from agent.extractor import EmailKind, ExtractionResult, Job, extract
from agent.generator import ApplicationMaterials, generate_materials
from agent.gmail_client import FetchedEmail, fetch_message, fetch_messages, search_messages
from agent.pdf_builder import OUTPUT_DIR, build_application_pdfs
from agent.profile_loader import Profile, load_profile
from agent.remote_filter import filter_and_dedupe
from agent.scorer import JobScore, score_jobs

DEFAULT_QUERY = "from:jobalerts-noreply@linkedin.com newer_than:30d"


@dataclass
class ScoredJob:
    job: Job
    score: JobScore
    source_email_id: str
    source_email_subject: str
    materials: ApplicationMaterials | None = None
    pdfs: dict[str, str] = field(default_factory=dict)


def _scored_to_dict(s: ScoredJob) -> dict:
    return {
        "job": s.job.model_dump(),
        "score": s.score.model_dump(),
        "source_email_id": s.source_email_id,
        "source_email_subject": s.source_email_subject,
        "materials": s.materials.model_dump() if s.materials else None,
        "pdfs": s.pdfs,
    }


def harvest_jobs(
    *,
    query: str = DEFAULT_QUERY,
    max_emails: int = 25,
    verbose: bool = False,
) -> list[tuple[FetchedEmail, ExtractionResult]]:
    """Fetch matching emails and extract jobs from each.

    Skips emails we've already classified (cache hit on the processed_emails
    table) to avoid paying Claude for the same email twice.
    """
    init_db()  # ensure processed_emails table exists
    ids = search_messages(query, max_results=max_emails)
    with connect() as conn:
        seen = already_processed(conn, ids)

    fresh_ids = [mid for mid in ids if mid not in seen]
    if verbose:
        print(f"  {len(ids)} email(s) matched, {len(seen)} already processed, "
              f"{len(fresh_ids)} new to classify")

    out: list[tuple[FetchedEmail, ExtractionResult]] = []
    for mid in fresh_ids:
        email = fetch_message(mid)
        result = extract(email)
        with connect() as conn:
            mark_processed(
                conn,
                email_id=email.id,
                kind=result.kind.value,
                sender=email.sender,
                subject=email.subject,
                job_count=len(result.jobs),
            )
        if result.kind != EmailKind.JOB_ALERT:
            continue
        out.append((email, result))
    return out


def run_pipeline(
    *,
    query: str = DEFAULT_QUERY,
    max_emails: int = 10,
    score_threshold: int = 60,
    generate_top_n: int = 5,
    output_dir: Path = OUTPUT_DIR,
    verbose: bool = True,
    persist: bool = True,
) -> list[ScoredJob]:
    profile: Profile = load_profile()
    if verbose:
        print(f"Loaded profile for {profile.full_name}")
        print(f"Searching Gmail: {query}")

    if persist:
        init_db()

    pairs = harvest_jobs(query=query, max_emails=max_emails, verbose=verbose)
    if verbose:
        print(f"Found {len(pairs)} job-alert email(s) to score")

    all_scored: list[ScoredJob] = []
    for email, result in pairs:
        remote_jobs = filter_and_dedupe(result.jobs)
        if verbose:
            print(
                f"  - {email.subject[:60]}: {len(result.jobs)} jobs -> "
                f"{len(remote_jobs)} remote+unique"
            )
        if not remote_jobs:
            continue
        scores = score_jobs(remote_jobs, profile)
        for s in scores:
            all_scored.append(
                ScoredJob(
                    job=remote_jobs[s.index],
                    score=s,
                    source_email_id=email.id,
                    source_email_subject=email.subject,
                )
            )

    # Cross-email dedupe + sort by score
    seen: set[tuple[str, str]] = set()
    deduped: list[ScoredJob] = []
    for sj in sorted(all_scored, key=lambda x: x.score.score, reverse=True):
        key = (sj.job.company.lower().strip(), sj.job.role.lower().strip())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sj)

    qualifying = [sj for sj in deduped if sj.score.score >= score_threshold]
    if verbose:
        print(
            f"\nTotal scored: {len(deduped)}  |  >= {score_threshold}: {len(qualifying)}"
        )

    # Persist all scored jobs (even below threshold) so the UI can show full picture.
    db_ids: dict[int, int] = {}  # index in deduped -> job_id
    # Map email id -> sender for fallback recipient pre-fill
    email_senders = {email.id: email.sender for email, _ in pairs}

    if persist:
        with connect() as conn:
            for i, sj in enumerate(deduped):
                db_ids[i] = upsert_scored_job(
                    conn,
                    job=sj.job.model_dump(),
                    score=sj.score.model_dump(),
                    source_email_id=sj.source_email_id,
                    source_email_subject=sj.source_email_subject,
                    source_email_sender=email_senders.get(sj.source_email_id, ""),
                )

    # Generate materials for the top N
    for i, sj in enumerate(deduped):
        if sj not in qualifying[:generate_top_n]:
            continue
        if verbose:
            print(
                f"  Drafting [{sj.score.score}] {sj.job.role} @ {sj.job.company}..."
            )
        materials = generate_materials(sj.job, profile)
        sj.materials = materials
        pdf_paths = build_application_pdfs(profile, sj.job, materials, output_dir=output_dir)
        sj.pdfs = {k: str(v) for k, v in pdf_paths.items()}

        if persist and i in db_ids:
            with connect() as conn:
                attach_materials(
                    conn,
                    db_ids[i],
                    resume_summary=materials.resume_summary,
                    cover_letter=materials.cover_letter,
                    email_subject=materials.email_subject,
                    email_body=materials.email_body,
                    resume_pdf=str(pdf_paths["resume"]),
                    cover_letter_pdf=str(pdf_paths["cover_letter"]),
                )

    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(description="Job-application pipeline")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Gmail search query")
    parser.add_argument("--max-emails", type=int, default=10)
    parser.add_argument(
        "--threshold",
        type=int,
        default=60,
        help="Minimum match score (0-100) to draft materials for",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Generate cover letter + PDFs for at most this many jobs",
    )
    parser.add_argument(
        "--save",
        type=Path,
        default=None,
        help="Optional path to dump the scored-job report as JSON",
    )
    args = parser.parse_args()

    load_dotenv()
    results = run_pipeline(
        query=args.query,
        max_emails=args.max_emails,
        score_threshold=args.threshold,
        generate_top_n=args.top_n,
    )

    print("\n=== TOP MATCHES ===")
    for sj in results[:10]:
        flag = "DRAFT" if sj.pdfs else "     "
        print(f"  [{sj.score.score:>3}] {flag} {sj.job.role} @ {sj.job.company}")
        print(f"          {sj.score.reason}")
        if sj.pdfs:
            for kind, path in sj.pdfs.items():
                print(f"          {kind}: {path}")

    if args.save:
        args.save.write_text(
            json.dumps([_scored_to_dict(s) for s in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nSaved report to {args.save}")


if __name__ == "__main__":
    main()

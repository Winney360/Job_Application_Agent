"""SQLite storage for scored jobs + draft materials + application status.

One row per (company, role, job_url). Re-running the pipeline upserts: new
jobs are inserted, jobs we've already seen are updated with the latest score
unless their status has been changed by the user (approved / rejected / sent).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "jobs.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Dedupe key: lowercased company+role+url
    fingerprint TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER NOT NULL DEFAULT 1,
    skills_json TEXT NOT NULL DEFAULT '[]',
    experience TEXT NOT NULL DEFAULT '',
    job_url TEXT NOT NULL DEFAULT '',
    posted TEXT NOT NULL DEFAULT '',

    -- Scoring
    score INTEGER NOT NULL DEFAULT 0,
    matched_skills_json TEXT NOT NULL DEFAULT '[]',
    missing_skills_json TEXT NOT NULL DEFAULT '[]',
    score_reason TEXT NOT NULL DEFAULT '',

    -- Generated materials (nullable until drafted)
    resume_summary TEXT,
    cover_letter TEXT,
    email_subject TEXT,
    email_body TEXT,
    resume_pdf TEXT,
    cover_letter_pdf TEXT,

    -- Source email
    source_email_id TEXT NOT NULL DEFAULT '',
    source_email_subject TEXT NOT NULL DEFAULT '',
    source_email_sender TEXT NOT NULL DEFAULT '',

    -- Recipient suggestion: extracted "apply to ..." email from the listing body.
    -- Pre-fills the Send form so the user doesn't retype it.
    recipient_email TEXT NOT NULL DEFAULT '',

    -- Status: pending | drafted | approved | sent | rejected
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    sent_to TEXT,
    sent_message_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(company: str, role: str, job_url: str) -> str:
    return "|".join([company.strip().lower(), role.strip().lower(), job_url.strip()])


@contextmanager
def connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Idempotent ALTER TABLE migrations for databases created before columns existed.
_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN source_email_sender TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN recipient_email TEXT NOT NULL DEFAULT ''",
]


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


@dataclass
class JobRow:
    id: int
    fingerprint: str
    company: str
    role: str
    location: str
    remote: bool
    skills: list[str]
    experience: str
    job_url: str
    posted: str
    score: int
    matched_skills: list[str]
    missing_skills: list[str]
    score_reason: str
    resume_summary: str | None
    cover_letter: str | None
    email_subject: str | None
    email_body: str | None
    resume_pdf: str | None
    cover_letter_pdf: str | None
    source_email_id: str
    source_email_subject: str
    source_email_sender: str
    recipient_email: str
    status: str
    sent_at: str | None
    sent_to: str | None
    sent_message_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "JobRow":
        return cls(
            id=row["id"],
            fingerprint=row["fingerprint"],
            company=row["company"],
            role=row["role"],
            location=row["location"],
            remote=bool(row["remote"]),
            skills=json.loads(row["skills_json"]),
            experience=row["experience"],
            job_url=row["job_url"],
            posted=row["posted"],
            score=row["score"],
            matched_skills=json.loads(row["matched_skills_json"]),
            missing_skills=json.loads(row["missing_skills_json"]),
            score_reason=row["score_reason"],
            resume_summary=row["resume_summary"],
            cover_letter=row["cover_letter"],
            email_subject=row["email_subject"],
            email_body=row["email_body"],
            resume_pdf=row["resume_pdf"],
            cover_letter_pdf=row["cover_letter_pdf"],
            source_email_id=row["source_email_id"],
            source_email_subject=row["source_email_subject"],
            source_email_sender=row["source_email_sender"] or "",
            recipient_email=row["recipient_email"] or "",
            status=row["status"],
            sent_at=row["sent_at"],
            sent_to=row["sent_to"],
            sent_message_id=row["sent_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def upsert_scored_job(
    conn: sqlite3.Connection,
    *,
    job: dict,
    score: dict,
    source_email_id: str,
    source_email_subject: str,
    source_email_sender: str = "",
) -> int:
    """Insert or update a scored job. Preserves user-set status (approved/sent/rejected).
    Returns the job id.
    """
    fp = _fingerprint(job["company"], job["role"], job.get("job_url", ""))
    now = _now()
    existing = conn.execute(
        "SELECT id, status FROM jobs WHERE fingerprint = ?", (fp,)
    ).fetchone()
    payload = {
        "fingerprint": fp,
        "company": job["company"],
        "role": job["role"],
        "location": job.get("location", ""),
        "remote": int(bool(job.get("remote", True))),
        "skills_json": json.dumps(job.get("skills", [])),
        "experience": job.get("experience", ""),
        "job_url": job.get("job_url", ""),
        "posted": job.get("posted", ""),
        "score": score["score"],
        "matched_skills_json": json.dumps(score.get("matched_skills", [])),
        "missing_skills_json": json.dumps(score.get("missing_skills", [])),
        "score_reason": score.get("reason", ""),
        "source_email_id": source_email_id,
        "source_email_subject": source_email_subject,
        "source_email_sender": source_email_sender,
        "recipient_email": job.get("apply_email", ""),
        "updated_at": now,
    }
    if existing is None:
        payload["created_at"] = now
        payload["status"] = "pending"
        cols = ", ".join(payload.keys())
        placeholders = ", ".join(["?"] * len(payload))
        cur = conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", tuple(payload.values())
        )
        return cur.lastrowid  # type: ignore[return-value]
    # Update — preserve approved/sent/rejected statuses
    set_clause = ", ".join(f"{k} = ?" for k in payload)
    conn.execute(
        f"UPDATE jobs SET {set_clause} WHERE id = ?",
        (*payload.values(), existing["id"]),
    )
    return existing["id"]


def attach_materials(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    resume_summary: str,
    cover_letter: str,
    email_subject: str,
    email_body: str,
    resume_pdf: str,
    cover_letter_pdf: str,
) -> None:
    conn.execute(
        """
        UPDATE jobs SET
            resume_summary = ?, cover_letter = ?, email_subject = ?, email_body = ?,
            resume_pdf = ?, cover_letter_pdf = ?,
            status = CASE WHEN status = 'pending' THEN 'drafted' ELSE status END,
            updated_at = ?
        WHERE id = ?
        """,
        (
            resume_summary,
            cover_letter,
            email_subject,
            email_body,
            resume_pdf,
            cover_letter_pdf,
            _now(),
            job_id,
        ),
    )


def set_status(conn: sqlite3.Connection, job_id: int, status: str) -> None:
    if status not in {"pending", "drafted", "approved", "sent", "rejected"}:
        raise ValueError(f"Invalid status: {status}")
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), job_id),
    )


def mark_sent(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    sent_to: str,
    sent_message_id: str,
) -> None:
    conn.execute(
        """
        UPDATE jobs SET status = 'sent', sent_at = ?, sent_to = ?,
                        sent_message_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (_now(), sent_to, sent_message_id, _now(), job_id),
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> JobRow | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return JobRow.from_row(row) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    min_score: int = 0,
    limit: int = 100,
) -> list[JobRow]:
    query = "SELECT * FROM jobs WHERE score >= ?"
    params: list[Any] = [min_score]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY score DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [JobRow.from_row(r) for r in rows]


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}

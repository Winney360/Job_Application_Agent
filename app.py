"""Flask review UI for the job application agent.

Routes:
  GET  /                   list all scored jobs (filterable by status / min score)
  GET  /jobs/<id>          review one job: drafts, scores, PDFs, send form
  POST /jobs/<id>/refresh  regenerate cover letter + PDFs for one job
  POST /jobs/<id>/approve  flip status to approved (no email yet)
  POST /jobs/<id>/reject   flip status to rejected
  POST /jobs/<id>/send     send the email + PDFs via Gmail; mark sent
  POST /fetch              run the pipeline against the inbox (refresh DB)
  GET  /pdf/<id>/<kind>    download a generated PDF (kind = resume | cover_letter)
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from agent.db import (
    attach_materials,
    connect,
    get_job,
    init_db,
    list_jobs,
    mark_sent,
    set_status,
    status_counts,
)
from agent.extractor import Job
from agent.gmail_client import send_email
from agent.generator import generate_materials
from agent.pdf_builder import build_application_pdfs
from agent.pipeline import run_pipeline
from agent.profile_loader import load_profile

load_dotenv()

ROOT = Path(__file__).resolve().parent

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals.update(
    USER_FULL_NAME=os.environ.get("USER_FULL_NAME", ""),
    USER_EMAIL=os.environ.get("USER_EMAIL", ""),
)


@app.before_request
def _ensure_db() -> None:
    init_db()


@app.template_filter("score_color")
def score_color(score: int) -> str:
    if score >= 75:
        return "score-green"
    if score >= 55:
        return "score-amber"
    if score >= 35:
        return "score-orange"
    return "score-red"


@app.route("/")
def index() -> str:
    status = request.args.get("status") or None
    min_score = int(request.args.get("min_score", 0))
    with connect() as conn:
        jobs = list_jobs(conn, status=status, min_score=min_score, limit=200)
        counts = status_counts(conn)
    return render_template(
        "index.html",
        jobs=jobs,
        counts=counts,
        active_status=status,
        active_min_score=min_score,
    )


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int) -> str:
    with connect() as conn:
        job = get_job(conn, job_id)
    if job is None:
        abort(404)
    return render_template("job.html", job=job)


@app.post("/jobs/<int:job_id>/approve")
def approve(job_id: int) -> "Response":
    with connect() as conn:
        set_status(conn, job_id, "approved")
    flash("Marked as approved. Click 'Send' on the job page when you're ready.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/reject")
def reject(job_id: int) -> "Response":
    with connect() as conn:
        set_status(conn, job_id, "rejected")
    flash("Job rejected.", "ok")
    return redirect(url_for("index"))


@app.post("/jobs/<int:job_id>/refresh")
def refresh_drafts(job_id: int) -> "Response":
    profile = load_profile()
    with connect() as conn:
        row = get_job(conn, job_id)
    if row is None:
        abort(404)
    job = Job(
        company=row.company,
        role=row.role,
        location=row.location,
        remote=row.remote,
        skills=row.skills,
        experience=row.experience,
        job_url=row.job_url,
    )
    materials = generate_materials(job, profile)
    pdfs = build_application_pdfs(profile, job, materials)
    with connect() as conn:
        attach_materials(
            conn,
            job_id,
            resume_summary=materials.resume_summary,
            cover_letter=materials.cover_letter,
            email_subject=materials.email_subject,
            email_body=materials.email_body,
            resume_pdf=str(pdfs["resume"]),
            cover_letter_pdf=str(pdfs["cover_letter"]),
        )
    flash("Regenerated cover letter, email and PDFs.", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<int:job_id>/send")
def send(job_id: int) -> "Response":
    to_address = (request.form.get("to") or "").strip()
    if not to_address:
        flash("Recipient address is required.", "err")
        return redirect(url_for("job_detail", job_id=job_id))

    with connect() as conn:
        job = get_job(conn, job_id)
    if job is None:
        abort(404)
    if job.status not in {"approved", "drafted"}:
        flash(f"Job status is {job.status!r}; only approved/drafted jobs can be sent.", "err")
        return redirect(url_for("job_detail", job_id=job_id))
    if not (job.email_subject and job.email_body and job.resume_pdf and job.cover_letter_pdf):
        flash("Missing draft materials. Click 'Refresh drafts' first.", "err")
        return redirect(url_for("job_detail", job_id=job_id))

    attachments = [Path(job.resume_pdf), Path(job.cover_letter_pdf)]
    message_id = send_email(
        to=to_address,
        subject=job.email_subject,
        body=job.email_body,
        attachments=attachments,
    )
    with connect() as conn:
        mark_sent(conn, job_id, sent_to=to_address, sent_message_id=message_id)
    flash(f"Sent to {to_address} (message id {message_id}).", "ok")
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/fetch")
def fetch() -> "Response":
    query = request.form.get("query") or None
    max_emails = int(request.form.get("max_emails") or 5)
    threshold = int(request.form.get("threshold") or 50)
    top_n = int(request.form.get("top_n") or 3)
    kwargs: dict = {"max_emails": max_emails, "score_threshold": threshold, "generate_top_n": top_n}
    if query:
        kwargs["query"] = query
    results = run_pipeline(verbose=True, **kwargs)
    flash(f"Fetched {len(results)} scored job(s).", "ok")
    return redirect(url_for("index"))


@app.route("/pdf/<int:job_id>/<kind>")
def download_pdf(job_id: int, kind: str):
    if kind not in {"resume", "cover_letter"}:
        abort(404)
    with connect() as conn:
        job = get_job(conn, job_id)
    if job is None:
        abort(404)
    path_str = job.resume_pdf if kind == "resume" else job.cover_letter_pdf
    if not path_str:
        abort(404)
    path = Path(path_str)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=False)


if __name__ == "__main__":
    app.run(debug=True, port=5000)

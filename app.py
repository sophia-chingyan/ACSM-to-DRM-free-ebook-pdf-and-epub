#!/usr/bin/env python3
"""Flask web interface for the unified ACSM -> DRM-free EPUB/PDF converter."""

import os
import threading
import time
import traceback
from collections import OrderedDict
from functools import wraps
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from flask import (
    Flask, jsonify, make_response, redirect, render_template,
    request, send_from_directory, session, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from converter import (
    SUPPORTED_EXTENSIONS, TOTAL_STEPS, STEP_LABELS,
    convert_pipeline, extract_cover,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())
# Trust Zeabur's reverse proxy so generated redirect URIs use the right
# scheme/host (works alongside APP_BASE_URL below).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ─── Config ──────────────────────────────────────────────────────────────────
#
# Set these in Zeabur:
#   GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  — Google Cloud Console credentials
#   ALLOWED_EMAIL                            — the single email allowed to log in
#   SECRET_KEY                               — fixed random string for sessions
#   APP_BASE_URL (optional)                  — e.g. https://your-app.zeabur.app
#
# Google OAuth authorised redirect URI:
#   https://<your-domain>/auth/google/callback
#
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_EMAIL = os.environ.get("ALLOWED_EMAIL", "")

SCRIPT_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = SCRIPT_DIR / "uploads"   # ephemeral
OUTPUT_DIR = SCRIPT_DIR / "output"    # persistent volume
COVER_DIR = SCRIPT_DIR / "covers"     # persistent volume

for _d in (UPLOAD_DIR, OUTPUT_DIR, COVER_DIR):
    _d.mkdir(exist_ok=True)

active_jobs = {}
_jobs_lock = threading.Lock()

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# ─── Auth ────────────────────────────────────────────────────────────────────


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login")
def login():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return render_template(
            "login.html",
            error=("Google OAuth is not configured. Set GOOGLE_CLIENT_ID, "
                   "GOOGLE_CLIENT_SECRET, and ALLOWED_EMAIL in Zeabur."),
        )
    return render_template("login.html", error=None)


@app.route("/login/google")
def login_google():
    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if base:
        redirect_uri = f"{base}/auth/google/callback"
    else:
        redirect_uri = url_for("auth_callback", _external=True, _scheme="https")
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception as exc:
        return render_template("login.html", error=f"OAuth error: {exc}")

    user_info = token.get("userinfo") or {}
    email = user_info.get("email", "").lower().strip()
    allowed = ALLOWED_EMAIL.lower().strip()

    if not allowed:
        return render_template(
            "login.html",
            error="ALLOWED_EMAIL is not set. Add it to your Zeabur environment.",
        )
    if not email:
        return render_template("login.html", error="Could not read your Google email.")
    if email != allowed:
        return render_template("login.html", error=f"Access denied for {email}.")

    session["authenticated"] = True
    session["user_email"] = email
    session["user_name"] = user_info.get("name", email)
    session["user_picture"] = user_info.get("picture", "")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Library helpers ─────────────────────────────────────────────────────────


def get_books():
    """Group output files by stem, attach a cover, and count files."""
    if not OUTPUT_DIR.exists():
        return [], 0
    books = OrderedDict()
    total_files = 0
    entries = sorted(
        (f for f in OUTPUT_DIR.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for f in entries:
        stem = f.stem
        if not stem:
            continue
        if stem not in books:
            books[stem] = {"stem": stem, "files": [], "cover": None}
        size_mb = f.stat().st_size / (1024 * 1024)
        books[stem]["files"].append({
            "name": f.name,
            "size": f"{size_mb:.1f} MB",
            "ext": f.suffix[1:].upper(),
        })
        total_files += 1
        if not books[stem]["cover"]:
            cover = extract_cover(f, COVER_DIR)
            if cover:
                books[stem]["cover"] = cover
    return list(books.values()), total_files


def _prune_old_jobs():
    cutoff = time.time() - 7200  # 2 hours
    with _jobs_lock:
        stale = [
            jid for jid, job in active_jobs.items()
            if job["status"] in ("done", "error") and job["start_time"] < cutoff
        ]
        for jid in stale:
            del active_jobs[jid]


def _user_ctx():
    return {
        "user_name": session.get("user_name", ""),
        "user_picture": session.get("user_picture", ""),
    }


# ─── Conversion worker ───────────────────────────────────────────────────────


def run_conversion_job(job_id, acsm_path):
    with _jobs_lock:
        job = active_jobs[job_id]
    print(f"[JOB] {job_id} started: {acsm_path}", flush=True)
    try:
        for step, message, warning in convert_pipeline(
            str(acsm_path), str(OUTPUT_DIR), str(COVER_DIR)
        ):
            if step == "done":
                job["steps"].append({"step": "done", "message": message, "warning": False})
                job["status"] = "done"
                job["done_message"] = message
            else:
                job["steps"].append(
                    {"step": int(step), "message": message, "warning": bool(warning)}
                )
                nxt = int(step) + 1
                if nxt <= TOTAL_STEPS:
                    job["current_step"] = nxt
                    job["current_label"] = STEP_LABELS.get(nxt, "")
        # Successful conversion: the single-use .acsm is no longer needed.
        try:
            Path(acsm_path).unlink(missing_ok=True)
        except OSError:
            pass
    except RuntimeError as exc:
        print(f"[JOB] {job_id} error: {exc}", flush=True)
        job["status"] = "error"
        job["error"] = str(exc)
    except Exception as exc:
        print(f"[JOB] {job_id} unexpected: {exc}\n{traceback.format_exc()}", flush=True)
        job["status"] = "error"
        job["error"] = f"Unexpected error: {exc}"


# ─── Routes ──────────────────────────────────────────────────────────────────


@app.route("/")
@login_required
def index():
    books, _ = get_books()
    resp = make_response(render_template("index.html", books=books, **_user_ctx()))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/library")
@login_required
def library():
    books, total_files = get_books()
    resp = make_response(render_template(
        "library.html", books=books, total_files=total_files, **_user_ctx()
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    if not file.filename.lower().endswith(".acsm"):
        return jsonify({"error": "Only .acsm files are accepted"}), 400
    filename = Path(file.filename).name
    file.save(UPLOAD_DIR / filename)
    return jsonify({"filename": filename})


@app.route("/start-convert/<filename>", methods=["POST"])
@login_required
def start_convert(filename):
    _prune_old_jobs()
    filename = Path(filename).name
    acsm_path = UPLOAD_DIR / filename
    if not acsm_path.exists():
        return jsonify({"error": "File not found"}), 404

    job_id = f"{filename}_{int(time.time())}"
    with _jobs_lock:
        active_jobs[job_id] = {
            "filename": filename,
            "status": "running",
            "steps": [],
            "current_step": 1,
            "current_label": STEP_LABELS[1],
            "error": None,
            "done_message": None,
            "start_time": time.time(),
        }

    threading.Thread(
        target=run_conversion_job, args=(job_id, acsm_path), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/job-status/<job_id>")
@login_required
def job_status(job_id):
    with _jobs_lock:
        job = active_jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        snapshot = {
            "status": job["status"],
            "steps": list(job["steps"]),
            "current_step": job["current_step"],
            "current_label": job["current_label"],
            "error": job["error"],
            "done_message": job["done_message"],
            "elapsed": round(time.time() - job["start_time"]),
            "total_steps": TOTAL_STEPS,
        }
    return jsonify(snapshot)


@app.route("/download/<filename>")
@login_required
def download(filename):
    filename = Path(filename).name
    if not (OUTPUT_DIR / filename).exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/delete/<stem>", methods=["POST"])
@login_required
def delete_book(stem):
    """Delete every output file for a stem, plus its cover and any leftover upload."""
    stem = Path(stem).stem
    if not stem:
        return jsonify({"error": "Invalid stem"}), 400
    deleted = []
    for f in list(OUTPUT_DIR.iterdir()):
        if f.stem == stem and f.suffix.lower() in SUPPORTED_EXTENSIONS:
            try:
                f.unlink()
                deleted.append(f.name)
            except OSError:
                pass
    for d in (COVER_DIR, UPLOAD_DIR):
        for f in list(d.iterdir()):
            if f.stem == stem:
                f.unlink(missing_ok=True)
    return jsonify({"deleted": deleted})


@app.route("/cover/<filename>")
@login_required
def cover(filename):
    return send_from_directory(COVER_DIR, Path(filename).name)


@app.route("/debug-status")
@login_required
def debug_status():
    from converter import find_tool
    with _jobs_lock:
        jobs_summary = {
            jid: {
                "status": job["status"],
                "steps": len(job["steps"]),
                "current_step": job["current_step"],
                "error": job["error"],
                "elapsed": round(time.time() - job["start_time"]),
            }
            for jid, job in active_jobs.items()
        }
    return jsonify({
        "active_jobs": jobs_summary,
        "uploads": [f.name for f in UPLOAD_DIR.iterdir()],
        "outputs": [f.name for f in OUTPUT_DIR.iterdir()],
        "tools": {
            name: bool(find_tool(name))
            for name in ("acsmdownloader", "adept_activate", "adept_remove")
        },
        "logged_in_as": session.get("user_email", "unknown"),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)

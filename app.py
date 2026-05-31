"""
SMA Check Item System — Flask web app (PostgreSQL / Render edition)
Run locally: DATABASE_URL=postgresql://... python3 app.py
"""
import json
import os
import io
from datetime import datetime, date
from functools import wraps
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import (Flask, render_template, request, redirect,
                   url_for, g, send_file, jsonify, abort, session, flash)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sma-bsapic-2026-secret")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
Q_DIR = Path("questions")

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".pdf", ".doc", ".docx", ".xls", ".xlsx"}
MAX_FILE_MB = 20

LEVEL_NAMES  = {1: "Ad-hoc", 2: "Reactive", 3: "Standardized", 4: "Proactive", 5: "Excellence"}
LEVEL_COLORS = {1: "danger", 2: "warning", 3: "info", 4: "primary", 5: "success"}
METHOD_LABELS = {"interview": "Interview", "onsite": "On-site", "document": "Document"}
PILLAR_ORDER  = ["leadership", "tm_engagement", "organization", "system"]

ROLES = {"gc_assessor": "GC Assessor", "sbu_pic": "SBU PIC", "admin": "Admin"}

STATUS_LABELS = {
    "gc_draft":               ("In Progress",  "warning"),
    "gc_submitted":           ("Submitted",    "info"),
    "validation_in_progress": ("Validation",   "primary"),
    "report_shared":          ("Report Shared","success"),
    "completed":              ("Completed",    "success"),
    "in_progress":            ("In Progress",  "warning"),
}


# ─── Questions ────────────────────────────────────────────────────────────────

def load_questions(assessment_type, scope=None):
    if assessment_type == "warehouse":
        data = json.loads((Q_DIR / "warehouse.json").read_text())
        return data["pillars"]
    elif assessment_type == "retail":
        data = json.loads((Q_DIR / "retail.json").read_text())
        return data["scopes"][scope or "store"]["pillars"]
    return []


def all_questions_flat(pillars):
    return [q for p in pillars for e in p["elements"] for q in e["questions"]]


# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur  = conn.cursor()

    for sql in [
        """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'gc_assessor',
            gc_id INTEGER,
            created_at TEXT DEFAULT (NOW()::TEXT)
        )""",
        """CREATE TABLE IF NOT EXISTS gcs (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'warehouse',
            scope TEXT,
            created_at TEXT DEFAULT (NOW()::TEXT)
        )""",
        """CREATE TABLE IF NOT EXISTS assessments (
            id SERIAL PRIMARY KEY,
            site_name TEXT NOT NULL,
            type TEXT NOT NULL,
            scope TEXT,
            gc_id INTEGER,
            kind TEXT DEFAULT 'validation',
            linked_to INTEGER,
            assessor_a TEXT,
            assessor_b TEXT,
            assessment_date TEXT,
            status TEXT DEFAULT 'gc_draft',
            created_by INTEGER,
            submitted_at TEXT,
            shared_at TEXT,
            created_at TEXT DEFAULT (NOW()::TEXT)
        )""",
        """CREATE TABLE IF NOT EXISTS responses (
            id SERIAL PRIMARY KEY,
            assessment_id INTEGER NOT NULL REFERENCES assessments(id),
            question_id TEXT NOT NULL,
            answer TEXT,
            comment TEXT,
            updated_at TEXT DEFAULT (NOW()::TEXT),
            UNIQUE(assessment_id, question_id)
        )""",
        """CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            assessment_id INTEGER NOT NULL REFERENCES assessments(id),
            question_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            mime_type TEXT,
            file_data BYTEA,
            uploaded_at TEXT DEFAULT (NOW()::TEXT)
        )""",
        """CREATE TABLE IF NOT EXISTS findings (
            id SERIAL PRIMARY KEY,
            assessment_id INTEGER NOT NULL REFERENCES assessments(id),
            question_id TEXT NOT NULL,
            pillar_id TEXT,
            element_id TEXT,
            action_plan TEXT,
            responsible TEXT,
            due_date TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT (NOW()::TEXT),
            updated_at TEXT DEFAULT (NOW()::TEXT)
        )""",
        """CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL,
            link TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (NOW()::TEXT)
        )""",
    ]:
        cur.execute(sql)

    # Create default admin if no users exist
    cur.execute("SELECT COUNT(*) as cnt FROM users")
    if cur.fetchone()["cnt"] == 0:
        cur.execute(
            "INSERT INTO users (username, full_name, password_hash, role) VALUES (%s,%s,%s,%s)",
            ("admin", "Administrator",
             generate_password_hash("admin123", method="pbkdf2:sha256"), "admin")
        )

    conn.commit()
    cur.close()
    conn.close()


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    cur = get_db().cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    return cur.fetchone()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            user = current_user()
            if not user or user["role"] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator


def notify_role(db, role, message, link=None):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE role=%s", (role,))
    for u in cur.fetchall():
        cur.execute("INSERT INTO notifications (user_id, message, link) VALUES (%s,%s,%s)",
                    (u["id"], message, link))


def notify_gc_users(db, gc_id, message, link=None):
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE gc_id=%s AND role='gc_assessor'", (gc_id,))
    for u in cur.fetchall():
        cur.execute("INSERT INTO notifications (user_id, message, link) VALUES (%s,%s,%s)",
                    (u["id"], message, link))


def unread_count(db, user_id):
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM notifications WHERE user_id=%s AND is_read=0", (user_id,))
    return cur.fetchone()["cnt"]


# ─── Score calculation ────────────────────────────────────────────────────────

def calculate_scores(pillars, responses):
    pillar_scores = {}
    for pillar in pillars:
        element_scores = {}
        for element in pillar["elements"]:
            applicable = [q for q in element["questions"]
                          if responses.get(q["id"]) not in ("na", "not_rolled_out", None, "")]
            if not applicable:
                element_scores[element["id"]] = None
                continue
            by_level = {}
            for q in applicable:
                by_level.setdefault(q["level"], []).append(responses.get(q["id"]))
            score = 5
            for lv in sorted(by_level.keys()):
                if "no" in by_level[lv]:
                    score = lv - 1
                    break
            element_scores[element["id"]] = score
        valid = [s for s in element_scores.values() if s is not None]
        pillar_scores[pillar["id"]] = {
            "score":    min(valid) if valid else None,
            "elements": element_scores,
        }

    def avg(*vals):
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 2) if v else None

    ld   = pillar_scores.get("leadership",    {}).get("score")
    tm   = pillar_scores.get("tm_engagement", {}).get("score")
    org  = pillar_scores.get("organization",  {}).get("score")
    sys_ = pillar_scores.get("system",        {}).get("score")
    sa      = avg(ld, tm)
    si      = avg(org, sys_)
    overall = avg(sa, si)

    return {
        "overall":               overall,
        "safety_awareness":      sa,
        "system_implementation": si,
        "pillars":               pillar_scores,
        "level_name":            LEVEL_NAMES.get(round(overall) if overall else 0, "—"),
    }


def get_responses_dict(db, assessment_id):
    cur = db.cursor()
    cur.execute("SELECT question_id, answer, comment FROM responses WHERE assessment_id=%s", (assessment_id,))
    return {r["question_id"]: {"answer": r["answer"], "comment": r["comment"] or ""} for r in cur.fetchall()}


def answers_only(resp_dict):
    return {qid: v["answer"] for qid, v in resp_dict.items()}


def can_access_assessment(user, assessment):
    if user["role"] in ("admin", "sbu_pic"):
        return True
    if user["role"] == "gc_assessor":
        return assessment["gc_id"] == user["gc_id"]
    return False


# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        db  = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["role"]    = user["role"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    db   = get_db()
    user = current_user()
    cur  = db.cursor()

    if user["role"] == "gc_assessor":
        cur.execute("SELECT * FROM assessments WHERE gc_id=%s ORDER BY created_at DESC", (user["gc_id"],))
    else:
        cur.execute("SELECT * FROM assessments ORDER BY created_at DESC")
    assessments_raw = cur.fetchall()

    enriched = []
    for a in assessments_raw:
        pillars  = load_questions(a["type"], a["scope"])
        resp     = get_responses_dict(db, a["id"])
        ans      = answers_only(resp)
        all_qs   = all_questions_flat(pillars)
        answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None, ""))
        scores   = calculate_scores(pillars, ans) if ans else None
        gc = None
        if a["gc_id"]:
            cur.execute("SELECT * FROM gcs WHERE id=%s", (a["gc_id"],))
            gc_row = cur.fetchone()
            gc = dict(gc_row) if gc_row else None
        enriched.append({"a": dict(a), "scores": scores,
                         "total_q": len(all_qs), "answered_q": answered, "gc": gc})

    unread = unread_count(db, user["id"])
    return render_template("dashboard.html", assessments=enriched,
                           level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS,
                           user=dict(user), unread=unread, status_labels=STATUS_LABELS)


# ─── New assessment ────────────────────────────────────────────────────────────

@app.route("/new", methods=["GET", "POST"])
@login_required
def new_assessment():
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM gcs ORDER BY name")
    gcs = cur.fetchall()

    if request.method == "POST":
        site_name       = request.form["site_name"].strip()
        atype           = request.form["type"]
        scope           = request.form.get("scope", "store")
        assessor_a      = request.form.get("assessor_a", "").strip()
        assessor_b      = request.form.get("assessor_b", "").strip()
        assessment_date = request.form.get("assessment_date") or date.today().isoformat()
        kind            = request.form.get("kind", "self")
        linked_to       = request.form.get("linked_to") or None

        if user["role"] == "gc_assessor":
            gc_id  = user["gc_id"]
            kind   = "self"
            status = "gc_draft"
        else:
            gc_id  = request.form.get("gc_id") or None
            if gc_id:
                gc_id = int(gc_id)
            status = "validation_in_progress" if kind == "validation" else "gc_draft"

        cur.execute(
            """INSERT INTO assessments
               (site_name, type, scope, gc_id, kind, linked_to, assessor_a, assessor_b,
                assessment_date, status, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (site_name, atype, scope, gc_id, kind, linked_to,
             assessor_a, assessor_b, assessment_date, status, user["id"])
        )
        new_id = cur.fetchone()["id"]
        db.commit()
        return redirect(url_for("assess", assessment_id=new_id))

    prefill_gc    = None
    submitted_self = []
    if user["role"] == "gc_assessor" and user["gc_id"]:
        cur.execute("SELECT * FROM gcs WHERE id=%s", (user["gc_id"],))
        prefill_gc = cur.fetchone()
    if user["role"] in ("sbu_pic", "admin"):
        cur.execute(
            "SELECT a.*, g.name as gc_name FROM assessments a LEFT JOIN gcs g ON a.gc_id=g.id "
            "WHERE a.kind='self' AND a.status='gc_submitted' ORDER BY a.created_at DESC"
        )
        submitted_self = cur.fetchall()

    unread = unread_count(db, user["id"])
    return render_template("new_assessment.html", today=date.today().isoformat(),
                           gcs=gcs, user=dict(user),
                           prefill_gc=dict(prefill_gc) if prefill_gc else None,
                           submitted_self=submitted_self, unread=unread)


# ─── Assess ────────────────────────────────────────────────────────────────────

@app.route("/assess/<int:assessment_id>")
@login_required
def assess(assessment_id):
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment or not can_access_assessment(user, assessment):
        return redirect(url_for("dashboard"))

    read_only = False
    if user["role"] == "gc_assessor" and assessment["status"] != "gc_draft":
        read_only = True
    if user["role"] == "sbu_pic" and assessment["kind"] == "self" and assessment["status"] != "gc_draft":
        read_only = True

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    ans     = answers_only(resp)
    all_qs  = all_questions_flat(pillars)

    all_levels      = sorted({q["level"] for q in all_qs})
    all_methods     = sorted({m for q in all_qs for m in q["audit_methods"]})
    all_answered_by = sorted({q["answered_by"] for q in all_qs if q["answered_by"]})
    all_standards   = sorted({q["standard"] for q in all_qs if q["standard"]})
    all_elements    = [(p["id"], e["id"], e["name"]) for p in pillars for e in p["elements"]]

    cur.execute(
        "SELECT question_id, id, original_name, mime_type FROM attachments WHERE assessment_id=%s",
        (assessment_id,)
    )
    attachments = {}
    for r in cur.fetchall():
        attachments.setdefault(r["question_id"], []).append(dict(r))

    scores = calculate_scores(pillars, ans)
    unread = unread_count(db, user["id"])

    self_resp = {}; self_resp_full = {}; self_assessment = None; self_scores = None
    if assessment["kind"] == "validation" and assessment["linked_to"]:
        cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment["linked_to"],))
        self_assessment = cur.fetchone()
        self_resp_full  = get_responses_dict(db, assessment["linked_to"])
        self_resp       = answers_only(self_resp_full)
        self_scores     = calculate_scores(pillars, self_resp)

    return render_template(
        "assess.html",
        assessment=dict(assessment), pillars=pillars, resp=resp, scores=scores,
        level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS, method_labels=METHOD_LABELS,
        all_levels=all_levels, all_methods=all_methods, all_answered_by=all_answered_by,
        all_standards=all_standards, all_elements=all_elements, attachments=attachments,
        total_q=len(all_qs),
        answered_q=sum(1 for q in all_qs if ans.get(q["id"]) not in (None, "")),
        user=dict(user), read_only=read_only, unread=unread,
        self_resp=self_resp, self_resp_full=self_resp_full,
        self_scores=self_scores,
        self_assessment=dict(self_assessment) if self_assessment else None,
    )


# ─── AJAX: auto-save answer ───────────────────────────────────────────────────

@app.route("/api/answer", methods=["POST"])
@login_required
def api_answer():
    data          = request.get_json()
    assessment_id = data.get("assessment_id")
    question_id   = data.get("question_id")
    answer        = data.get("answer", "")
    comment       = data.get("comment", "")

    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    cur.execute(
        """INSERT INTO responses (assessment_id, question_id, answer, comment, updated_at)
           VALUES (%s,%s,%s,%s,NOW()::TEXT)
           ON CONFLICT(assessment_id, question_id)
           DO UPDATE SET answer=EXCLUDED.answer, comment=EXCLUDED.comment, updated_at=NOW()::TEXT""",
        (assessment_id, question_id, answer, comment)
    )
    db.commit()

    pillars  = load_questions(assessment["type"], assessment["scope"])
    resp     = get_responses_dict(db, assessment_id)
    ans      = answers_only(resp)
    all_qs   = all_questions_flat(pillars)
    scores   = calculate_scores(pillars, ans)
    answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None, ""))

    return jsonify({"scores": scores, "answered": answered,
                    "total": len(all_qs), "level_name": scores["level_name"],
                    "level_names": LEVEL_NAMES})


# ─── AJAX: file upload ────────────────────────────────────────────────────────

@app.route("/api/upload/<int:assessment_id>/<question_id>", methods=["POST"])
@login_required
def api_upload(assessment_id, question_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM assessments WHERE id=%s", (assessment_id,))
    if not cur.fetchone():
        return jsonify({"error": "not found"}), 404

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"File type {ext} not allowed"}), 400
    content = file.read()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        return jsonify({"error": f"File too large (max {MAX_FILE_MB}MB)"}), 400

    cur.execute(
        "INSERT INTO attachments (assessment_id, question_id, original_name, mime_type, file_data) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (assessment_id, question_id, file.filename, file.content_type, psycopg2.Binary(content))
    )
    att_id = cur.fetchone()["id"]
    db.commit()
    return jsonify({"id": att_id, "original_name": file.filename,
                    "is_image": ext in {".jpg",".jpeg",".png",".gif"},
                    "url": url_for("serve_attachment", att_id=att_id)})


@app.route("/attachment/<int:att_id>")
@login_required
def serve_attachment(att_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM attachments WHERE id=%s", (att_id,))
    row = cur.fetchone()
    if not row or not row["file_data"]:
        abort(404)
    return send_file(io.BytesIO(bytes(row["file_data"])),
                     download_name=row["original_name"],
                     mimetype=row["mime_type"] or "application/octet-stream")


@app.route("/api/attachment/<int:att_id>", methods=["DELETE"])
@login_required
def delete_attachment(att_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM attachments WHERE id=%s", (att_id,))
    if not cur.fetchone():
        return jsonify({"error": "not found"}), 404
    cur.execute("DELETE FROM attachments WHERE id=%s", (att_id,))
    db.commit()
    return jsonify({"ok": True})


# ─── Workflow ─────────────────────────────────────────────────────────────────

@app.route("/api/submit/<int:assessment_id>", methods=["POST"])
@login_required
def api_submit(assessment_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    cur.execute("UPDATE assessments SET status='gc_submitted', submitted_at=NOW()::TEXT WHERE id=%s",
                (assessment_id,))
    gc_name = assessment["site_name"]
    if assessment["gc_id"]:
        cur.execute("SELECT name FROM gcs WHERE id=%s", (assessment["gc_id"],))
        gc_row = cur.fetchone()
        if gc_row:
            gc_name = gc_row["name"]
    msg  = f"Self-assessment submitted by {gc_name} — awaiting validation."
    link = url_for("result", assessment_id=assessment_id)
    notify_role(db, "sbu_pic", msg, link)
    notify_role(db, "admin",   msg, link)
    db.commit()
    return jsonify({"redirect": url_for("result", assessment_id=assessment_id)})


@app.route("/api/start-validation/<int:self_assessment_id>", methods=["POST"])
@role_required("sbu_pic", "admin")
def api_start_validation(self_assessment_id):
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (self_assessment_id,))
    sa = cur.fetchone()
    if not sa:
        return jsonify({"error": "not found"}), 404

    cur.execute(
        """INSERT INTO assessments
           (site_name, type, scope, gc_id, kind, linked_to, assessment_date, status, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (sa["site_name"], sa["type"], sa["scope"], sa["gc_id"],
         "validation", self_assessment_id, date.today().isoformat(),
         "validation_in_progress", user["id"])
    )
    new_id = cur.fetchone()["id"]
    db.commit()
    return jsonify({"redirect": url_for("assess", assessment_id=new_id)})


@app.route("/api/complete/<int:assessment_id>", methods=["POST"])
@login_required
def api_complete(assessment_id):
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    if assessment["kind"] == "self" or user["role"] == "gc_assessor":
        cur.execute("UPDATE assessments SET status='gc_draft' WHERE id=%s", (assessment_id,))
    else:
        if assessment["status"] not in ("report_shared", "completed"):
            cur.execute("UPDATE assessments SET status='validation_in_progress' WHERE id=%s", (assessment_id,))
    db.commit()
    return jsonify({"redirect": url_for("result", assessment_id=assessment_id)})


@app.route("/api/share-report/<int:assessment_id>", methods=["POST"])
@role_required("sbu_pic", "admin")
def api_share_report(assessment_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    cur.execute("UPDATE assessments SET status='report_shared', shared_at=NOW()::TEXT WHERE id=%s",
                (assessment_id,))

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    for pillar in pillars:
        for element in pillar["elements"]:
            for q in element["questions"]:
                if resp.get(q["id"], {}).get("answer") == "no":
                    cur.execute("SELECT id FROM findings WHERE assessment_id=%s AND question_id=%s",
                                (assessment_id, q["id"]))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO findings (assessment_id, question_id, pillar_id, element_id, status) "
                            "VALUES (%s,%s,%s,%s,%s)",
                            (assessment_id, q["id"], pillar["id"], element["id"], "open")
                        )

    if assessment["gc_id"]:
        cur.execute("SELECT name FROM gcs WHERE id=%s", (assessment["gc_id"],))
        gc_row  = cur.fetchone()
        gc_name = gc_row["name"] if gc_row else assessment["site_name"]
        notify_gc_users(db, assessment["gc_id"],
                        f"Validation report shared for {gc_name}. Please review findings.",
                        url_for("findings", assessment_id=assessment_id))
    db.commit()
    return jsonify({"redirect": url_for("findings", assessment_id=assessment_id)})


# ─── Scores API ──────────────────────────────────────────────────────────────

@app.route("/api/scores/<int:assessment_id>")
@login_required
def api_scores(assessment_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return jsonify({}), 404
    pillars  = load_questions(assessment["type"], assessment["scope"])
    resp     = get_responses_dict(db, assessment_id)
    ans      = answers_only(resp)
    all_qs   = all_questions_flat(pillars)
    scores   = calculate_scores(pillars, ans)
    answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None, ""))
    return jsonify({"scores": scores, "answered": answered,
                    "total": len(all_qs), "level_names": LEVEL_NAMES})


# ─── Result ───────────────────────────────────────────────────────────────────

@app.route("/result/<int:assessment_id>")
@login_required
def result(assessment_id):
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment or not can_access_assessment(user, assessment):
        return redirect(url_for("dashboard"))

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    ans     = answers_only(resp)
    scores  = calculate_scores(pillars, ans)

    cur.execute("SELECT * FROM attachments WHERE assessment_id=%s", (assessment_id,))
    attachments = {}
    for r in cur.fetchall():
        attachments.setdefault(r["question_id"], []).append(dict(r))

    self_assessment = None
    if assessment["linked_to"]:
        cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment["linked_to"],))
        self_assessment = cur.fetchone()

    gc = None
    if assessment["gc_id"]:
        cur.execute("SELECT * FROM gcs WHERE id=%s", (assessment["gc_id"],))
        gc = cur.fetchone()

    cur.execute("SELECT COUNT(*) as cnt FROM findings WHERE assessment_id=%s", (assessment_id,))
    findings_count = cur.fetchone()["cnt"]

    unread = unread_count(db, user["id"])
    return render_template(
        "result.html",
        assessment=dict(assessment), pillars=pillars, responses=ans,
        comments={qid: v["comment"] for qid, v in resp.items()},
        attachments=attachments, scores=scores,
        level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS,
        user=dict(user),
        self_assessment=dict(self_assessment) if self_assessment else None,
        gc=dict(gc) if gc else None,
        findings_count=findings_count, unread=unread, status_labels=STATUS_LABELS,
    )


# ─── Findings ─────────────────────────────────────────────────────────────────

@app.route("/findings/<int:assessment_id>")
@login_required
def findings(assessment_id):
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment or not can_access_assessment(user, assessment):
        return redirect(url_for("dashboard"))

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    ans     = answers_only(resp)
    scores  = calculate_scores(pillars, ans)

    q_lookup = {}; p_lookup = {}; e_lookup = {}
    for p in pillars:
        p_lookup[p["id"]] = p["name"]
        for e in p["elements"]:
            e_lookup[e["id"]] = e["name"]
            for q in e["questions"]:
                q_lookup[q["id"]] = q

    cur.execute("SELECT * FROM findings WHERE assessment_id=%s ORDER BY status, pillar_id, element_id",
                (assessment_id,))
    findings_rows = cur.fetchall()

    gc = None
    if assessment["gc_id"]:
        cur.execute("SELECT * FROM gcs WHERE id=%s", (assessment["gc_id"],))
        gc = cur.fetchone()

    unread = unread_count(db, user["id"])
    return render_template(
        "findings.html",
        assessment=dict(assessment), findings=[dict(f) for f in findings_rows],
        q_lookup=q_lookup, p_lookup=p_lookup, e_lookup=e_lookup,
        scores=scores, level_names=LEVEL_NAMES, user=dict(user),
        gc=dict(gc) if gc else None, unread=unread, status_labels=STATUS_LABELS,
    )


@app.route("/api/finding/<int:finding_id>", methods=["POST"])
@login_required
def api_update_finding(finding_id):
    db  = get_db()
    cur = db.cursor()
    data = request.get_json()
    cur.execute(
        "UPDATE findings SET action_plan=%s, responsible=%s, due_date=%s, "
        "status=%s, updated_at=NOW()::TEXT WHERE id=%s",
        (data.get("action_plan",""), data.get("responsible",""),
         data.get("due_date",""), data.get("status","open"), finding_id)
    )
    db.commit()
    return jsonify({"ok": True})


# ─── Notifications ────────────────────────────────────────────────────────────

@app.route("/notifications")
@login_required
def notifications():
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC", (user["id"],))
    notes = cur.fetchall()
    cur.execute("UPDATE notifications SET is_read=1 WHERE user_id=%s", (user["id"],))
    db.commit()
    return render_template("notifications.html", notifications=notes,
                           user=dict(user), unread=0)


# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route("/admin")
@role_required("admin")
def admin_panel():
    db   = get_db()
    user = current_user()
    cur  = db.cursor()
    cur.execute("SELECT u.*, g.name as gc_name FROM users u LEFT JOIN gcs g ON u.gc_id=g.id ORDER BY u.role, u.username")
    users = cur.fetchall()
    cur.execute("SELECT * FROM gcs ORDER BY name")
    gcs = cur.fetchall()
    unread = unread_count(db, user["id"])
    return render_template("admin.html", users=users, gcs=gcs,
                           user=dict(user), roles=ROLES, unread=unread)


@app.route("/admin/user/new", methods=["POST"])
@role_required("admin")
def admin_new_user():
    db  = get_db()
    cur = db.cursor()
    username  = request.form["username"].strip()
    full_name = request.form["full_name"].strip()
    password  = request.form["password"]
    role      = request.form["role"]
    gc_id     = request.form.get("gc_id") or None
    if gc_id:
        gc_id = int(gc_id)
    try:
        cur.execute(
            "INSERT INTO users (username, full_name, password_hash, role, gc_id) VALUES (%s,%s,%s,%s,%s)",
            (username, full_name, generate_password_hash(password, method="pbkdf2:sha256"), role, gc_id)
        )
        db.commit()
        flash(f"User '{username}' created.", "success")
    except psycopg2.IntegrityError:
        db.rollback()
        flash(f"Username '{username}' already exists.", "danger")
    return redirect(url_for("admin_panel"))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_user(user_id):
    db  = get_db()
    cur = db.cursor()
    if user_id == session["user_id"]:
        flash("Cannot delete yourself.", "danger")
        return redirect(url_for("admin_panel"))
    cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
    db.commit()
    flash("User deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/user/<int:user_id>/reset", methods=["POST"])
@role_required("admin")
def admin_reset_password(user_id):
    db  = get_db()
    cur = db.cursor()
    new_pw = request.form["new_password"]
    cur.execute("UPDATE users SET password_hash=%s WHERE id=%s",
                (generate_password_hash(new_pw, method="pbkdf2:sha256"), user_id))
    db.commit()
    flash("Password reset.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/gc/new", methods=["POST"])
@role_required("admin")
def admin_new_gc():
    db  = get_db()
    cur = db.cursor()
    name  = request.form["name"].strip()
    atype = request.form["type"]
    scope = request.form.get("scope") or None
    cur.execute("INSERT INTO gcs (name, type, scope) VALUES (%s,%s,%s)", (name, atype, scope))
    db.commit()
    flash(f"GC '{name}' created.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/gc/<int:gc_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_gc(gc_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM gcs WHERE id=%s", (gc_id,))
    db.commit()
    flash("GC deleted.", "success")
    return redirect(url_for("admin_panel"))


# ─── Export ───────────────────────────────────────────────────────────────────

@app.route("/export/<int:assessment_id>/pdf")
@login_required
def export_pdf(assessment_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return redirect(url_for("dashboard"))
    pillars  = load_questions(assessment["type"], assessment["scope"])
    resp     = get_responses_dict(db, assessment_id)
    ans      = answers_only(resp)
    comments = {qid: v["comment"] for qid, v in resp.items()}
    scores   = calculate_scores(pillars, ans)
    from export.pdf_report import generate_pdf
    pdf_bytes = generate_pdf(dict(assessment), pillars, ans, scores, LEVEL_NAMES, comments)
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True,
                     download_name=f"SMA_{assessment['site_name']}_{assessment['assessment_date']}.pdf")


@app.route("/export/<int:assessment_id>/excel")
@login_required
def export_excel(assessment_id):
    db  = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM assessments WHERE id=%s", (assessment_id,))
    assessment = cur.fetchone()
    if not assessment:
        return redirect(url_for("dashboard"))
    pillars  = load_questions(assessment["type"], assessment["scope"])
    resp     = get_responses_dict(db, assessment_id)
    ans      = answers_only(resp)
    comments = {qid: v["comment"] for qid, v in resp.items()}
    scores   = calculate_scores(pillars, ans)
    from export.excel_report import generate_excel
    excel_bytes = generate_excel(dict(assessment), pillars, ans, scores, LEVEL_NAMES, comments)
    return send_file(io.BytesIO(excel_bytes),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"SMA_{assessment['site_name']}_{assessment['assessment_date']}.xlsx")


@app.route("/delete/<int:assessment_id>", methods=["POST"])
@role_required("admin", "sbu_pic")
def delete_assessment(assessment_id):
    db  = get_db()
    cur = db.cursor()
    for table, col in [("findings","assessment_id"), ("attachments","assessment_id"),
                       ("responses","assessment_id"), ("assessments","id")]:
        cur.execute(f"DELETE FROM {table} WHERE {col}=%s", (assessment_id,))
    db.commit()
    return redirect(url_for("dashboard"))


@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


# Initialize DB on startup
init_db()

if __name__ == "__main__":
    print("SMA System running at http://localhost:5001")
    print("Default login: admin / admin123")
    app.run(debug=True, port=5001)

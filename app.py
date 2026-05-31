"""
SMA Check Item System — Flask web app
Run: python3 app.py  →  open http://localhost:5001
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime, date
from functools import wraps
from pathlib import Path
from flask import (Flask, render_template, request, redirect,
                   url_for, g, send_file, jsonify, abort, session, flash)
from werkzeug.security import generate_password_hash, check_password_hash
import io

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sma-bsapic-2026-secret")

# Railway: set DATA_DIR=/data on persistent volume; local: use current dir
DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DB_PATH = DATA_DIR / "sma.db"
Q_DIR = Path("questions")

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".pdf", ".doc", ".docx", ".xls", ".xlsx"}
MAX_FILE_MB = 20

LEVEL_NAMES = {1: "Ad-hoc", 2: "Reactive", 3: "Standardized", 4: "Proactive", 5: "Excellence"}
LEVEL_COLORS = {1: "danger", 2: "warning", 3: "info", 4: "primary", 5: "success"}
METHOD_LABELS = {"interview": "Interview", "onsite": "On-site", "document": "Document"}
PILLAR_ORDER = ["leadership", "tm_engagement", "organization", "system"]

ROLES = {"gc_assessor": "GC Assessor", "sbu_pic": "SBU PIC", "admin": "Admin"}

# Assessment status labels
STATUS_LABELS = {
    "gc_draft": ("In Progress", "warning"),
    "gc_submitted": ("Submitted", "info"),
    "validation_in_progress": ("Validation", "primary"),
    "report_shared": ("Report Shared", "success"),
    "completed": ("Completed", "success"),
    "in_progress": ("In Progress", "warning"),  # legacy
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
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'gc_assessor',
            gc_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS gcs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'warehouse',
            scope TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER NOT NULL REFERENCES assessments(id),
            question_id TEXT NOT NULL,
            answer TEXT,
            comment TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(assessment_id, question_id)
        );
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER NOT NULL REFERENCES assessments(id),
            question_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            mime_type TEXT,
            file_data BLOB,
            uploaded_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER NOT NULL REFERENCES assessments(id),
            question_id TEXT NOT NULL,
            pillar_id TEXT,
            element_id TEXT,
            action_plan TEXT,
            responsible TEXT,
            due_date TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            message TEXT NOT NULL,
            link TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    # Migrations for existing DB
    for col_sql in [
        "ALTER TABLE responses ADD COLUMN comment TEXT",
        "ALTER TABLE responses ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))",
        "ALTER TABLE assessments ADD COLUMN gc_id INTEGER",
        "ALTER TABLE assessments ADD COLUMN kind TEXT DEFAULT 'validation'",
        "ALTER TABLE assessments ADD COLUMN linked_to INTEGER",
        "ALTER TABLE assessments ADD COLUMN created_by INTEGER",
        "ALTER TABLE assessments ADD COLUMN submitted_at TEXT",
        "ALTER TABLE assessments ADD COLUMN shared_at TEXT",
        "ALTER TABLE attachments ADD COLUMN file_data BLOB",
    ]:
        try:
            db.execute(col_sql)
            db.commit()
        except Exception:
            pass

    # Create default admin if no users
    cur = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if cur == 0:
        db.execute(
            "INSERT INTO users (username, full_name, password_hash, role) VALUES (?,?,?,?)",
            ("admin", "Administrator", generate_password_hash("admin123", method="pbkdf2:sha256"), "admin")
        )
        db.commit()
    db.commit()
    db.close()


# ─── Auth helpers ─────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


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
    """Send notification to all users with a given role."""
    users = db.execute("SELECT id FROM users WHERE role=?", (role,)).fetchall()
    for u in users:
        db.execute(
            "INSERT INTO notifications (user_id, message, link) VALUES (?,?,?)",
            (u["id"], message, link)
        )


def notify_gc_users(db, gc_id, message, link=None):
    """Send notification to all gc_assessor users of a GC."""
    users = db.execute("SELECT id FROM users WHERE gc_id=? AND role='gc_assessor'", (gc_id,)).fetchall()
    for u in users:
        db.execute(
            "INSERT INTO notifications (user_id, message, link) VALUES (?,?,?)",
            (u["id"], message, link)
        )


def unread_count(db, user_id):
    return db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0", (user_id,)
    ).fetchone()[0]


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
            "score": min(valid) if valid else None,
            "elements": element_scores,
        }

    def avg(*vals):
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 2) if v else None

    ld = pillar_scores.get("leadership", {}).get("score")
    tm = pillar_scores.get("tm_engagement", {}).get("score")
    org = pillar_scores.get("organization", {}).get("score")
    sys_ = pillar_scores.get("system", {}).get("score")
    sa = avg(ld, tm)
    si = avg(org, sys_)
    overall = avg(sa, si)

    return {
        "overall": overall,
        "safety_awareness": sa,
        "system_implementation": si,
        "pillars": pillar_scores,
        "level_name": LEVEL_NAMES.get(round(overall) if overall else 0, "—"),
    }


def get_responses_dict(db, assessment_id):
    rows = db.execute(
        "SELECT question_id, answer, comment FROM responses WHERE assessment_id=?",
        (assessment_id,)
    ).fetchall()
    return {r["question_id"]: {"answer": r["answer"], "comment": r["comment"] or ""} for r in rows}


def answers_only(resp_dict):
    return {qid: v["answer"] for qid, v in resp_dict.items()}


def can_access_assessment(user, assessment):
    """Check if user can view/edit an assessment."""
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
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
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
    db = get_db()
    user = current_user()

    if user["role"] == "gc_assessor":
        assessments_raw = db.execute(
            "SELECT * FROM assessments WHERE gc_id=? ORDER BY created_at DESC", (user["gc_id"],)
        ).fetchall()
    else:
        assessments_raw = db.execute(
            "SELECT * FROM assessments ORDER BY created_at DESC"
        ).fetchall()

    enriched = []
    for a in assessments_raw:
        pillars = load_questions(a["type"], a["scope"])
        resp = get_responses_dict(db, a["id"])
        ans = answers_only(resp)
        all_qs = all_questions_flat(pillars)
        answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None, ""))
        scores = calculate_scores(pillars, ans) if ans else None
        gc = db.execute("SELECT * FROM gcs WHERE id=?", (a["gc_id"],)).fetchone() if a["gc_id"] else None
        enriched.append({
            "a": dict(a),
            "scores": scores,
            "total_q": len(all_qs),
            "answered_q": answered,
            "gc": dict(gc) if gc else None,
        })

    unread = unread_count(db, user["id"])
    return render_template("dashboard.html", assessments=enriched,
                           level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS,
                           user=dict(user), unread=unread,
                           status_labels=STATUS_LABELS)


# ─── New assessment ────────────────────────────────────────────────────────────

@app.route("/new", methods=["GET", "POST"])
@login_required
def new_assessment():
    db = get_db()
    user = current_user()
    gcs = db.execute("SELECT * FROM gcs ORDER BY name").fetchall()

    if request.method == "POST":
        site_name = request.form["site_name"].strip()
        atype = request.form["type"]
        scope = request.form.get("scope", "store")
        assessor_a = request.form.get("assessor_a", "").strip()
        assessor_b = request.form.get("assessor_b", "").strip()
        assessment_date = request.form.get("assessment_date") or date.today().isoformat()
        kind = request.form.get("kind", "self")
        linked_to = request.form.get("linked_to") or None

        # GC ID
        if user["role"] == "gc_assessor":
            gc_id = user["gc_id"]
            kind = "self"
            status = "gc_draft"
        else:
            gc_id = request.form.get("gc_id") or None
            if gc_id:
                gc_id = int(gc_id)
            status = "validation_in_progress" if kind == "validation" else "gc_draft"

        cur = db.execute(
            """INSERT INTO assessments
               (site_name, type, scope, gc_id, kind, linked_to, assessor_a, assessor_b,
                assessment_date, status, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (site_name, atype, scope, gc_id, kind, linked_to,
             assessor_a, assessor_b, assessment_date, status, user["id"])
        )
        db.commit()
        return redirect(url_for("assess", assessment_id=cur.lastrowid))

    # Pre-fill from GC if gc_assessor
    prefill_gc = None
    if user["role"] == "gc_assessor" and user["gc_id"]:
        prefill_gc = db.execute("SELECT * FROM gcs WHERE id=?", (user["gc_id"],)).fetchone()

    # For SBU starting validation: pass submitted self-assessments
    submitted_self = []
    if user["role"] in ("sbu_pic", "admin"):
        submitted_self = db.execute(
            "SELECT a.*, g.name as gc_name FROM assessments a LEFT JOIN gcs g ON a.gc_id=g.id WHERE a.kind='self' AND a.status='gc_submitted' ORDER BY a.created_at DESC"
        ).fetchall()

    unread = unread_count(db, user["id"])
    return render_template("new_assessment.html", today=date.today().isoformat(),
                           gcs=gcs, user=dict(user), prefill_gc=dict(prefill_gc) if prefill_gc else None,
                           submitted_self=submitted_self, unread=unread)


# ─── Assess ────────────────────────────────────────────────────────────────────

@app.route("/assess/<int:assessment_id>")
@login_required
def assess(assessment_id):
    db = get_db()
    user = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment or not can_access_assessment(user, assessment):
        return redirect(url_for("dashboard"))

    # GC assessor can only edit gc_draft assessments
    read_only = False
    if user["role"] == "gc_assessor" and assessment["status"] != "gc_draft":
        read_only = True
    # Admin can always edit anything
    # SBU PIC can always edit validation assessments; read-only only for self-assessments they don't own
    if user["role"] == "sbu_pic" and assessment["kind"] == "self" and assessment["status"] != "gc_draft":
        read_only = True

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    all_qs = all_questions_flat(pillars)

    all_levels = sorted({q["level"] for q in all_qs})
    all_methods = sorted({m for q in all_qs for m in q["audit_methods"]})
    all_answered_by = sorted({q["answered_by"] for q in all_qs if q["answered_by"]})
    all_standards = sorted({q["standard"] for q in all_qs if q["standard"]})
    all_elements = [(p["id"], e["id"], e["name"]) for p in pillars for e in p["elements"]]

    att_rows = db.execute(
        "SELECT question_id, id, original_name, mime_type FROM attachments WHERE assessment_id=?",
        (assessment_id,)
    ).fetchall()
    attachments = {}
    for r in att_rows:
        attachments.setdefault(r["question_id"], []).append(dict(r))

    scores = calculate_scores(pillars, ans)
    unread = unread_count(db, user["id"])

    # Load self-assessment responses as reference (for validation assessments)
    self_resp = {}
    self_resp_full = {}
    self_assessment = None
    self_scores = None
    if assessment["kind"] == "validation" and assessment["linked_to"]:
        self_assessment = db.execute(
            "SELECT * FROM assessments WHERE id=?", (assessment["linked_to"],)
        ).fetchone()
        self_resp_full = get_responses_dict(db, assessment["linked_to"])
        self_resp = answers_only(self_resp_full)
        self_scores = calculate_scores(pillars, self_resp)

    return render_template(
        "assess.html",
        assessment=dict(assessment),
        pillars=pillars,
        resp=resp,
        scores=scores,
        level_names=LEVEL_NAMES,
        level_colors=LEVEL_COLORS,
        method_labels=METHOD_LABELS,
        all_levels=all_levels,
        all_methods=all_methods,
        all_answered_by=all_answered_by,
        all_standards=all_standards,
        all_elements=all_elements,
        attachments=attachments,
        total_q=len(all_qs),
        answered_q=sum(1 for q in all_qs if ans.get(q["id"]) not in (None, "")),
        user=dict(user),
        read_only=read_only,
        unread=unread,
        self_resp=self_resp,
        self_resp_full=self_resp_full,
        self_scores=self_scores,
        self_assessment=dict(self_assessment) if self_assessment else None,
    )


# ─── AJAX: auto-save answer ───────────────────────────────────────────────────

@app.route("/api/answer", methods=["POST"])
@login_required
def api_answer():
    data = request.get_json()
    assessment_id = data.get("assessment_id")
    question_id = data.get("question_id")
    answer = data.get("answer", "")
    comment = data.get("comment", "")

    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    db.execute(
        """INSERT INTO responses (assessment_id, question_id, answer, comment, updated_at)
           VALUES (?,?,?,?,datetime('now'))
           ON CONFLICT(assessment_id, question_id)
           DO UPDATE SET answer=excluded.answer, comment=excluded.comment, updated_at=excluded.updated_at""",
        (assessment_id, question_id, answer, comment)
    )
    db.commit()

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    all_qs = all_questions_flat(pillars)
    scores = calculate_scores(pillars, ans)
    answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None, ""))

    return jsonify({
        "scores": scores,
        "answered": answered,
        "total": len(all_qs),
        "level_name": scores["level_name"],
        "level_names": LEVEL_NAMES,
    })


# ─── AJAX: file upload ────────────────────────────────────────────────────────

@app.route("/api/upload/<int:assessment_id>/<question_id>", methods=["POST"])
@login_required
def api_upload(assessment_id, question_id):
    db = get_db()
    if not db.execute("SELECT id FROM assessments WHERE id=?", (assessment_id,)).fetchone():
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

    # Store file content directly in DB as BLOB — no filesystem needed
    db.execute(
        "INSERT INTO attachments (assessment_id, question_id, original_name, mime_type, file_data) VALUES (?,?,?,?,?)",
        (assessment_id, question_id, file.filename, file.content_type, content)
    )
    db.commit()
    att_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    return jsonify({
        "id": att_id,
        "original_name": file.filename,
        "is_image": ext in {".jpg", ".jpeg", ".png", ".gif"},
        "url": url_for("serve_attachment", att_id=att_id),
    })


@app.route("/attachment/<int:att_id>")
@login_required
def serve_attachment(att_id):
    db = get_db()
    row = db.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if not row or not row["file_data"]:
        abort(404)
    return send_file(
        io.BytesIO(row["file_data"]),
        download_name=row["original_name"],
        mimetype=row["mime_type"] or "application/octet-stream"
    )


@app.route("/api/attachment/<int:att_id>", methods=["DELETE"])
@login_required
def delete_attachment(att_id):
    db = get_db()
    if not db.execute("SELECT id FROM attachments WHERE id=?", (att_id,)).fetchone():
        return jsonify({"error": "not found"}), 404
    db.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    db.commit()
    return jsonify({"ok": True})


# ─── Workflow: GC submits self-assessment ─────────────────────────────────────

@app.route("/api/submit/<int:assessment_id>", methods=["POST"])
@login_required
def api_submit(assessment_id):
    db = get_db()
    user = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    db.execute(
        "UPDATE assessments SET status='gc_submitted', submitted_at=datetime('now') WHERE id=?",
        (assessment_id,)
    )
    # Notify SBU PIC and admins
    gc = db.execute("SELECT * FROM gcs WHERE id=?", (assessment["gc_id"],)).fetchone() if assessment["gc_id"] else None
    gc_name = gc["name"] if gc else assessment["site_name"]
    msg = f"Self-assessment submitted by {gc_name} — awaiting validation."
    link = url_for("result", assessment_id=assessment_id)
    notify_role(db, "sbu_pic", msg, link)
    notify_role(db, "admin", msg, link)
    db.commit()
    return jsonify({"redirect": url_for("result", assessment_id=assessment_id)})


# ─── Workflow: SBU starts validation ─────────────────────────────────────────

@app.route("/api/start-validation/<int:self_assessment_id>", methods=["POST"])
@role_required("sbu_pic", "admin")
def api_start_validation(self_assessment_id):
    db = get_db()
    user = current_user()
    sa = db.execute("SELECT * FROM assessments WHERE id=?", (self_assessment_id,)).fetchone()
    if not sa:
        return jsonify({"error": "not found"}), 404

    cur = db.execute(
        """INSERT INTO assessments
           (site_name, type, scope, gc_id, kind, linked_to, assessment_date, status, created_by)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (sa["site_name"], sa["type"], sa["scope"], sa["gc_id"],
         "validation", self_assessment_id, date.today().isoformat(),
         "validation_in_progress", user["id"])
    )
    db.commit()
    return jsonify({"redirect": url_for("assess", assessment_id=cur.lastrowid)})


# ─── Workflow: complete assessment ────────────────────────────────────────────

@app.route("/api/complete/<int:assessment_id>", methods=["POST"])
@login_required
def api_complete(assessment_id):
    db = get_db()
    user = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    if assessment["kind"] == "self" or user["role"] == "gc_assessor":
        # GC finishes → stays as gc_draft (they use Submit button to submit)
        db.execute("UPDATE assessments SET status='gc_draft' WHERE id=?", (assessment_id,))
    else:
        # SBU finishes validation → set to validation_in_progress so Share Report button appears
        # If already shared/completed, keep existing status so findings aren't reset
        if assessment["status"] not in ("report_shared", "completed"):
            db.execute("UPDATE assessments SET status='validation_in_progress' WHERE id=?", (assessment_id,))

    db.commit()
    return jsonify({"redirect": url_for("result", assessment_id=assessment_id)})


# ─── Workflow: SBU shares report ─────────────────────────────────────────────

@app.route("/api/share-report/<int:assessment_id>", methods=["POST"])
@role_required("sbu_pic", "admin")
def api_share_report(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return jsonify({"error": "not found"}), 404

    db.execute(
        "UPDATE assessments SET status='report_shared', shared_at=datetime('now') WHERE id=?",
        (assessment_id,)
    )

    # Auto-generate findings from all "no" answers in validation
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    for pillar in pillars:
        for element in pillar["elements"]:
            for q in element["questions"]:
                if resp.get(q["id"], {}).get("answer") == "no":
                    # Check if finding already exists
                    exists = db.execute(
                        "SELECT id FROM findings WHERE assessment_id=? AND question_id=?",
                        (assessment_id, q["id"])
                    ).fetchone()
                    if not exists:
                        db.execute(
                            """INSERT INTO findings (assessment_id, question_id, pillar_id, element_id, status)
                               VALUES (?,?,?,?,?)""",
                            (assessment_id, q["id"], pillar["id"], element["id"], "open")
                        )

    # Notify GC assessors
    if assessment["gc_id"]:
        gc = db.execute("SELECT * FROM gcs WHERE id=?", (assessment["gc_id"],)).fetchone()
        gc_name = gc["name"] if gc else assessment["site_name"]
        msg = f"Validation report shared for {gc_name}. Please review findings and add action plans."
        notify_gc_users(db, assessment["gc_id"], msg, url_for("findings", assessment_id=assessment_id))

    db.commit()
    return jsonify({"redirect": url_for("findings", assessment_id=assessment_id)})


# ─── AJAX: live scores ────────────────────────────────────────────────────────

@app.route("/api/scores/<int:assessment_id>")
@login_required
def api_scores(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return jsonify({}), 404
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    all_qs = all_questions_flat(pillars)
    scores = calculate_scores(pillars, ans)
    answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None, ""))
    return jsonify({
        "scores": scores,
        "answered": answered,
        "total": len(all_qs),
        "level_names": LEVEL_NAMES,
    })


# ─── Result ───────────────────────────────────────────────────────────────────

@app.route("/result/<int:assessment_id>")
@login_required
def result(assessment_id):
    db = get_db()
    user = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment or not can_access_assessment(user, assessment):
        return redirect(url_for("dashboard"))

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    scores = calculate_scores(pillars, ans)

    att_rows = db.execute("SELECT * FROM attachments WHERE assessment_id=?", (assessment_id,)).fetchall()
    attachments = {}
    for r in att_rows:
        attachments.setdefault(r["question_id"], []).append(dict(r))

    # Self-assessment linked to this (for SBU comparison)
    self_assessment = None
    if assessment["linked_to"]:
        self_assessment = db.execute(
            "SELECT * FROM assessments WHERE id=?", (assessment["linked_to"],)
        ).fetchone()

    gc = db.execute("SELECT * FROM gcs WHERE id=?", (assessment["gc_id"],)).fetchone() if assessment["gc_id"] else None
    findings_count = db.execute(
        "SELECT COUNT(*) FROM findings WHERE assessment_id=?", (assessment_id,)
    ).fetchone()[0]

    unread = unread_count(db, user["id"])
    return render_template(
        "result.html",
        assessment=dict(assessment),
        pillars=pillars,
        responses=ans,
        comments={qid: v["comment"] for qid, v in resp.items()},
        attachments=attachments,
        scores=scores,
        level_names=LEVEL_NAMES,
        level_colors=LEVEL_COLORS,
        user=dict(user),
        self_assessment=dict(self_assessment) if self_assessment else None,
        gc=dict(gc) if gc else None,
        findings_count=findings_count,
        unread=unread,
        status_labels=STATUS_LABELS,
    )


# ─── Findings ─────────────────────────────────────────────────────────────────

@app.route("/findings/<int:assessment_id>")
@login_required
def findings(assessment_id):
    db = get_db()
    user = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment or not can_access_assessment(user, assessment):
        return redirect(url_for("dashboard"))

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    scores = calculate_scores(pillars, ans)

    # Build question lookup
    q_lookup = {}
    p_lookup = {}
    e_lookup = {}
    for p in pillars:
        p_lookup[p["id"]] = p["name"]
        for e in p["elements"]:
            e_lookup[e["id"]] = e["name"]
            for q in e["questions"]:
                q_lookup[q["id"]] = q

    findings_rows = db.execute(
        "SELECT * FROM findings WHERE assessment_id=? ORDER BY status, pillar_id, element_id",
        (assessment_id,)
    ).fetchall()

    gc = db.execute("SELECT * FROM gcs WHERE id=?", (assessment["gc_id"],)).fetchone() if assessment["gc_id"] else None
    unread = unread_count(db, user["id"])

    return render_template(
        "findings.html",
        assessment=dict(assessment),
        findings=[dict(f) for f in findings_rows],
        q_lookup=q_lookup,
        p_lookup=p_lookup,
        e_lookup=e_lookup,
        scores=scores,
        level_names=LEVEL_NAMES,
        user=dict(user),
        gc=dict(gc) if gc else None,
        unread=unread,
        status_labels=STATUS_LABELS,
    )


@app.route("/api/finding/<int:finding_id>", methods=["POST"])
@login_required
def api_update_finding(finding_id):
    db = get_db()
    data = request.get_json()
    db.execute(
        """UPDATE findings SET action_plan=?, responsible=?, due_date=?, status=?,
           updated_at=datetime('now') WHERE id=?""",
        (data.get("action_plan", ""), data.get("responsible", ""),
         data.get("due_date", ""), data.get("status", "open"), finding_id)
    )
    db.commit()
    return jsonify({"ok": True})


# ─── Notifications ────────────────────────────────────────────────────────────

@app.route("/notifications")
@login_required
def notifications():
    db = get_db()
    user = current_user()
    notes = db.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)
    ).fetchall()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    db.commit()
    unread = 0
    return render_template("notifications.html", notifications=notes,
                           user=dict(user), unread=unread)


# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route("/admin")
@role_required("admin")
def admin_panel():
    db = get_db()
    user = current_user()
    users = db.execute(
        "SELECT u.*, g.name as gc_name FROM users u LEFT JOIN gcs g ON u.gc_id=g.id ORDER BY u.role, u.username"
    ).fetchall()
    gcs = db.execute("SELECT * FROM gcs ORDER BY name").fetchall()
    unread = unread_count(db, user["id"])
    return render_template("admin.html", users=users, gcs=gcs,
                           user=dict(user), roles=ROLES, unread=unread)


@app.route("/admin/user/new", methods=["POST"])
@role_required("admin")
def admin_new_user():
    db = get_db()
    username = request.form["username"].strip()
    full_name = request.form["full_name"].strip()
    password = request.form["password"]
    role = request.form["role"]
    gc_id = request.form.get("gc_id") or None
    if gc_id:
        gc_id = int(gc_id)
    try:
        db.execute(
            "INSERT INTO users (username, full_name, password_hash, role, gc_id) VALUES (?,?,?,?,?)",
            (username, full_name, generate_password_hash(password, method="pbkdf2:sha256"), role, gc_id)
        )
        db.commit()
        flash(f"User '{username}' created.", "success")
    except sqlite3.IntegrityError:
        flash(f"Username '{username}' already exists.", "danger")
    return redirect(url_for("admin_panel"))


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_user(user_id):
    db = get_db()
    if user_id == session["user_id"]:
        flash("Cannot delete yourself.", "danger")
        return redirect(url_for("admin_panel"))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash("User deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/user/<int:user_id>/reset", methods=["POST"])
@role_required("admin")
def admin_reset_password(user_id):
    db = get_db()
    new_pw = request.form["new_password"]
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(new_pw, method="pbkdf2:sha256"), user_id))
    db.commit()
    flash("Password reset.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/gc/new", methods=["POST"])
@role_required("admin")
def admin_new_gc():
    db = get_db()
    name = request.form["name"].strip()
    atype = request.form["type"]
    scope = request.form.get("scope") or None
    db.execute("INSERT INTO gcs (name, type, scope) VALUES (?,?,?)", (name, atype, scope))
    db.commit()
    flash(f"GC '{name}' created.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/gc/<int:gc_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_gc(gc_id):
    db = get_db()
    db.execute("DELETE FROM gcs WHERE id=?", (gc_id,))
    db.commit()
    flash("GC deleted.", "success")
    return redirect(url_for("admin_panel"))


# ─── Export ───────────────────────────────────────────────────────────────────

@app.route("/export/<int:assessment_id>/pdf")
@login_required
def export_pdf(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return redirect(url_for("dashboard"))
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    comments = {qid: v["comment"] for qid, v in resp.items()}
    scores = calculate_scores(pillars, ans)
    from export.pdf_report import generate_pdf
    pdf_bytes = generate_pdf(dict(assessment), pillars, ans, scores, LEVEL_NAMES, comments)
    return send_file(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True,
        download_name=f"SMA_{assessment['site_name']}_{assessment['assessment_date']}.pdf"
    )


@app.route("/export/<int:assessment_id>/excel")
@login_required
def export_excel(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not assessment:
        return redirect(url_for("dashboard"))
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id)
    ans = answers_only(resp)
    comments = {qid: v["comment"] for qid, v in resp.items()}
    scores = calculate_scores(pillars, ans)
    from export.excel_report import generate_excel
    excel_bytes = generate_excel(dict(assessment), pillars, ans, scores, LEVEL_NAMES, comments)
    return send_file(
        io.BytesIO(excel_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"SMA_{assessment['site_name']}_{assessment['assessment_date']}.xlsx"
    )


@app.route("/delete/<int:assessment_id>", methods=["POST"])
@role_required("admin", "sbu_pic")
def delete_assessment(assessment_id):
    db = get_db()
    db.execute("DELETE FROM findings WHERE assessment_id=?", (assessment_id,))
    db.execute("DELETE FROM attachments WHERE assessment_id=?", (assessment_id,))
    db.execute("DELETE FROM responses WHERE assessment_id=?", (assessment_id,))
    db.execute("DELETE FROM assessments WHERE id=?", (assessment_id,))
    db.commit()
    return redirect(url_for("dashboard"))


@app.route("/admin/backup")
@role_required("admin")
def admin_backup():
    """Download a copy of the SQLite database."""
    return send_file(
        str(DB_PATH),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=f"sma_backup_{date.today().isoformat()}.db"
    )


@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


if __name__ == "__main__":
    init_db()
    print("SMA System running at http://localhost:5001")
    print("Default login: admin / admin123")
    app.run(debug=True, port=5001)

"""
SMA Check Item System — Simplified single-user edition
Run: python3 app.py  →  open http://localhost:5001
"""
import json, os, sqlite3, io
from datetime import date
from functools import wraps
from pathlib import Path
from flask import (Flask, render_template, request, redirect,
                   url_for, g, send_file, jsonify, abort, session, flash)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "sma-bsapic-2026-secret")

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DB_PATH  = DATA_DIR / "sma.db"
Q_DIR    = BASE_DIR / "questions"

ALLOWED_EXTENSIONS = {".jpg",".jpeg",".png",".gif",".pdf",".doc",".docx",".xls",".xlsx"}
MAX_FILE_MB = 20

LEVEL_NAMES  = {1:"Ad-hoc",2:"Reactive",3:"Standardized",4:"Proactive",5:"Excellence"}
LEVEL_COLORS = {1:"danger",2:"warning",3:"info",4:"primary",5:"success"}
METHOD_LABELS = {"interview":"Interview","onsite":"On-site","document":"Document"}


# ─── Questions ────────────────────────────────────────────────────────────────

def load_questions(assessment_type, scope=None):
    if assessment_type == "warehouse":
        return json.loads((Q_DIR/"warehouse.json").read_text())["pillars"]
    elif assessment_type == "retail":
        return json.loads((Q_DIR/"retail.json").read_text())["scopes"][scope or "store"]["pillars"]
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
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
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
            assessor_a TEXT,
            assessor_b TEXT,
            assessment_date TEXT,
            status TEXT DEFAULT 'in_progress',
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
    """)
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        db.execute("INSERT INTO users (username, full_name, password_hash) VALUES (?,?,?)",
            ("admin","Administrator",generate_password_hash("admin123",method="pbkdf2:sha256")))
        db.commit()
    db.commit(); db.close()


# ─── Auth ─────────────────────────────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    if not uid: return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Auto-login as the first user — no password required
        if not session.get("user_id"):
            user = get_db().execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
            if user:
                session["user_id"] = user["id"]
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET","POST"])
def login():
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))


# ─── Score calculation ────────────────────────────────────────────────────────

def calculate_scores(pillars, responses):
    pillar_scores = {}
    for pillar in pillars:
        element_scores = {}
        for element in pillar["elements"]:
            applicable = [q for q in element["questions"]
                          if responses.get(q["id"]) not in ("na","not_rolled_out",None,"")]
            if not applicable:
                element_scores[element["id"]] = None
                continue
            by_level = {}
            for q in applicable:
                by_level.setdefault(q["level"],[]).append(responses.get(q["id"]))
            score = 5
            for lv in sorted(by_level.keys()):
                if "no" in by_level[lv]:
                    score = max(1, lv - 1); break
            element_scores[element["id"]] = score
        valid = [s for s in element_scores.values() if s is not None]
        pillar_scores[pillar["id"]] = {"score": min(valid) if valid else None, "elements": element_scores}

    def avg(*vals):
        v = [x for x in vals if x is not None]
        return round(sum(v)/len(v),2) if v else None

    ld   = pillar_scores.get("leadership",    {}).get("score")
    tm   = pillar_scores.get("tm_engagement", {}).get("score")
    org  = pillar_scores.get("organization",  {}).get("score")
    sys_ = pillar_scores.get("system",        {}).get("score")
    sa = avg(ld,tm); si = avg(org,sys_); overall = avg(sa,si)

    return {"overall":overall,"safety_awareness":sa,"system_implementation":si,
            "pillars":pillar_scores,
            "level_name":LEVEL_NAMES.get(int(overall) if overall else 0,"—")}

def get_responses_dict(db, assessment_id):
    rows = db.execute("SELECT question_id, answer, comment FROM responses WHERE assessment_id=?",(assessment_id,)).fetchall()
    return {r["question_id"]:{"answer":r["answer"],"comment":r["comment"] or ""} for r in rows}

def answers_only(resp_dict):
    return {qid: v["answer"] for qid,v in resp_dict.items()}


# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def dashboard():
    db   = get_db()
    user = current_user()
    assessments_raw = db.execute("SELECT * FROM assessments ORDER BY created_at DESC").fetchall()
    enriched = []
    for a in assessments_raw:
        pillars  = load_questions(a["type"], a["scope"])
        resp     = get_responses_dict(db, a["id"])
        ans      = answers_only(resp)
        all_qs   = all_questions_flat(pillars)
        answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None,""))
        scores   = calculate_scores(pillars, ans) if ans else None
        gc = db.execute("SELECT * FROM gcs WHERE id=?",(a["gc_id"],)).fetchone() if a["gc_id"] else None
        enriched.append({"a":dict(a),"scores":scores,"total_q":len(all_qs),
                         "answered_q":answered,"gc":dict(gc) if gc else None})
    return render_template("dashboard.html", assessments=enriched,
                           level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS,
                           user=dict(user), unread=0,
                           status_labels={"in_progress":("In Progress","warning"),"done":("Done","success")})


# ─── Analysis ────────────────────────────────────────────────────────────────

@app.route("/analysis")
@login_required
def analysis():
    db   = get_db()
    user = current_user()
    assessments = db.execute("SELECT * FROM assessments ORDER BY assessment_date DESC, site_name").fetchall()
    sites = []
    for a in assessments:
        pillars = load_questions(a["type"], a["scope"])
        resp    = get_responses_dict(db, a["id"])
        ans     = answers_only(resp)
        scores  = calculate_scores(pillars, ans) if ans else None
        if not scores or scores["overall"] is None:
            continue
        # Build per-element detail
        pillar_detail = {}
        for p in pillars:
            ps = scores["pillars"].get(p["id"], {})
            elems = {}
            for e in p["elements"]:
                es = ps.get("elements", {}).get(e["id"])
                # count yes/no/na per element
                eqs = e["questions"]
                cts = {"yes":0,"no":0,"na":0,"not_rolled_out":0,"total":len(eqs)}
                for q in eqs:
                    a_ = ans.get(q["id"])
                    if a_ in cts: cts[a_] += 1
                elems[e["id"]] = {"name": e["name"], "score": es, "counts": cts}
            pillar_detail[p["id"]] = {"name": p["name"], "score": ps.get("score"), "elements": elems}
        sites.append({
            "id":         a["id"],
            "name":       a["site_name"],
            "date":       a["assessment_date"] or "",
            "overall":    scores["overall"],
            "sa":         scores["safety_awareness"],
            "si":         scores["system_implementation"],
            "level_name": scores["level_name"],
            "pillars":    pillar_detail,
        })
    return render_template("analysis.html", sites=sites, user=dict(user),
                           level_names=LEVEL_NAMES, unread=0)


# ─── New assessment ───────────────────────────────────────────────────────────

@app.route("/new", methods=["GET","POST"])
@login_required
def new_assessment():
    db   = get_db()
    user = current_user()
    gcs  = db.execute("SELECT * FROM gcs ORDER BY name").fetchall()
    if request.method == "POST":
        gc_id = request.form.get("gc_id") or None
        if gc_id: gc_id = int(gc_id)
        cur = db.execute(
            "INSERT INTO assessments (site_name,type,scope,gc_id,assessor_a,assessor_b,assessment_date,status) VALUES (?,?,?,?,?,?,?,'in_progress')",
            (request.form["site_name"].strip(), request.form["type"],
             request.form.get("scope","store"), gc_id,
             request.form.get("assessor_a","").strip(), request.form.get("assessor_b","").strip(),
             request.form.get("assessment_date") or date.today().isoformat())
        )
        db.commit()
        return redirect(url_for("assess", assessment_id=cur.lastrowid))
    return render_template("new_assessment.html", today=date.today().isoformat(),
                           gcs=gcs, user=dict(user), unread=0, prefill_gc=None, submitted_self=[])


# ─── Assess ───────────────────────────────────────────────────────────────────

@app.route("/assess/<int:assessment_id>")
@login_required
def assess(assessment_id):
    db         = get_db()
    user       = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return redirect(url_for("dashboard"))

    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    ans     = answers_only(resp)
    all_qs  = all_questions_flat(pillars)

    att_rows = db.execute("SELECT question_id,id,original_name,mime_type FROM attachments WHERE assessment_id=?",(assessment_id,)).fetchall()
    attachments = {}
    for r in att_rows:
        attachments.setdefault(r["question_id"],[]).append(dict(r))

    return render_template("assess.html",
        assessment=dict(assessment), pillars=pillars, resp=resp,
        scores=calculate_scores(pillars, ans),
        level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS, method_labels=METHOD_LABELS,
        all_levels=sorted({q["level"] for q in all_qs}),
        all_methods=sorted({m for q in all_qs for m in q["audit_methods"]}),
        all_answered_by=sorted({q["answered_by"] for q in all_qs if q["answered_by"]}),
        all_standards=sorted({q["standard"] for q in all_qs if q["standard"]}),
        all_elements=[(p["id"],e["id"],e["name"]) for p in pillars for e in p["elements"]],
        attachments=attachments,
        total_q=len(all_qs),
        answered_q=sum(1 for q in all_qs if ans.get(q["id"]) not in (None,"")),
        user=dict(user), read_only=False, unread=0,
        self_resp={}, self_resp_full={}, self_scores=None, self_assessment=None)


# ─── AJAX: auto-save ──────────────────────────────────────────────────────────

@app.route("/api/answer", methods=["POST"])
@login_required
def api_answer():
    data = request.get_json()
    assessment_id = data.get("assessment_id")
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return jsonify({"error":"not found"}),404
    db.execute(
        """INSERT INTO responses (assessment_id,question_id,answer,comment,updated_at)
           VALUES (?,?,?,?,datetime('now'))
           ON CONFLICT(assessment_id,question_id)
           DO UPDATE SET answer=excluded.answer,comment=excluded.comment,updated_at=excluded.updated_at""",
        (assessment_id,data.get("question_id"),data.get("answer",""),data.get("comment",""))
    )
    db.commit()
    pillars  = load_questions(assessment["type"], assessment["scope"])
    resp     = get_responses_dict(db, assessment_id)
    ans      = answers_only(resp)
    all_qs   = all_questions_flat(pillars)
    scores   = calculate_scores(pillars, ans)
    answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None,""))
    return jsonify({"scores":scores,"answered":answered,"total":len(all_qs),
                    "level_name":scores["level_name"],"level_names":LEVEL_NAMES})


# ─── File upload ──────────────────────────────────────────────────────────────

@app.route("/api/upload/<int:assessment_id>/<question_id>", methods=["POST"])
@login_required
def api_upload(assessment_id, question_id):
    db = get_db()
    if not db.execute("SELECT id FROM assessments WHERE id=?",(assessment_id,)).fetchone():
        return jsonify({"error":"not found"}),404
    file = request.files.get("file")
    if not file or not file.filename: return jsonify({"error":"no file"}),400
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS: return jsonify({"error":f"Type {ext} not allowed"}),400
    content = file.read()
    if len(content) > MAX_FILE_MB*1024*1024: return jsonify({"error":"File too large"}),400
    db.execute("INSERT INTO attachments (assessment_id,question_id,original_name,mime_type,file_data) VALUES (?,?,?,?,?)",
               (assessment_id,question_id,file.filename,file.content_type,content))
    db.commit()
    att_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"id":att_id,"original_name":file.filename,
                    "is_image":ext in {".jpg",".jpeg",".png",".gif"},
                    "url":url_for("serve_attachment",att_id=att_id)})

@app.route("/attachment/<int:att_id>")
@login_required
def serve_attachment(att_id):
    row = get_db().execute("SELECT * FROM attachments WHERE id=?",(att_id,)).fetchone()
    if not row or not row["file_data"]: abort(404)
    return send_file(io.BytesIO(row["file_data"]),download_name=row["original_name"],
                     mimetype=row["mime_type"] or "application/octet-stream")

@app.route("/api/attachment/<int:att_id>", methods=["DELETE"])
@login_required
def delete_attachment(att_id):
    db = get_db()
    if not db.execute("SELECT id FROM attachments WHERE id=?",(att_id,)).fetchone():
        return jsonify({"error":"not found"}),404
    db.execute("DELETE FROM attachments WHERE id=?",(att_id,))
    db.commit()
    return jsonify({"ok":True})


# ─── Scores / Result / Findings ───────────────────────────────────────────────

@app.route("/api/scores/<int:assessment_id>")
@login_required
def api_scores(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return jsonify({}),404
    pillars  = load_questions(assessment["type"], assessment["scope"])
    resp     = get_responses_dict(db, assessment_id)
    ans      = answers_only(resp)
    all_qs   = all_questions_flat(pillars)
    scores   = calculate_scores(pillars, ans)
    answered = sum(1 for q in all_qs if ans.get(q["id"]) not in (None,""))
    return jsonify({"scores":scores,"answered":answered,"total":len(all_qs),"level_names":LEVEL_NAMES})

@app.route("/result/<int:assessment_id>")
@login_required
def result(assessment_id):
    db         = get_db()
    user       = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return redirect(url_for("dashboard"))
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    ans     = answers_only(resp)
    scores  = calculate_scores(pillars, ans)
    att_rows = db.execute("SELECT * FROM attachments WHERE assessment_id=?",(assessment_id,)).fetchall()
    attachments = {}
    for r in att_rows: attachments.setdefault(r["question_id"],[]).append(dict(r))
    gc = db.execute("SELECT * FROM gcs WHERE id=?",(assessment["gc_id"],)).fetchone() if assessment["gc_id"] else None
    findings_count = db.execute("SELECT COUNT(*) FROM findings WHERE assessment_id=?",(assessment_id,)).fetchone()[0]
    return render_template("result.html",
        assessment=dict(assessment), pillars=pillars, responses=ans,
        comments={qid:v["comment"] for qid,v in resp.items()},
        attachments=attachments, scores=scores,
        level_names=LEVEL_NAMES, level_colors=LEVEL_COLORS,
        user=dict(user), gc=dict(gc) if gc else None,
        findings_count=findings_count, unread=0, self_assessment=None,
        status_labels={"in_progress":("In Progress","warning"),"done":("Done","success")})

@app.route("/findings/<int:assessment_id>")
@login_required
def findings(assessment_id):
    db         = get_db()
    user       = current_user()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return redirect(url_for("dashboard"))
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    ans     = answers_only(resp)
    scores  = calculate_scores(pillars, ans)
    q_lookup={}; p_lookup={}; e_lookup={}
    for p in pillars:
        p_lookup[p["id"]] = p["name"]
        for e in p["elements"]:
            e_lookup[e["id"]] = e["name"]
            for q in e["questions"]: q_lookup[q["id"]] = q
    findings_rows = db.execute("SELECT * FROM findings WHERE assessment_id=? ORDER BY status,pillar_id,element_id",(assessment_id,)).fetchall()
    gc = db.execute("SELECT * FROM gcs WHERE id=?",(assessment["gc_id"],)).fetchone() if assessment["gc_id"] else None
    return render_template("findings.html",
        assessment=dict(assessment), findings=[dict(f) for f in findings_rows],
        q_lookup=q_lookup, p_lookup=p_lookup, e_lookup=e_lookup,
        scores=scores, level_names=LEVEL_NAMES, user=dict(user),
        gc=dict(gc) if gc else None, unread=0,
        status_labels={"in_progress":("In Progress","warning"),"done":("Done","success")})

@app.route("/api/generate-findings/<int:assessment_id>", methods=["POST"])
@login_required
def api_generate_findings(assessment_id):
    db         = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return jsonify({"error":"not found"}),404
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp    = get_responses_dict(db, assessment_id)
    count = 0
    for pillar in pillars:
        for element in pillar["elements"]:
            for q in element["questions"]:
                if resp.get(q["id"],{}).get("answer") == "no":
                    if not db.execute("SELECT id FROM findings WHERE assessment_id=? AND question_id=?",(assessment_id,q["id"])).fetchone():
                        db.execute("INSERT INTO findings (assessment_id,question_id,pillar_id,element_id,status) VALUES (?,?,?,?,?)",
                                   (assessment_id,q["id"],pillar["id"],element["id"],"open"))
                        count += 1
    db.commit()
    return jsonify({"ok":True,"generated":count})

@app.route("/api/finding/<int:finding_id>", methods=["POST"])
@login_required
def api_update_finding(finding_id):
    data = request.get_json()
    db   = get_db()
    db.execute("UPDATE findings SET action_plan=?,responsible=?,due_date=?,status=?,updated_at=datetime('now') WHERE id=?",
               (data.get("action_plan",""),data.get("responsible",""),data.get("due_date",""),data.get("status","open"),finding_id))
    db.commit()
    return jsonify({"ok":True})

@app.route("/api/mark-done/<int:assessment_id>", methods=["POST"])
@login_required
def api_mark_done(assessment_id):
    db = get_db()
    db.execute("UPDATE assessments SET status='done' WHERE id=?",(assessment_id,))
    db.commit()
    return jsonify({"ok":True})


# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route("/admin")
@login_required
def admin_panel():
    db   = get_db()
    user = current_user()
    users = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    gcs   = db.execute("SELECT * FROM gcs ORDER BY name").fetchall()
    return render_template("admin.html", users=users, gcs=gcs,
                           user=dict(user), roles={"admin":"Admin"}, unread=0)

@app.route("/admin/user/new", methods=["POST"])
@login_required
def admin_new_user():
    db = get_db()
    try:
        db.execute("INSERT INTO users (username,full_name,password_hash) VALUES (?,?,?)",
            (request.form["username"].strip(), request.form["full_name"].strip(),
             generate_password_hash(request.form["password"],method="pbkdf2:sha256")))
        db.commit(); flash("User created.","success")
    except sqlite3.IntegrityError:
        flash("Username already exists.","danger")
    return redirect(url_for("admin_panel"))

@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
@login_required
def admin_delete_user(user_id):
    db = get_db()
    if user_id == session["user_id"]:
        flash("Cannot delete yourself.","danger")
        return redirect(url_for("admin_panel"))
    db.execute("DELETE FROM users WHERE id=?",(user_id,))
    db.commit(); flash("User deleted.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/user/<int:user_id>/reset", methods=["POST"])
@login_required
def admin_reset_password(user_id):
    db = get_db()
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(request.form["new_password"],method="pbkdf2:sha256"),user_id))
    db.commit(); flash("Password reset.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/gc/new", methods=["POST"])
@login_required
def admin_new_gc():
    db = get_db()
    db.execute("INSERT INTO gcs (name,type,scope) VALUES (?,?,?)",
               (request.form["name"].strip(), request.form["type"], request.form.get("scope") or None))
    db.commit(); flash(f"GC created.","success")
    return redirect(url_for("admin_panel"))

@app.route("/admin/gc/<int:gc_id>/delete", methods=["POST"])
@login_required
def admin_delete_gc(gc_id):
    db = get_db()
    db.execute("DELETE FROM gcs WHERE id=?",(gc_id,))
    db.commit(); flash("GC deleted.","success")
    return redirect(url_for("admin_panel"))


# ─── Export / Delete ──────────────────────────────────────────────────────────

@app.route("/export/<int:assessment_id>/pdf")
@login_required
def export_pdf(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return redirect(url_for("dashboard"))
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id); ans = answers_only(resp)
    from export.pdf_report import generate_pdf
    pdf_bytes = generate_pdf(dict(assessment), pillars, ans,
                             calculate_scores(pillars,ans), LEVEL_NAMES,
                             {qid:v["comment"] for qid,v in resp.items()})
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True,
                     download_name=f"SMA_{assessment['site_name']}_{assessment['assessment_date']}.pdf")

@app.route("/export/<int:assessment_id>/excel")
@login_required
def export_excel(assessment_id):
    db = get_db()
    assessment = db.execute("SELECT * FROM assessments WHERE id=?",(assessment_id,)).fetchone()
    if not assessment: return redirect(url_for("dashboard"))
    pillars = load_questions(assessment["type"], assessment["scope"])
    resp = get_responses_dict(db, assessment_id); ans = answers_only(resp)
    from export.excel_report import generate_excel
    excel_bytes = generate_excel(dict(assessment), pillars, ans,
                                 calculate_scores(pillars,ans), LEVEL_NAMES,
                                 {qid:v["comment"] for qid,v in resp.items()})
    return send_file(io.BytesIO(excel_bytes),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"SMA_{assessment['site_name']}_{assessment['assessment_date']}.xlsx")

@app.route("/delete/<int:assessment_id>", methods=["POST"])
@login_required
def delete_assessment(assessment_id):
    db = get_db()
    for t,c in [("findings","assessment_id"),("attachments","assessment_id"),
                ("responses","assessment_id"),("assessments","id")]:
        db.execute(f"DELETE FROM {t} WHERE {c}=?",(assessment_id,))
    db.commit()
    return redirect(url_for("dashboard"))

@app.route("/admin/backup")
@login_required
def admin_backup():
    return send_file(str(DB_PATH), mimetype="application/octet-stream", as_attachment=True,
                     download_name=f"sma_backup_{date.today().isoformat()}.db")

@app.route("/api/export-all")
@login_required
def export_all():
    db = get_db()
    assessments = [dict(r) for r in db.execute("SELECT * FROM assessments").fetchall()]
    responses   = [dict(r) for r in db.execute(
        "SELECT assessment_id,question_id,answer,comment FROM responses").fetchall()]
    return jsonify({"assessments": assessments, "responses": responses})

@app.errorhandler(403)
def forbidden(e): return render_template("403.html"), 403

init_db()

if __name__ == "__main__":
    print("SMA System running at http://localhost:5001")
    print("Default login: admin / admin123")
    app.run(debug=True, port=5001)

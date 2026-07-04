# SMA System — Safety Maturity Assessment

A web application for conducting **Safety Maturity Assessments (SMA)** across business units.
Assessors answer level-based questions per site; the system scores them automatically
(element → pillar → overall) and presents results through a dashboard, an analysis matrix,
and PDF/Excel exports.

> **Current focus: Manufacturing (MFG) + Retread.** Warehouse and Retail are the original/legacy
> types (their historical records are retained). See `HANDOFF.md` for full project state and
> `SCORING.md` for the exact score-calculation spec.

---

## 1. What it does

- **Four assessment types** — **Manufacturing (MFG)**, **Retread**, and the legacy **Warehouse** and
  **Retail** (Store + Local Company scopes). Each has its own question bank and judgement criteria.
- **Run assessments** — answer Yes / No / N/A / Not-rolled-out per question, with comments and file
  attachments; auto-saves on every change.
- **Score automatically** — a maturity engine converts answers into a 1–5 score per element, pillar,
  and overall, split into *Safety Awareness* and *System Implementation* axes.
- **[MFG] Count-based judgement** — for Manufacturing, a question isn't a single click; you record
  **who was interviewed and their Yes/No**, and the app derives the question's answer (see §2).
- **Live Score panel** — overall + pillar/element scores update live; for MFG also a **per-system-item**
  breakdown and a **DP vs Safety** split.
- **Dashboard** — assessments grouped by business unit (🏭 MFG / ♻️ Retread / 🚛 Warehouse / 🛞 Retail).
- **Analysis & exports** — scatter matrix, radar charts; PDF and Excel reports per assessment.

---

## 2. Manufacturing (MFG) — count-based judgement  ⭐

This is the distinctive part of the system. Where other types answer a question with one Yes/No, **MFG
aggregates multiple interviewees into the answer**, using the rule:

> **A question is _Yes_ only if _every_ interviewed person answers Yes. Any single _No_ ⇒ the question is _No_.**
> Until enough people are recorded, it is **incomplete** (counts as unanswered).

Each MFG question is asked to **one role** (taken from the source Excel's column-H "Who?"). How that role's
people are captured depends on the role's **mode** (defined in `questions/manufacturing.json → role_config`):

| Mode | Roles | How you enter it |
|------|-------|------------------|
| **single** | Plant Manager, Production GM, Maintenance Manager, Safety Manager, DP Manager | one **Yes / No** |
| **count** | Supervisor* | **# interviewed / # Yes / # No** (amounts). Complete when `Yes+No ≥ expected`; any `No` ⇒ question No |
| **departments** | Production Manager | **add / delete** free-text **department** rows, each with **Yes/No** (up to 6) |
| **sections** | Teammate, Foreperson, Maintenance Foreman, Maintenance Staff, Supervisor | up to **6 collapsible named sections**; inside each, **add / remove named people** (name + Yes/No) |

Interaction details:
- **Add / remove** — departments and section-people are add/deletable (`+ add`, `✕`); sections have `+ add section` (max 6) and each is collapsible (show/hide).
- **Amounts** — `count` mode records numbers (interviewed / Yes / No); the expected N is editable inline.
- **Re-click to clear** — clicking an already-selected Yes/No **deselects** it back to unanswered.
- **Rollup (100% Yes)** — across all of a question's people/departments/counts: any `No` ⇒ **No**; all present and all `Yes` ⇒ **Yes**; otherwise **incomplete**.

The derived Yes/No is stored in `responses.answer`; the full per-person breakdown is stored in
`responses.detail` (JSON). **Because the derived value lands in `answer`, the pillar/overall scoring
(§4) is identical for every type** — MFG is not a special case to the maturity math.

**Live Score extras for MFG** (informational; do **not** change the pillar score):
- **System items** — each System subsystem (LOTO, Work at Height, Fire Fighting, …23 items) scored on its own.
- **DP vs Safety** — 🛡️ *System · Safety* and 🔥 *System · DP* aggregate scores + a DP/S tag per item.

Full algorithm (every mode + worked TBSCN example) → **`SCORING.md`**.

---

## 3. Data model (SQLite)

| Table | Purpose |
|-------|---------|
| `assessments` | One row per assessment (site, type, scope, assessors, status, date) |
| `responses` | One row per question: `answer`, `comment`, **`detail`** (JSON — MFG per-role/per-person counts) |
| `attachments` | Evidence files linked to a question |
| `findings` | Improvement actions generated from "No" answers |
| `users`, `gcs` | Users and group-company reference data |

Questions are **not** in the database — they live in `questions/*.json` (pillars → elements → questions,
each with `level`, `answered_by`, `audit_methods`, `standard`, `judgement_criteria`; MFG questions add
`responders` and a `dp` flag). MFG also has top-level `role_config` (role modes) and `department_options`.

---

## 4. How scoring works (all types)

1. **Element score** — questions grouped by maturity level 1–5. Walking up the levels, the **first level
   with a "No"** sets `score = max(1, level − 1)`; all "Yes" → 5. (N/A and Not-rolled-out are skipped.)
2. **Pillar score** = the **lowest** element score in that pillar.
3. **Axes** — *Safety Awareness* = avg(Leadership, Teammate); *System Implementation* = avg(Organization, System).
4. **Overall (SMA)** = avg(SA, SI).  Levels: `1 Ad-hoc · 2 Reactive · 3 Standardized · 4 Proactive · 5 Excellence`.

For MFG the per-question Yes/No first comes from §2, then feeds this unchanged. **Full spec: `SCORING.md`.**

---

## 5. Request flow (answering a question)

```
User answers  → JS POST /api/answer {assessment_id, question_id, answer|detail, comment}
   → Flask (MFG) derives Yes/No from detail via 100%-Yes rollup → stores answer + detail
   → recalculates scores from all answers + question JSON → returns scores JSON
   → JS updates the live score panel (no page reload)
```

---

## 6. Project structure

```
sma_system/
├── app.py                      # Flask: routes, load_questions, derive_answer (rollup),
│                               #        calculate_scores (+ system_items, DP/Safety), exports
├── questions/
│   ├── manufacturing.json      # MFG bank (233) + role_config + department_options   ⭐
│   ├── retread.json            # Retread bank (113)
│   ├── warehouse.json          # legacy Warehouse bank (159)
│   └── retail.json             # legacy Retail bank (Store 62 + Local Company 57)
├── templates/                  # Jinja2 HTML (assess.html = capture UI + Live Score panel)
├── static/                     # Bootstrap, Chart.js, custom style.css
├── export/                     # pdf_report.py, excel_report.py
├── import_tbscn.py             # TBSCN MFG result importer (data file is git-ignored)
├── requirements.txt            # flask, openpyxl, reportlab, gunicorn
├── HANDOFF.md                  # project state, deployment process, gotchas, next tasks
├── SCORING.md                  # exact score-calculation specification
└── sma.db                      # SQLite database (NOT committed — git-ignored)
```

> **Git-ignored (never in the repo):** `sma.db`, `uploads/`, `tbscn_responses.json`, `.env`.
> The repository holds the **application + question content only** — never assessment results.

---

## 7. Running locally

```bash
pip install -r requirements.txt
python app.py            # http://localhost:5001
```

Deployment (PythonAnywhere) and its gotchas are documented in **`HANDOFF.md §3`**.

---

## 8. Tech summary

`Python` · `Flask` · `Jinja2` · `SQLite` · `Bootstrap 5` · `Chart.js` ·
`reportlab` (PDF) · `openpyxl` (Excel) · `gunicorn` · hosted on **PythonAnywhere**.

📄 See also: **`HANDOFF.md`** (full state & next steps) · **`SCORING.md`** (calculation spec).

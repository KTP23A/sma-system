# SMA System — Project Handoff / Continuation Guide

> **Purpose:** everything a new session (or engineer) needs to continue this project.
> Last updated: 2026-07-04. Latest code commit at time of writing: see `git log -1`.

---

## 1. What this is
**SMA (Safety Maturity Assessment) System** — a Flask + SQLite web app for running BSANZ/BSAPIC
safety maturity validation assessments. Assessors answer level-based questions per site; the app
auto-scores (element → pillar → overall on a 1–5 maturity scale) and shows dashboards, analysis,
and PDF/Excel exports.

- **Owner:** Kantapon Arunrungruengkit (BSAPIC senior safety staff). Co-assessor: Aric Ng. Signatory: Anisara Tisamee.
- **This is the CANONICAL SMA system** (the original plan). A parallel Google Apps Script build exists
  (historical, `WORK/sma_gas_v2`) — do NOT treat its `/exec` URL as "the site".

## 2. Live / repo / access
- 🌐 **Live site:** https://kantapon.pythonanywhere.com
- 💻 **GitHub:** https://github.com/KTP23A/sma-system  (the old `kantapon230540-tech/sma-system`
  redirects here — same repo. `gh` acct `kantapon230540-tech` has push.)
- 📁 **Local:** `/Users/ktp/Desktop/WORK/sma_system`
- **Login today:** the deployed version AUTO-LOGS-IN as the first user (no password) — see §7 governance.

## 3. Deployment architecture & process  ⚠️ READ BEFORE DEPLOYING
- Host: **PythonAnywhere free tier**, project at `/home/kantapon/sma`, Python 3.13, no virtualenv,
  WSGI at `/var/www/kantapon_pythonanywhere_com_wsgi.py`. Free tier **auto-disables monthly** unless
  you log in and click "Run until 1 month from today".
- **The live server serves branch `phase1-simple`, NOT `main`.** `main` is the canonical/latest;
  `phase1-simple` gets the same changes via **cherry-pick**.
- **Deploy flow (what works):**
  1. Commit to `main` locally (local working branch is `main-prod` tracking `origin/main`); `git push origin HEAD:main`.
  2. On the PythonAnywhere **Bash console** (`/home/kantapon/sma`):
     `git fetch origin -q && GIT_EDITOR=true git cherry-pick <sha>`
     (or for a single JSON file: `git checkout origin/main -- questions/<file>.json`).
  3. If schema changed: run `python3 -c "import app; app.init_db()"` (idempotent; adds columns).
  4. **Reload** the web app on the **Web** tab. Then verify with `curl`.
- **GOTCHAS (all real, all hit this session):**
  - **Reload silently no-ops ~half the time.** Click Reload and WATCH FOR THE SPINNER; if no spinner, click again. Flask caches compiled templates, so template/app.py changes need a real reload.
  - The PythonAnywhere web console **display lags** — it often shows stale scrollback. Click the prompt line to focus, and re-screenshot to confirm.
  - **Do NOT type literal Japanese/CJK chars into the web console** — they garble/abort the line. Use ord-range escapes in Python one-liners.
  - **`manufacturing.json` and templates are read fresh per request; app.py is NOT** (needs reload).
  - **NEVER run verification JS (clicking Y/N, add/del) on a REAL record's live assess page** — it autosaves and corrupts data. (This corrupted TBSCN once; restored via `import_tbscn.py`.)

## 4. Assessment types & current scope
Types: **warehouse, retail (scope: store/local_company), manufacturing, retread.**
- **Direction (as of 2026-07-04): the system is moving to Manufacturing + Retread focus.**
  Warehouse & Retail are considered legacy. PENDING task: remove Warehouse/Retail from the *New
  Assessment* + Admin selectors and tidy WH/Retail-flavored labels — **but keep the existing WH/Retail
  records** (they are REAL data: Lytton, BSWX, BSTJ, JSDC, ZBDC warehouses; RAMA3, TIWANON, Truganina
  retail — several with real scores). Keep `load_questions` branches + JSON so old records still render.

## 5. Data — where it lives
- **All assessment data is in `sma.db` on the PythonAnywhere server ONLY.** It is **git-ignored**
  (`.gitignore`: `sma.db`, `uploads/`, `tbscn_responses.json`) — deliberately, to keep real data out of GitHub.
- **TBSCN** (Tire mfg. Mar-2025 MFG validation, overall **2.5 Reactive**) is the flagship MFG record.
  Its answers are in git-ignored `tbscn_responses.json`; re-import with `python3 import_tbscn.py`
  (idempotent — deletes+reinserts, so **its assessment id changes each run; currently id 33**).
- `responses` table has `answer`, `comment`, and a `detail` (JSON) column for responder counts (§6).

## 6. Feature inventory (Manufacturing — the sophisticated part)
Built this session, all live & backward-compatible (other types untouched; scoring engine unchanged):
- **Question→responder mapping from Excel column H "Who?"** (NOT the ✓ checkmarks — that was wrong).
  Each MFG question maps to ONE role. Decisions baked in: Management/procurement→Production Manager;
  MOC coord/patrol→Safety Manager; KY leader→Supervisor; Tour/blank→plain Yes/No.
  Review sheet: `/Users/ktp/Desktop/MFG_Question_to_Responder_MAP.xlsx`.
- **Count-based judgement** with strict **100%-Yes rollup** (any No ⇒ question No; incomplete until filled).
  Stored in `responses.detail`; derived Yes/No kept in `answer` so scoring is unchanged.
- **Role capture modes** (in `manufacturing.json` `role_config`):
  - `single` (Plant/Production GM/Maintenance/Safety/DP Manager) — one Yes/No.
  - `departments` (Production Manager) — add/deletable free-text department rows, each Y/N.
  - `sections` (Teammate, Supervisor, Foreperson, Maintenance Foreman/Staff) — up to 6 collapsible
    named sections, each with add/removable named people (name + Y/N).
- **Re-click a selected Y/N to clear it** (handle the INPUT click, not the label — Bootstrap btn-check re-checks otherwise).
- **Live Score panel extras:** per-**system-item** scores (23 subsystems by `standard`) + **DP vs Safety**
  split (🛡️ System·Safety / 🔥 System·DP + a DP/S tag per item). Validated 22/23 vs the official Excel "System detail".

## 7. Governance audit (2026-07-04) — findings still OPEN
🔴 **Critical (fix before real audit use):**
1. **No login — world-writable.** Auto-login means anyone with the URL can read/edit/delete/download everything (verified).
2. **`/admin/backup` downloads the full DB with no auth** (9.2 MB, verified anonymous).
3. **`admin/admin123` is the live default**, shown on the Admin page + console banner. `SECRET_KEY` has a hardcoded fallback. `app.run(debug=True)`.
4. **MFG PDF export crashes (HTTP 500)** — 4 MFG System questions have empty `text`; `pdf_report.py` does `None.split`. Excel export works.
🟡 **Process gaps:** no lock on completed assessments (`read_only=False` hardcoded); no audit trail (only latest answer); no question-bank version field; fragile manual deploy; single free-tier SQLite w/ monthly auto-disable + only-manual backup.

## 8. Strategic direction — M365 / SharePoint
Owner's foundation is **Microsoft 365; data standard is SharePoint** (see the M365 Incident System:
Power App + 4 SharePoint lists on CAPTC311 + Power BI; and `WORK/26BSANZ311_SMA_365_Migration_Proposal.pdf`).
SharePoint natively solves most governance gaps (Entra SSO, item-level permissions, **version history =
audit trail**, retention, backup). Options discussed:
- **A) Power Platform native** (SharePoint Lists + Power Apps + Power BI) — org standard; but the rich
  responder/section UI + live scoring are hard to rebuild in Power Fx. Pilot the MFG responder screen first.
- **B) Keep Flask UI, data in SharePoint via Graph + Entra SSO, host on Azure** — preserves the UX we built.
- **C) Interim:** keep the app, make SharePoint the governed system-of-record (auto-export data + signed PDF/Excel to a versioned library) + close the critical security holes.
> **NOTE:** a **full multi-user login + role system already exists** on branch **`fix/init-db-gunicorn`**
> (roles: admin / sbu_pic / gc_assessor; GC self-assessment → SBU validation workflow; notifications) —
> but it does NOT have the MFG/Retread work. The deployed `phase1-simple` has MFG but no login.
> **To make it a real multi-user company tool, these two branches must be MERGED.**

## 9. Suggested next tasks (priority order)
1. **Merge** `fix/init-db-gunicorn` (login/roles) with the MFG/Retread work — prerequisite for company use + auth.
2. **Security:** real login on every route; lock `/admin/backup` to admin; change admin pw; `SECRET_KEY` env var; remove creds from UI/banner; `debug=False`.
3. **Fix MFG PDF** (default empty `q["text"]`).
4. **Scope change:** New-assessment offers only Manufacturing + Retread; keep WH/Retail records as history.
5. **Lock completed assessments** + add an append-only **audit trail** (who/when/old→new).
6. **Version the question banks**; stamp each assessment with the bank version used.
7. **Automated off-site backup** (guards the monthly auto-disable + accidental overwrite).
8. Decide **M365 path A/B/C**; reconcile `main` ↔ `phase1-simple` into one branch + a one-command deploy+smoke-test.

## 10. Key files
- `app.py` — routes, `load_questions`, `load_type_meta`, `derive_answer` (rollup), `calculate_scores`
  (+ `system_items`, `system_safety`/`system_dp`), `/api/answer`, `init_db` (+ `detail` migration).
- `questions/manufacturing.json` — 233 Qs + `role_config` + `department_options` (responders, dp flags, criteria).
- `questions/retread.json` — 113 Qs. `questions/warehouse.json`, `questions/retail.json` — legacy.
- `templates/assess.html` — the assessment UI (responder panels + sections/departments JS + Live Score panel).
- `import_tbscn.py` (+ git-ignored `tbscn_responses.json`) — TBSCN result importer.
- `export/pdf_report.py`, `export/excel_report.py` — exports (PDF currently broken for MFG).

## 11. Reference artifacts (on the owner's Desktop, NOT in repo)
- `MFG_Question_to_Responder_MAP.xlsx` — question→role mapping, editable.
- `WORK/26BSANZ311_SMA_365_Migration_Proposal.pdf` — SMA→M365 proposal.
- Att-1 source Excel (Downloads) — the TBSCN MFG assessment the app data was built from.

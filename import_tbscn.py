#!/usr/bin/env python3
"""
Idempotent importer for the TBSCN (Tire mfg.) Safety Maturity Validation Assessment,
Mar-2025, from Att-1. Inserts one completed 'manufacturing' assessment + its 233
question responses into sma.db.

Run:  python3 import_tbscn.py
Data lives in the companion tbscn_responses.json (git-ignored — not committed).
Safe to re-run: it removes any prior TBSCN manufacturing record first.
"""
import json, os, sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DB_PATH = DATA_DIR / "sma.db"
RESP = json.load(open(Path(__file__).parent / "tbscn_responses.json"))

SITE = "TBSCN"
TYPE = "manufacturing"
DATE = "2025-03-24"                       # 24-26 Mar. 2025
ASSESSOR_A = "Jarinya Srimora (Safety)"
ASSESSOR_B = "Supattra Viriyaprasit (Safety); Thanida Wanamkang (DP)"

def main():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys=ON")
    # Remove any prior import (idempotent)
    old = db.execute(
        "SELECT id FROM assessments WHERE site_name=? AND type=? AND assessment_date=?",
        (SITE, TYPE, DATE)).fetchall()
    for (aid,) in old:
        db.execute("DELETE FROM responses WHERE assessment_id=?", (aid,))
        db.execute("DELETE FROM assessments WHERE id=?", (aid,))
    # Insert assessment (production schema: no 'kind' column)
    cur = db.execute(
        """INSERT INTO assessments
           (site_name, type, scope, assessor_a, assessor_b, assessment_date, status)
           VALUES (?,?,?,?,?,?,?)""",
        (SITE, TYPE, None, ASSESSOR_A, ASSESSOR_B, DATE, "done"))
    aid = cur.lastrowid
    # Insert responses
    n = 0
    for qid, ans in RESP.items():
        db.execute(
            """INSERT INTO responses (assessment_id, question_id, answer, updated_at)
               VALUES (?,?,?, datetime('now'))""",
            (aid, qid, ans))
        n += 1
    db.commit()
    print(f"Imported TBSCN assessment id={aid} with {n} responses into {DB_PATH}")

if __name__ == "__main__":
    main()

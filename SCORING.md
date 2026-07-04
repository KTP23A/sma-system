# SMA Score Calculation — Specification

> Plain-language spec of how scores are computed, so the logic is explicit (not just implicit in code)
> and defensible for audit. Source of truth = `app.py` (`calculate_scores`, `derive_answer`,
> `_element_score`) + the question banks in `questions/*.json`. Applies to all types; the MFG-specific
> parts (count-based judgement, system items, DP/Safety) are marked **[MFG]**.

## 0. Answer values
Each question resolves to one of: `yes`, `no`, `na` (not applicable), `not_rolled_out`, or unanswered (`""`).
`na` and `not_rolled_out` are **excluded** from scoring (treated as "no data").

## 1. Element score  (`_element_score`)
An **element** is a group of questions (spanning maturity levels 1–5). Algorithm:
1. Take only **applicable** questions (answer is `yes` or `no`; drop `na`/`not_rolled_out`/unanswered).
2. If none applicable → element score = **None** (shows "—").
3. Walk levels **ascending** (1→5). At the **first level that contains any `no`**, score = `max(1, level − 1)`.
4. If no level has a `no` → score = **5**.

*Meaning: you "achieve" maturity up to the level just below your first failure. A No at level 1 → 1 (floored).*

## 2. Pillar score
`pillar_score = min(element scores)` across that pillar's elements (ignoring None elements). None if all None.
The 4 pillars: **Leadership, Teammate Engagement (tm_engagement), Organization, System**.

## 3. Overall / axes  (`calculate_scores`)
```
avg(x…) = round(mean(non-None values), 2)
Safety Awareness (SA)     = avg(Leadership, Teammate)
System Implementation (SI)= avg(Organization, System)
Overall                   = avg(SA, SI)
level_name                = LEVEL_NAMES[int(Overall)]   # 1 Ad-hoc,2 Reactive,3 Standardized,4 Proactive,5 Excellence
```

---

## 4. [MFG] Count-based judgement → the question's yes/no  (`derive_answer`)
For MFG, a question is not answered with a single click; it aggregates **per-responder** input into the
`yes`/`no` that then feeds §1 unchanged. Rule = **strict 100%-Yes**:

> The question is **Yes** only if **every** required respondent answered Yes.
> **Any single No ⇒ the question is No.** Until enough responses are recorded it is **incomplete** (`""`, unanswered).
> `na` / `not_rolled_out` remain manual question-level toggles.

Each MFG question maps to exactly **one role** (from the Excel column-H "Who?"). The role's capture `mode`
(in `manufacturing.json` `role_config`) decides how its people are recorded and checked:
- **`single`** (Plant/Production GM/Maintenance/Safety/DP Manager): one Yes/No. No ⇒ question No.
- **`count`** (Supervisor default): `interviewed` / `yes` / `no` counts. Any `no`>0 ⇒ No; incomplete until `yes+no ≥ expected`.
- **`departments`** (Production Manager): up to N free-text departments, each Yes/No. Any dept No ⇒ No; incomplete if a used dept is unanswered.
- **`sections`** (Teammate, Supervisor, Foreperson, Maintenance Foreman/Staff): up to 6 named sections, each with named people (name + Yes/No). Any person No ⇒ No; incomplete if a named person has no answer or none answered.

The derived `yes`/`no` is stored in `responses.answer` (the per-person breakdown is stored in `responses.detail` JSON).
**Because the derived value lands in `answer`, §1–§3 scoring is identical for every type — MFG is not a special case to the pillar math.**

## 5. [MFG] System items (per-standard breakdown)  — informational
Every System question carries a `standard` (its subsystem, e.g. "LOTO Standard", "Work at Height"). For each
distinct `standard`, score its questions with the **same §1 element algorithm**. Displayed under System in the
Live Score panel. Does **not** change the System pillar score (which is still §2 = min of Design/Integration/Change).
23 items for MFG; validated **22/23** against the official Excel "System detail" (the 23rd is all-N/A → "—",
where the Excel showed a `#DIV/0!` and a hand-typed 1).

## 6. [MFG] DP vs Safety split — informational
Each System question has a boolean `dp` flag; each system item is **pure** Safety or pure DP.
```
System · Safety = min( element-score over Safety-only questions, per element )   # §1 per element, then min
System · DP     = min( element-score over DP-only questions,     per element )
```
Shown as two rows + a DP/S tag per item. Informational; does not alter the System pillar score.

## 7. Worked example — TBSCN (MFG, Mar-2025)
Pillars: Leadership **4**, Teammate **2**, Organization **2**, System **2**.
`SA = avg(4,2) = 3.0`, `SI = avg(2,2) = 2.0`, **Overall = avg(3,2) = 2.5 → "Reactive"**.
Matches the official Att-1 exactly. System items and Safety(2)/DP(2) validated against the official Excel.
Re-import the answers any time with `python3 import_tbscn.py`.

## 8. Where to change what
- Change the **rollup rule** (e.g. majority instead of 100%-Yes) → `derive_answer` in `app.py`.
- Change the **level-walk / floor** → `_element_score`.
- Change **which role a question is asked to**, its **level**, its **system item**, or its **DP flag**
  → `questions/manufacturing.json` (no code change needed).
- Change **role capture modes / thresholds** → `role_config` in `questions/manufacturing.json`.

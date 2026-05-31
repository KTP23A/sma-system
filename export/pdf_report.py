"""Generate PDF assessment report using ReportLab."""
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak
)

LEVEL_NAMES = {1: "Ad-hoc", 2: "Reactive", 3: "Standardized", 4: "Proactive", 5: "Excellence"}
LEVEL_COLORS = {
    1: colors.HexColor("#dc3545"),
    2: colors.HexColor("#fd7e14"),
    3: colors.HexColor("#0dcaf0"),
    4: colors.HexColor("#0d6efd"),
    5: colors.HexColor("#198754"),
}
ANSWER_COLORS = {
    "yes": colors.HexColor("#d1e7dd"),
    "no": colors.HexColor("#f8d7da"),
    "na": colors.HexColor("#e9ecef"),
    "not_rolled_out": colors.HexColor("#fff3cd"),
}
ANSWER_LABELS = {"yes": "Yes", "no": "No", "na": "N/A", "not_rolled_out": "Not rolled out", "": "—"}

W, H = A4


def score_color(score):
    return LEVEL_COLORS.get(round(score) if score else 0, colors.grey)


def generate_pdf(assessment, pillars, responses, scores, level_names, comments=None):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("body", parent=styles["Normal"], fontSize=9, leading=13)
    small_style = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=11, textColor=colors.grey)
    bold_style = ParagraphStyle("bold", parent=styles["Normal"], fontSize=9, leading=13, fontName="Helvetica-Bold")
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceBefore=8, spaceAfter=4)

    story = []

    # ── Cover ──────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 10*mm))
    story.append(Paragraph("Safety Maturity Assessment", ParagraphStyle("cover_sub", parent=styles["Normal"], fontSize=11, textColor=colors.grey)))
    story.append(Paragraph(assessment["site_name"], ParagraphStyle("cover_title", parent=styles["Heading1"], fontSize=22, spaceAfter=6)))

    atype = assessment["type"].upper()
    scope = f" — {assessment['scope'].replace('_',' ').title()}" if assessment.get("scope") and assessment["type"] == "retail" else ""
    story.append(Paragraph(f"{atype}{scope}", ParagraphStyle("cover_type", parent=styles["Normal"], fontSize=12, textColor=colors.HexColor("#6c757d"))))
    story.append(Spacer(1, 6*mm))

    meta = [
        ["Assessment Date", assessment.get("assessment_date") or "—"],
        ["Master Assessor A", assessment.get("assessor_a") or "—"],
        ["Master Assessor B", assessment.get("assessor_b") or "—"],
    ]
    meta_tbl = Table(meta, colWidths=[50*mm, 100*mm])
    meta_tbl.setStyle(TableStyle([
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("FONTNAME", (0,0), (0,-1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [colors.HexColor("#f8f9fa"), colors.white]),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dee2e6")))
    story.append(Spacer(1, 6*mm))

    # ── Score summary ──────────────────────────────────────────────────────────
    story.append(Paragraph("Score Summary", h2))

    ov = scores.get("overall")
    sa = scores.get("safety_awareness")
    si = scores.get("system_implementation")

    summary_data = [
        ["", "Score", "Level"],
        ["Overall Safety Maturity", f"{ov:.2f}" if ov else "—", level_names.get(round(ov) if ov else 0, "—")],
        ["Safety Awareness (Leadership + TM Eng.)", f"{sa:.2f}" if sa else "—", level_names.get(round(sa) if sa else 0, "—")],
        ["System Implementation (Org. + System)", f"{si:.2f}" if si else "—", level_names.get(round(si) if si else 0, "—")],
    ]
    for pillar in pillars:
        ps = scores["pillars"].get(pillar["id"], {}).get("score")
        summary_data.append([
            f"  {pillar['name']}",
            str(ps) if ps is not None else "—",
            level_names.get(ps, "—") if ps else "—"
        ])

    col_w = [105*mm, 25*mm, 40*mm]
    tbl = Table(summary_data, colWidths=col_w)
    tbl_style = [
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#212529")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("ALIGN", (1,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#f8f9fa"), colors.white]),
        ("GRID", (0,0), (-1,-1), 0.5, colors.HexColor("#dee2e6")),
        ("FONTNAME", (0,1), (0,3), "Helvetica-Bold"),
    ]
    # Color score cells
    for i, row in enumerate(summary_data[1:], 1):
        try:
            score_val = float(row[1])
            c = score_color(score_val)
            tbl_style.append(("BACKGROUND", (1,i), (2,i), c))
            tbl_style.append(("TEXTCOLOR", (1,i), (2,i), colors.white))
        except (ValueError, TypeError):
            pass

    tbl.setStyle(TableStyle(tbl_style))
    story.append(tbl)
    story.append(Spacer(1, 6*mm))

    # ── 1-point alerts ────────────────────────────────────────────────────────
    alerts = []
    for pillar in pillars:
        for element in pillar["elements"]:
            es = scores["pillars"].get(pillar["id"], {}).get("elements", {}).get(element["id"])
            if es == 1:
                alerts.append(f"{pillar['name']} → {element['name']}")

    if alerts:
        story.append(Paragraph("⚠ IMMEDIATE ACTION REQUIRED (Score = 1)", ParagraphStyle("alert_h", parent=h2, textColor=colors.HexColor("#dc3545"))))
        for a in alerts:
            story.append(Paragraph(f"• {a} — corrective measures must be taken immediately and reported to SBU.", body_style))
        story.append(Spacer(1, 4*mm))

    story.append(PageBreak())

    # ── Per-pillar detail ─────────────────────────────────────────────────────
    story.append(Paragraph("Detailed Results by Pillar", h1))

    for pillar in pillars:
        ps_data = scores["pillars"].get(pillar["id"], {})
        ps = ps_data.get("score")
        lv_name = level_names.get(ps, "—") if ps else "—"
        lv_color = LEVEL_COLORS.get(ps, colors.grey) if ps else colors.grey

        story.append(Spacer(1, 4*mm))
        pillar_header = Table(
            [[Paragraph(pillar["name"], ParagraphStyle("ph", parent=styles["Normal"], fontSize=12, fontName="Helvetica-Bold", textColor=colors.white)),
              Paragraph(f"{ps} — {lv_name}" if ps else "—", ParagraphStyle("ps", parent=styles["Normal"], fontSize=11, fontName="Helvetica-Bold", textColor=colors.white, alignment=2))]],
            colWidths=[120*mm, 50*mm]
        )
        pillar_header.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#212529")),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING", (0,0), (0,-1), 8),
            ("RIGHTPADDING", (-1,0), (-1,-1), 8),
        ]))
        story.append(pillar_header)

        for element in pillar["elements"]:
            es = ps_data.get("elements", {}).get(element["id"])
            es_color = LEVEL_COLORS.get(es, colors.grey) if es is not None else colors.grey

            el_row = Table(
                [[Paragraph(element["name"], ParagraphStyle("en", parent=styles["Normal"], fontSize=10, fontName="Helvetica-Bold")),
                  Paragraph(f"Score: {es} ({level_names.get(es,'N/A')})" if es is not None else "Score: N/A",
                            ParagraphStyle("es", parent=styles["Normal"], fontSize=9, textColor=colors.white, alignment=2))]],
                colWidths=[120*mm, 50*mm]
            )
            el_style = [
                ("BACKGROUND", (0,0), (0,0), colors.HexColor("#e9ecef")),
                ("BACKGROUND", (1,0), (1,0), es_color),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                ("TOPPADDING", (0,0), (-1,-1), 4),
                ("LEFTPADDING", (0,0), (0,-1), 8),
                ("RIGHTPADDING", (-1,0), (-1,-1), 8),
            ]
            el_row.setStyle(TableStyle(el_style))
            story.append(el_row)

            # Questions
            q_data = [["No", "Question", "Answer", "Comment"]]
            q_styles = [
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f8f9fa")),
                ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE", (0,0), (-1,-1), 8),
                ("VALIGN", (0,0), (-1,-1), "TOP"),
                ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#dee2e6")),
                ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                ("TOPPADDING", (0,0), (-1,-1), 3),
            ]
            for i, q in enumerate(element["questions"], 1):
                ans = responses.get(q["id"], "")
                ans_label = ANSWER_LABELS.get(ans, "—")
                cmt = (comments or {}).get(q["id"], "") or ""
                q_data.append([
                    str(q["no"]),
                    Paragraph(q["text"], ParagraphStyle("qt", parent=styles["Normal"], fontSize=8, leading=11)),
                    ans_label,
                    Paragraph(cmt, ParagraphStyle("qc", parent=styles["Normal"], fontSize=7, leading=10, textColor=colors.HexColor("#555"))),
                ])
                if ans in ANSWER_COLORS:
                    q_styles.append(("BACKGROUND", (2, i), (2, i), ANSWER_COLORS[ans]))

            q_tbl = Table(q_data, colWidths=[10*mm, 100*mm, 20*mm, 40*mm])
            q_tbl.setStyle(TableStyle(q_styles))
            story.append(q_tbl)
            story.append(Spacer(1, 2*mm))

    doc.build(story)
    return buf.getvalue()

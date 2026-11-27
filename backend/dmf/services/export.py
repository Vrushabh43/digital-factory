"""CSV and PDF exports of cell passes (generated in the backend)."""

from __future__ import annotations

import codecs
import csv
import io
from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

RESULT_LABELS = {
    "inside": "Still inside",
    "passed": "Passed",
    "to_robot": "Passed to robot",
    "removed": "Taken off the line",
    "stored": "Went into storage",
    "lost": "Lost track",
}
PDF_MAX_ROWS = 5000
PDF_ROW_HEIGHT = 15.6  # pt: one line of 8 pt Helvetica plus reportlab's default 3 pt padding


def result_label(row: dict[str, Any], stuck_now: bool = False) -> str:
    if row["result"] == "inside":
        return "Stuck at exit" if stuck_now else "Still inside"
    label = RESULT_LABELS.get(row["result"], row["result"])
    return f"{label} (was stuck)" if row.get("was_stuck") else label


def fmt_dt(dt: datetime | None, tz: ZoneInfo, with_date: bool = True) -> str:
    if dt is None:
        return ""
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S" if with_date else "%H:%M:%S")


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = max(0, int(round(seconds)))  # a clock step can make "left" earlier than "entered"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h} h {m:02d} min {sec:02d} s" if h else f"{m} min {sec:02d} s"


def _duration(row: dict[str, Any], now: datetime) -> float:
    end = row["left_at"] or now
    return max(0.0, (end - row["entered_at"]).total_seconds())


FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """Defuse spreadsheet formulas (CSV injection): a cell starting with = + - @ is shown
    as text by Excel/LibreOffice when prefixed with an apostrophe. All values are generated
    by the backend today; this is defence in depth."""
    return "'" + value if value.startswith(FORMULA_START) else value


HEADER = [
    "Piece",
    "Cell",
    "Position",
    "Entered",
    "Left",
    "Time in cell",
    "Time in cell (s)",
    "Result",
    "Came from cell",
    "Went to position",
    "Uncertain",
]


def _row(r: dict[str, Any], tz: ZoneInfo, now: datetime, stuck: set[str]) -> list[str]:
    secs = _duration(r, now)
    return [
        r["piece"],
        str(r["cell_number"]),
        str(r["position"]),
        fmt_dt(r["entered_at"], tz),
        fmt_dt(r["left_at"], tz),
        fmt_duration(secs) + (" so far" if r["left_at"] is None else ""),
        str(int(round(secs))),
        result_label(r, r["piece"] in stuck and r["left_at"] is None),
        "" if r["came_from_cell"] is None else str(r["came_from_cell"]),
        "" if r["went_to_position"] is None else str(r["went_to_position"]),
        "yes" if r["uncertain"] else "",
    ]


def to_csv(rows: list[dict[str, Any]], tz_name: str, now: datetime, stuck: set[str]) -> bytes:
    tz = ZoneInfo(tz_name)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow([csv_safe(v) for v in _row(r, tz, now, stuck)])
    return codecs.BOM_UTF8 + buf.getvalue().encode("utf-8")  # BOM: Excel opens UTF-8 correctly


def to_pdf(rows: list[dict[str, Any]], tz_name: str, now: datetime, stuck: set[str], title: str, subtitle: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    tz = ZoneInfo(tz_name)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=title,
        author="DMF Tracking Dashboard",
    )
    styles = getSampleStyleSheet()
    results = Counter(result_label(r, r["piece"] in stuck and r["left_at"] is None) for r in rows)
    story = [
        Paragraph(title, styles["Title"]),
        Paragraph(subtitle, styles["Normal"]),
        Paragraph(
            f"Generated {now.astimezone(tz).strftime('%d.%m.%Y %H:%M:%S')} · {len(rows)} cell passes · "
            f"{len({r['piece'] for r in rows})} pieces · " + ", ".join(f"{k}: {v}" for k, v in results.most_common()),
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
    ]
    shown = rows[:PDF_MAX_ROWS]
    cols = [0, 1, 2, 3, 4, 5, 7, 8, 10]
    data = [[HEADER[i] for i in cols]]
    for r in shown:
        full = _row(r, tz, now, stuck)  # once per row (was once per column)
        data.append([full[i] for i in cols])
    # Every cell is one line of 8 pt text, which reportlab sizes to 15.6 pt. Giving the heights up front
    # keeps that layout and avoids re-measuring all remaining rows at each page split (quadratic).
    table = Table(
        data,
        repeatRows=1,
        colWidths=[22 * mm, 13 * mm, 18 * mm, 38 * mm, 38 * mm, 32 * mm, 48 * mm, 26 * mm, 20 * mm],
        rowHeights=[PDF_ROW_HEIGHT] * len(data),
    )
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(table)
    if len(rows) > PDF_MAX_ROWS:
        story.append(Spacer(1, 4 * mm))
        story.append(
            Paragraph(
                f"Only the first {PDF_MAX_ROWS} of {len(rows)} passes are shown. " "Use the CSV export for the complete list.",
                styles["Italic"],
            )
        )
    doc.build(story)
    return buf.getvalue()

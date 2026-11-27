"""CSV / PDF export of cell passes: every cell of a known winter and summer pass."""

from __future__ import annotations

import base64
import codecs
import csv
import io
import re
import zlib
from datetime import UTC, datetime, timedelta

from dmf.services import export

NOW = datetime(2026, 11, 26, 9, 0, 0, tzinfo=UTC)  # 10:00:00 in Berlin (CET)

HEADER_LINE = (
    "Piece,Cell,Position,Entered,Left,Time in cell,Time in cell (s),Result,Came from cell,Went to position,Uncertain\r\n"
)


def _pass(**kw) -> dict:
    row = {
        "id": 1,
        "piece": "P-0001",
        "position": 3,
        "cell_number": 12,
        "entered_at": None,
        "left_at": None,
        "result": "passed",
        "was_stuck": False,
        "stuck_since": None,
        "came_from_position": None,
        "came_from_cell": None,
        "went_to_position": None,
        "via": None,
        "first_conveyor": 1,
        "last_conveyor": 4,
        "uncertain": False,
    }
    row.update(kw)
    return row


# winter (CET = UTC+1): position 3 = cell 12, came from position 2 = cell 11
WINTER = _pass(
    piece="P-0001",
    position=3,
    cell_number=12,
    entered_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
    left_at=datetime(2026, 1, 15, 10, 1, 5, tzinfo=UTC),
    came_from_position=2,
    came_from_cell=11,
    went_to_position=4,
)
# summer (CEST = UTC+2), crosses midnight in Berlin: position 7 = cell 4, came from position 18 = cell 3
SUMMER = _pass(
    piece="P-0002",
    position=7,
    cell_number=4,
    entered_at=datetime(2026, 7, 1, 22, 30, 0, tzinfo=UTC),
    left_at=datetime(2026, 7, 1, 23, 45, 30, tzinfo=UTC),
    result="passed",
    was_stuck=True,
    stuck_since=datetime(2026, 7, 1, 22, 31, 0, tzinfo=UTC),
    came_from_position=18,
    came_from_cell=3,
    went_to_position=8,
    uncertain=True,
)
# still inside (open pass): position 11 = cell 15, waiting for 125 s
INSIDE = _pass(piece="P-0003", position=11, cell_number=15, entered_at=NOW - timedelta(seconds=125), result="inside")
ROBOT = _pass(
    piece="P-0004",
    position=11,
    cell_number=15,
    entered_at=datetime(2026, 3, 29, 0, 59, 59, tzinfo=UTC),  # 01:59:59 CET, one second before the clocks change
    left_at=datetime(2026, 3, 29, 1, 0, 1, tzinfo=UTC),  # 03:00:01 CEST
    result="to_robot",
    came_from_position=5,
    came_from_cell=13,
    went_to_position=12,
)


def test_csv_header_bom_and_every_cell_of_known_passes():
    body = export.to_csv([WINTER, SUMMER, INSIDE, ROBOT], "Europe/Berlin", NOW, stuck={"P-0003"})
    assert body.startswith(codecs.BOM_UTF8) and body.count(codecs.BOM_UTF8) == 1
    text = body[len(codecs.BOM_UTF8) :].decode("utf-8")
    assert text == (
        HEADER_LINE
        + "P-0001,12,3,15.01.2026 11:00:00,15.01.2026 11:01:05,1 min 05 s,65,Passed,11,4,\r\n"
        + "P-0002,4,7,02.07.2026 00:30:00,02.07.2026 01:45:30,1 h 15 min 30 s,4530,Passed (was stuck),3,8,yes\r\n"
        + "P-0003,15,11,26.11.2026 09:57:55,,2 min 05 s so far,125,Stuck at exit,,,\r\n"
        + "P-0004,15,11,29.03.2026 01:59:59,29.03.2026 03:00:01,0 min 02 s,2,Passed to robot,13,12,\r\n"
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == export.HEADER and all(len(r) == len(export.HEADER) for r in rows)


def test_csv_open_pass_that_is_not_stuck_is_still_inside():
    body = export.to_csv([INSIDE], "Europe/Berlin", NOW, stuck=set())
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert rows[1][7] == "Still inside" and rows[1][4] == "" and rows[1][5] == "2 min 05 s so far"


def test_csv_quotes_commas_and_quotes():
    odd = _pass(**{**WINTER, "result": 'odd, "quoted" value'})  # unknown result: shown as stored
    body = export.to_csv([odd], "Europe/Berlin", NOW, stuck=set())
    line = body.decode("utf-8-sig").split("\r\n")[1]
    assert line == 'P-0001,12,3,15.01.2026 11:00:00,15.01.2026 11:01:05,1 min 05 s,65,"odd, ""quoted"" value",11,4,'
    (_, row) = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    assert row[7] == 'odd, "quoted" value'


def test_csv_of_an_empty_range_is_the_header_only():
    assert export.to_csv([], "Europe/Berlin", NOW, stuck=set()) == codecs.BOM_UTF8 + HEADER_LINE.encode("utf-8")


def test_csv_uses_the_configured_timezone():
    body = export.to_csv([WINTER], "UTC", NOW, stuck=set())
    assert "15.01.2026 10:00:00,15.01.2026 10:01:05" in body.decode("utf-8-sig")


def test_result_labels():
    assert export.result_label(_pass(result="passed", was_stuck=True)) == "Passed (was stuck)"
    assert export.result_label(_pass(result="passed")) == "Passed"
    assert export.result_label(_pass(result="removed", was_stuck=True)) == "Taken off the line (was stuck)"
    assert export.result_label(_pass(result="inside"), stuck_now=True) == "Stuck at exit"
    assert export.result_label(_pass(result="inside", was_stuck=True)) == "Still inside"
    assert {k: export.result_label(_pass(result=k)) for k in export.RESULT_LABELS} == export.RESULT_LABELS


# ------------------------------------------------------------------ PDF
def pdf_text(data: bytes) -> str:
    """All text strings of a reportlab PDF (content streams are ASCII85 + Flate encoded)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = m.group(1).strip()
        try:
            if raw.endswith(b"~>"):
                raw = base64.a85decode(raw[:-2].removeprefix(b"<~"))
            raw = zlib.decompress(raw)
        except (ValueError, zlib.error):
            continue
        for s in re.findall(rb"\(((?:\\.|[^\\)])*)\)", raw):
            s = re.sub(rb"\\([0-7]{1,3})", lambda o: bytes([int(o.group(1), 8)]), s)  # e.g. \267 = middle dot
            out.append(re.sub(rb"\\(.)", rb"\1", s).decode("cp1252"))
    return "\n".join(out)


def test_pdf_contains_the_passes_in_plant_time():
    pdf = export.to_pdf([WINTER, SUMMER, INSIDE], "Europe/Berlin", NOW, {"P-0003"}, "Digital Model Factory", "all cells")
    assert pdf.startswith(b"%PDF") and pdf.rstrip().endswith(b"%%EOF")
    text = pdf_text(pdf)
    for expected in (
        "Digital Model Factory",
        "Generated 26.11.2026 10:00:00",
        "Generated 26.11.2026 10:00:00 · 3 cell passes · 3 pieces · ",
        "15.01.2026 11:00:00",
        "02.07.2026 01:45:30",
        "1 h 15 min 30 s",
        "Passed (was stuck)",
        "Stuck at exit",
        "Came from cell",
    ):
        assert expected in text, expected
    assert "Only the first" not in text


def test_pdf_above_the_row_limit_says_it_is_truncated():
    limit = export.PDF_MAX_ROWS
    rows = [_pass(**{**WINTER, "piece": f"P-{i:04d}"}) for i in range(1, limit + 2)]
    text = pdf_text(export.to_pdf(rows, "Europe/Berlin", NOW, set(), "T", "S"))
    assert f"Only the first {limit} of {limit + 1} passes are shown." in text
    assert f"P-{limit:04d}" in text and f"P-{limit + 1:04d}" not in text
    assert f"{limit + 1} cell passes" in text  # the summary counts all of them


def test_pdf_at_the_row_limit_is_complete(monkeypatch):
    monkeypatch.setattr(export, "PDF_MAX_ROWS", 3)
    rows = [_pass(**{**WINTER, "piece": f"P-{i:04d}"}) for i in range(1, 4)]
    text = pdf_text(export.to_pdf(rows, "Europe/Berlin", NOW, set(), "T", "S"))
    assert "Only the first" not in text and "P-0003" in text
    text = pdf_text(export.to_pdf(rows + [_pass(**{**WINTER, "piece": "P-0004"})], "Europe/Berlin", NOW, set(), "T", "S"))
    assert "Only the first 3 of 4 passes are shown." in text and "P-0004" not in text

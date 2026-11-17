"""Word (.docx) and Excel (.xlsx) files, and the data-analysis engine behind the Analyze page.

Pure Python (python-docx, openpyxl): no AI, no network. Everything a chat answer or an analysis needs is
written to a file on this server.

* build_docx / build_xlsx turn a Markdown answer into a document: headings, lists, bold, code and tables
  (every Markdown table becomes its own Excel sheet, with numbers and dates stored as real numbers and dates).
* profile_rows measures a table of records (column types, missing values, duplicates, statistics, outliers,
  top values, group totals, trends) and writes plain-language insights.
* analysis_markdown / build_analysis_xlsx turn that profile into a report (Word / PDF through Markdown, Excel
  with summary, column and chart sheets plus the data itself).
"""
from __future__ import annotations

import datetime as dt
import io
import math
import re
import statistics
from collections import Counter
from typing import Any, Iterable

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_DATA_ROWS = 50_000   # rows copied into the "Data" sheet of an analysis workbook
MAX_COLUMNS = 200


# ================================================================== Markdown parsing
_TABLE_SEP = re.compile(r"^:?-{2,}:?$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|(?<!\*)\*[^*\n]+\*(?!\*)|\[[^\]]+\]\([^)]+\))")


def inline_runs(text: str) -> list[tuple[str, dict]]:
    """'a **b** `c`' -> [('a ', {}), ('b', {'bold': True}), (' ', {}), ('c', {'code': True})]."""
    out: list[tuple[str, dict]] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], {}))
        tok = m.group(0)
        if tok.startswith("**"):
            out.append((tok[2:-2], {"bold": True}))
        elif tok.startswith("`"):
            out.append((tok[1:-1], {"code": True}))
        elif tok.startswith("["):
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok).groups()
            out.append((f"{label} ({url})", {}))
        else:
            out.append((tok[1:-1], {"italic": True}))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], {}))
    return out


def plain(text: str) -> str:
    return "".join(t for t, _ in inline_runs(text)).strip()


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_table(lines: list[str]) -> list[list[str]]:
    rows = []
    for ln in lines:
        cells = _split_row(ln)
        if all(_TABLE_SEP.match(c) for c in cells if c) and any(cells):
            continue
        rows.append([plain(c) for c in cells])
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def parse_blocks(body: str) -> list[tuple]:
    """Markdown -> ('h', level, text) ('p', text) ('ul', [..]) ('ol', [..]) ('table', rows) ('code', text) ('hr',)."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    blocks: list[tuple] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            blocks.append(("p", " ".join(s.strip() for s in para)))
            para.clear()

    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            flush()
            code, i = [], i + 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(code)))
            i += 1
            continue
        if not s:
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush()
            blocks.append(("h", min(len(m.group(1)), 3), plain(m.group(2))))
            i += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", s):
            flush()
            blocks.append(("hr",))
            i += 1
            continue
        if s.startswith("|") and s.count("|") >= 2:
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            table = _parse_table(rows)
            if table:
                blocks.append(("table", table))
            continue
        ul, ol = re.match(r"^[-*•]\s+(.*)$", s), re.match(r"^\d+[.)]\s+(.*)$", s)
        if ul or ol:
            flush()
            kind, pat = ("ul", r"^[-*•]\s+(.*)$") if ul else ("ol", r"^\d+[.)]\s+(.*)$")
            items = []
            while i < len(lines):
                mm = re.match(pat, lines[i].strip())
                if not mm:
                    break
                items.append(mm.group(1))
                i += 1
            blocks.append((kind, items))
            continue
        para.append(s)
        i += 1
    flush()
    return blocks


# ================================================================== numbers, dates, cells
_NUM = re.compile(r"^[-+]?(?:[₹$€£]|Rs\.?|INR|USD)?\s*\d[\d,]*(?:\.\d+)?\s*%?$|^[-+]?\.\d+$", re.I)
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                 "%d-%b-%Y", "%d %b %Y")


def to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        t = v.strip()
        if _NUM.fullmatch(t):
            core = re.sub(r"[^\d.\-]", "", t.replace(",", ""))
            try:
                return float(core)
            except ValueError:
                return None
    return None


def to_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str) and re.search(r"\d", v) and not _NUM.fullmatch(v.strip()):
        t = v.strip()
        for f in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(t, f).date()
            except ValueError:
                continue
    return None


def to_cell(value: Any) -> tuple[Any, str | None]:
    """A text cell -> (python value, Excel number format). Numbers and dates become real numbers and dates;
    IDs and phone numbers (leading zeros, 10+ digits) stay text."""
    if not isinstance(value, str):
        return value, None
    t = value.strip()
    if t == "":
        return None, None
    if _NUM.fullmatch(t):
        core = re.sub(r"[^\d.\-]", "", t.replace(",", ""))
        digits = core.lstrip("-").split(".")[0]
        if "." not in core and (len(digits) >= 10 or (len(digits) > 1 and digits.startswith("0"))):
            return t, None
        try:
            v = float(core)
        except ValueError:
            return t, None
        if t.endswith("%"):
            return v / 100, "0.0%"
        if "." not in core:
            return int(v), ("#,##0" if abs(v) >= 1000 else None)
        return v, "#,##0.00"
    d = to_date(t) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t) else None
    if d:
        return d, "yyyy-mm-dd"
    return t, None


def fmt_num(x: float) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return ""
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s or "0"


# ================================================================== Word
def _hex(accent: str | None) -> str:
    h = (accent or "#2563eb").lstrip("#")
    return h if re.fullmatch(r"[0-9a-fA-F]{6}", h) else "2563eb"


def _shade(cell, fill: str) -> None:
    pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def _set_fonts(style, latin: str, complex_script: str) -> None:
    style.font.name = latin
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for attr, name in (("w:ascii", latin), ("w:hAnsi", latin), ("w:cs", complex_script), ("w:eastAsia", latin)):
        fonts.set(qn(attr), name)


def _add_runs(paragraph, text: str, size: float | None = None, color: RGBColor | None = None, bold: bool = False) -> None:
    for t, fmt in inline_runs(text):
        r = paragraph.add_run(t)
        r.bold = bold or fmt.get("bold", False)
        r.italic = fmt.get("italic", False)
        if fmt.get("code"):
            r.font.name = "Consolas"
        if size:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color


def build_docx(title: str, body: str, citations: list[dict] | None = None, subtitle: str = "",
               accent: str = "#2563eb") -> bytes:
    """A Word document from a Markdown answer. Hindi text works (complex-script font is set)."""
    acc = _hex(accent)
    rgb = RGBColor.from_string(acc.upper())
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    normal = doc.styles["Normal"]
    _set_fonts(normal, "Calibri", "Nirmala UI")
    normal.font.size = Pt(11)
    for name in ("Title", "Heading 1", "Heading 2", "Heading 3"):
        st = doc.styles[name]
        _set_fonts(st, "Calibri", "Nirmala UI")
        st.font.color.rgb = rgb
    doc.core_properties.title = (title or "Document")[:150]

    doc.add_heading(title or "Document", level=0)
    if subtitle:
        p = doc.add_paragraph()
        _add_runs(p, subtitle, size=11, color=RGBColor(0x66, 0x66, 0x66))

    for block in parse_blocks(body):
        kind = block[0]
        if kind == "h":
            doc.add_heading(block[2], level=block[1])
        elif kind == "p":
            _add_runs(doc.add_paragraph(), block[1])
        elif kind in ("ul", "ol"):
            style = "List Bullet" if kind == "ul" else "List Number"
            for item in block[1]:
                _add_runs(doc.add_paragraph(style=style), item)
        elif kind == "code":
            p = doc.add_paragraph()
            r = p.add_run(block[1])
            r.font.name, r.font.size = "Consolas", Pt(9)
            p.paragraph_format.left_indent = Cm(0.4)
        elif kind == "hr":
            doc.add_paragraph("─" * 40).alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif kind == "table":
            rows = block[1]
            width = len(rows[0])
            table = doc.add_table(rows=len(rows), cols=width)
            table.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci, text in enumerate(row):
                    cell = table.cell(ri, ci)
                    p = cell.paragraphs[0]
                    header = ri == 0
                    _add_runs(p, text, size=10, color=RGBColor(255, 255, 255) if header else None, bold=header)
                    if header:
                        _shade(cell, acc)
                    elif ri % 2 == 0:
                        _shade(cell, "F3F4F6")
                    if not header and to_number(text) is not None:
                        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            doc.add_paragraph()
    if citations:
        doc.add_heading("Sources", level=2)
        for i, c in enumerate(citations, 1):
            label = c.get("title") or c.get("source") or c.get("doc_id") or ""
            doc.add_paragraph(f"[{i}] {label}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ================================================================== Excel
_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _set_cell(cell, value: Any, number_format: str | None = None) -> None:
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"  # data is data: never let a record turn into an Excel formula
    if number_format:
        cell.number_format = number_format


def _sheet_name(candidate: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", candidate or "").strip()[:28] or "Sheet"
    name, n = base, 2
    while name.lower() in used:
        name = f"{base[:26]} {n}"
        n += 1
    used.add(name.lower())
    return name


def _style_header(ws, width: int, accent: str, row: int = 1) -> None:
    fill = PatternFill("solid", fgColor=accent)
    for c in range(1, width + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = _BORDER


def _fit_columns(ws, sample_rows: int = 200) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, sample_rows)):
        for cell in row:
            if cell.value is not None:
                widths[cell.column] = max(widths.get(cell.column, 0), len(str(cell.value)))
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = max(8, min(50, w + 2))


def write_table(ws, rows: list[list[Any]], accent: str, convert: bool = True) -> None:
    for ri, row in enumerate(rows, 1):
        for ci, value in enumerate(row, 1):
            if ri == 1:
                _set_cell(ws.cell(row=ri, column=ci), "" if value is None else str(value))
            else:
                v, nf = to_cell(value) if convert else (value, None)
                cell = ws.cell(row=ri, column=ci)
                _set_cell(cell, v, nf)
                if ri > 1 and isinstance(v, (int, float, dt.date)):
                    cell.alignment = Alignment(horizontal="right")
    if rows:
        _style_header(ws, len(rows[0]), accent)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(rows[0]))}{len(rows)}"
        _fit_columns(ws)


def build_xlsx(title: str, body: str, citations: list[dict] | None = None, subtitle: str = "",
               accent: str = "#2563eb") -> bytes:
    """An Excel workbook from a Markdown answer: each table becomes a sheet (header row, filter, frozen header,
    real numbers and dates). Text without tables goes to a 'Content' sheet."""
    acc = _hex(accent)
    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title = (title or "Workbook")[:150]
    used: set[str] = set()
    heading, n, text_lines = "", 0, []
    for block in parse_blocks(body):
        if block[0] == "h":
            heading = block[2]
            text_lines.append((block[2], True))
        elif block[0] == "table":
            n += 1
            ws = wb.create_sheet(_sheet_name(heading or f"Table {n}", used))
            write_table(ws, block[1], acc)
        elif block[0] == "p":
            text_lines.append((plain(block[1]), False))
        elif block[0] in ("ul", "ol"):
            text_lines.extend((f"• {plain(i)}", False) for i in block[1])
    if not wb.worksheets or (text_lines and not n):
        ws = wb.create_sheet(_sheet_name("Content", used), 0)
        ws.column_dimensions["A"].width = 110
        ws.cell(row=1, column=1, value=title or "Content").font = Font(bold=True, size=14, color=acc)
        for r, (text, is_head) in enumerate(text_lines, 3):
            c = ws.cell(row=r, column=1)
            _set_cell(c, text)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            if is_head:
                c.font = Font(bold=True, color=acc)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ================================================================== data profile
def _col_order(rows: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            if k not in seen:
                seen[k] = None
                if len(seen) >= MAX_COLUMNS:
                    return list(seen)
    return list(seen)


_BOOL = {"true", "false", "yes", "no", "y", "n"}
_ID_NAME = re.compile(r"(^|[\s_\-])(id|no|number|code|sr|sno|serial|uuid)($|[\s_\-])", re.I)
_VALUE_NAME = re.compile(r"amount|salary|price|total|revenue|cost|sales|value|income|profit|fee|budget|ctc|qty|quantity", re.I)


def _round(x: float | None, d: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, d)


def _numeric_stats(vals: list[float]) -> dict:
    n = len(vals)
    s = sorted(vals)
    out = {"count": n, "sum": _round(sum(vals)), "mean": _round(statistics.fmean(vals)), "median": _round(statistics.median(vals)),
           "min": _round(s[0]), "max": _round(s[-1]), "std": _round(statistics.stdev(vals)) if n > 1 else 0.0}
    if n >= 4:
        q1, _, q3 = statistics.quantiles(s, n=4, method="inclusive")
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        far_lo, far_hi = q1 - 3 * iqr, q3 + 3 * iqr
        out.update(q1=_round(q1), q3=_round(q3), outliers=sum(1 for v in vals if v < lo or v > hi),
                   low_fence=_round(lo), high_fence=_round(hi),
                   extreme=sum(1 for v in vals if iqr > 0 and (v < far_lo or v > far_hi)))
    else:
        out.update(outliers=0)
    lo_v, hi_v = s[0], s[-1]
    bins = 8 if hi_v > lo_v else 1
    width = (hi_v - lo_v) / bins if bins > 1 else 1
    counts = [0] * bins
    for v in vals:
        counts[min(bins - 1, int((v - lo_v) / width)) if bins > 1 else 0] += 1
    out["histogram"] = [{"from": _round(lo_v + i * width), "to": _round(lo_v + (i + 1) * width), "count": c}
                        for i, c in enumerate(counts)]
    return out


def _infer(name: str, values: list[Any]) -> str:
    n = len(values)
    if n == 0:
        return "empty"
    if all(isinstance(v, bool) or (isinstance(v, str) and v.strip().lower() in _BOOL) for v in values):
        return "bool"
    nums = sum(1 for v in values if to_number(v) is not None)
    if nums / n >= 0.9:
        ints = all(float(to_number(v)).is_integer() for v in values if to_number(v) is not None)
        if ints and _ID_NAME.search(name or "") and len({str(v) for v in values}) / n > 0.9:
            return "id"
        return "number"
    dates = sum(1 for v in values if to_date(v) is not None)
    if dates / n >= 0.9:
        return "date"
    if n >= 20 and len({str(v) for v in values}) / n > 0.98:
        return "id"
    return "text"


def profile_rows(rows: list[dict], truncated: bool = False) -> dict:
    """Measures a table of records (list of dicts)."""
    headers = _col_order(rows)
    n_rows = len(rows)
    columns, missing_total = [], 0
    series: dict[str, list[Any]] = {}
    for h in headers:
        vals = [r.get(h) for r in rows]
        present = [v for v in vals if v is not None and not (isinstance(v, str) and not v.strip())]
        series[h] = vals
        missing = n_rows - len(present)
        missing_total += missing
        kind = _infer(h, present)
        col: dict[str, Any] = {"name": h, "type": kind, "filled": len(present), "missing": missing,
                               "missing_pct": _round(100 * missing / n_rows, 1) if n_rows else 0.0,
                               "unique": len({str(v) for v in present})}
        if kind == "number":
            nums = [x for x in (to_number(v) for v in present) if x is not None]
            col["stats"] = _numeric_stats(nums)
        elif kind == "date":
            ds = sorted(d for d in (to_date(v) for v in present) if d)
            months = Counter(d.strftime("%Y-%m") for d in ds)
            col["range"] = {"from": ds[0].isoformat(), "to": ds[-1].isoformat(), "days": (ds[-1] - ds[0]).days}
            col["by_month"] = [{"month": m, "count": c} for m, c in sorted(months.items())[-24:]]
        elif kind in ("text", "bool"):
            top = Counter(str(v).strip() for v in present).most_common(8)
            col["top"] = [{"value": v[:80], "count": c, "pct": _round(100 * c / len(present), 1)} for v, c in top]
        columns.append(col)

    dup = 0
    if n_rows:
        counts = Counter(tuple(str(r.get(h, "")) for h in headers) for r in rows)
        dup = sum(c - 1 for c in counts.values() if c > 1)

    profile = {"rows": n_rows, "columns": columns, "column_count": len(headers), "duplicates": dup, "truncated": truncated,
               "missing_cells": missing_total,
               "missing_pct": _round(100 * missing_total / (n_rows * len(headers)), 1) if n_rows and headers else 0.0}
    profile["group"] = _group_summary(rows, columns)
    profile["correlations"] = _correlations(rows, columns)
    profile["insights"] = _insights(profile)
    return profile


def _group_summary(rows: list[dict], columns: list[dict]) -> dict | None:
    cats = [c for c in columns if c["type"] == "text" and 2 <= c["unique"] <= 30 and c["filled"] >= 2]
    if not cats:
        return None
    cat = min(cats, key=lambda c: c["unique"])
    nums = [c for c in columns if c["type"] == "number"]
    value = next((c for c in nums if _VALUE_NAME.search(c["name"])), nums[0] if nums else None)
    groups: dict[str, list[float]] = {}
    counts: Counter = Counter()
    for r in rows:
        k = r.get(cat["name"])
        if k is None or (isinstance(k, str) and not k.strip()):
            continue
        k = str(k).strip()[:60]
        counts[k] += 1
        if value:
            x = to_number(r.get(value["name"]))
            if x is not None:
                groups.setdefault(k, []).append(x)
    items = []
    for k, c in counts.items():
        g = groups.get(k, [])
        items.append({"key": k, "count": c, "sum": _round(sum(g)) if g else None, "mean": _round(statistics.fmean(g)) if g else None})
    items.sort(key=lambda i: (i["sum"] is None, -(i["sum"] or 0), -i["count"]) if value else (-i["count"],))
    return {"by": cat["name"], "value": value["name"] if value else None, "rows": items[:15]}


def _correlations(rows: list[dict], columns: list[dict]) -> list[dict]:
    nums = [c["name"] for c in columns if c["type"] == "number"][:12]
    out = []
    for i, a in enumerate(nums):
        for b in nums[i + 1:]:
            xs, ys = [], []
            for r in rows:
                x, y = to_number(r.get(a)), to_number(r.get(b))
                if x is not None and y is not None:
                    xs.append(x)
                    ys.append(y)
            if len(xs) >= 10 and len(set(xs)) > 1 and len(set(ys)) > 1:
                try:
                    r_ = statistics.correlation(xs, ys)
                except statistics.StatisticsError:
                    continue
                if abs(r_) >= 0.5:
                    out.append({"a": a, "b": b, "r": _round(r_, 2)})
    out.sort(key=lambda c: -abs(c["r"]))
    return out[:6]


def _insights(p: dict) -> list[str]:
    out = [f"The data has {p['rows']:,} rows and {p['column_count']} columns."]
    if p.get("truncated"):
        out.append("Only the first rows were analysed because the file is very large.")
    if p["missing_pct"] >= 1:
        out.append(f"{p['missing_pct']}% of all cells are empty.")
    for c in p["columns"]:
        if c["missing_pct"] >= 5 and c["type"] != "empty":
            out.append(f"Column “{c['name']}” is empty in {c['missing_pct']}% of rows.")
    if p["duplicates"]:
        out.append(f"{p['duplicates']:,} {'row is an exact duplicate' if p['duplicates'] == 1 else 'rows are exact duplicates'} of another row.")
    for c in p["columns"]:
        st = c.get("stats")
        if st and st.get("extreme") and st["count"] >= 10:  # a far-out value matters even if it is only one row
            far = st["max"] if st["max"] > st["high_fence"] else st["min"]
            out.append(f"“{c['name']}” has {st['extreme']} extreme value{'s' if st['extreme'] > 1 else ''} "
                       f"(for example {fmt_num(far)}, while the middle value is {fmt_num(st['median'])}). Check if {'they are' if st['extreme'] > 1 else 'it is'} correct.")
        elif st and st.get("outliers") and st["count"] >= 10 and st["outliers"] / st["count"] >= 0.005:
            out.append(f"“{c['name']}” has {st['outliers']} unusual values (outside {fmt_num(st['low_fence'])} – {fmt_num(st['high_fence'])}).")
        if st and st["count"] >= 10 and st["median"] and st["mean"] and not (0.67 <= st["mean"] / st["median"] <= 1.5):
            out.append(f"“{c['name']}” is skewed: average {fmt_num(st['mean'])} but median {fmt_num(st['median'])}.")
        if c["type"] == "text" and c.get("top") and c["unique"] >= 2 and c["top"][0]["pct"] >= 50 and c["filled"] >= 10:
            out.append(f"“{c['name']}”: “{c['top'][0]['value']}” makes up {c['top'][0]['pct']}% of rows.")
        if c["type"] == "date":
            out.append(f"“{c['name']}” runs from {c['range']['from']} to {c['range']['to']}.")
    g = p.get("group")
    if g and g["rows"]:
        top = g["rows"][0]
        if g["value"] and top["sum"] is not None:
            out.append(f"Highest total “{g['value']}” by “{g['by']}”: {top['key']} ({fmt_num(top['sum'])}).")
        else:
            out.append(f"Most rows by “{g['by']}”: {top['key']} ({top['count']:,}).")
    for c in p.get("correlations", [])[:3]:
        out.append(f"“{c['a']}” and “{c['b']}” move {'together' if c['r'] > 0 else 'in opposite directions'} (correlation {c['r']}).")
    return out[:14]


def profile_text(p: dict, limit: int = 6000) -> str:
    """The profile as compact text, for an AI to comment on (no raw rows)."""
    lines = [f"Rows: {p['rows']}, columns: {p['column_count']}, duplicate rows: {p['duplicates']}, empty cells: {p['missing_pct']}%"]
    for c in p["columns"][:60]:
        s = f"- {c['name']} [{c['type']}] filled={c['filled']} unique={c['unique']}"
        st = c.get("stats")
        if st:
            s += f" min={fmt_num(st['min'])} max={fmt_num(st['max'])} mean={fmt_num(st['mean'])} median={fmt_num(st['median'])} sum={fmt_num(st['sum'])} outliers={st['outliers']}"
        if c.get("top"):
            s += " top=" + "; ".join(f"{t['value']} ({t['pct']}%)" for t in c["top"][:5])
        if c.get("range"):
            s += f" from {c['range']['from']} to {c['range']['to']}"
        lines.append(s)
    g = p.get("group")
    if g:
        lines.append(f"By {g['by']}" + (f" (total {g['value']}):" if g["value"] else ":"))
        lines += [f"  {i['key']}: n={i['count']}" + (f" sum={fmt_num(i['sum'])}" if i["sum"] is not None else "") for i in g["rows"][:10]]
    lines.append("Findings: " + " | ".join(p["insights"]))
    return "\n".join(lines)[:limit]


# ================================================================== analysis report
def analysis_markdown(p: dict, title: str, ai_text: str | None = None) -> str:
    """The analysis as Markdown, for the Word and PDF reports."""
    md = [f"# {title}", "", "## Overview", "",
          "| Measure | Value |", "| --- | --- |",
          f"| Rows | {p['rows']:,} |", f"| Columns | {p['column_count']} |",
          f"| Duplicate rows | {p['duplicates']:,} |", f"| Empty cells | {p['missing_pct']}% |", "",
          "## Key findings", ""]
    md += [f"- {i}" for i in p["insights"]]
    if ai_text:
        md += ["", "## AI commentary", "", ai_text.strip()]
    md += ["", "## Columns", "", "| Column | Type | Filled | Empty % | Unique |", "| --- | --- | --- | --- | --- |"]
    md += [f"| {c['name']} | {c['type']} | {c['filled']:,} | {c['missing_pct']} | {c['unique']:,} |" for c in p["columns"][:80]]
    nums = [c for c in p["columns"] if c.get("stats")]
    if nums:
        md += ["", "## Numbers", "", "| Column | Sum | Average | Median | Min | Max | Unusual |", "| --- | --- | --- | --- | --- | --- | --- |"]
        md += [f"| {c['name']} | {fmt_num(c['stats']['sum'])} | {fmt_num(c['stats']['mean'])} | {fmt_num(c['stats']['median'])} | "
               f"{fmt_num(c['stats']['min'])} | {fmt_num(c['stats']['max'])} | {c['stats']['outliers']} |" for c in nums[:40]]
    g = p.get("group")
    if g:
        head = f"## By {g['by']}"
        md += ["", head, ""]
        if g["value"]:
            md += [f"| {g['by']} | Rows | Total {g['value']} | Average |", "| --- | --- | --- | --- |"]
            md += [f"| {i['key']} | {i['count']:,} | {fmt_num(i['sum']) if i['sum'] is not None else ''} | "
                   f"{fmt_num(i['mean']) if i['mean'] is not None else ''} |" for i in g["rows"]]
        else:
            md += [f"| {g['by']} | Rows |", "| --- | --- |"] + [f"| {i['key']} | {i['count']:,} |" for i in g["rows"]]
    tops = [c for c in p["columns"] if c.get("top") and c["type"] == "text"][:5]
    for c in tops:
        md += ["", f"## Most common: {c['name']}", "", "| Value | Rows | Share % |", "| --- | --- | --- |"]
        md += [f"| {t['value']} | {t['count']:,} | {t['pct']} |" for t in c["top"][:8]]
    return "\n".join(md)


def build_analysis_xlsx(p: dict, headers: list[str], rows: Iterable[dict], title: str, accent: str = "#2563eb",
                        max_rows: int = MAX_DATA_ROWS) -> bytes:
    """Workbook: Summary, Columns, Numbers, grouped totals with a chart, a monthly trend, and the data itself."""
    acc = _hex(accent)
    wb = Workbook()
    wb.properties.title = title[:150]
    used: set[str] = set()

    ws = wb.active
    ws.title = _sheet_name("Summary", used)
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=16, color=acc)
    ws["A3"], ws["B3"] = "Rows", p["rows"]
    ws["A4"], ws["B4"] = "Columns", p["column_count"]
    ws["A5"], ws["B5"] = "Duplicate rows", p["duplicates"]
    ws["A6"], ws["B6"] = "Empty cells %", p["missing_pct"]
    for r in range(3, 7):
        ws.cell(row=r, column=1).font = Font(bold=True)
    ws["A8"] = "Key findings"
    ws["A8"].font = Font(bold=True, size=12, color=acc)
    for i, text in enumerate(p["insights"], 9):
        c = ws.cell(row=i, column=1)
        _set_cell(c, f"• {text}")
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=i, start_column=1, end_row=i, end_column=6)
        ws.row_dimensions[i].height = 30 if len(text) > 90 else 18
    ws.column_dimensions["A"].width = 26
    for col in "BCDEF":
        ws.column_dimensions[col].width = 18

    cols = wb.create_sheet(_sheet_name("Columns", used))
    write_table(cols, [["Column", "Type", "Filled", "Empty", "Empty %", "Unique"]] +
                [[c["name"], c["type"], c["filled"], c["missing"], c["missing_pct"], c["unique"]] for c in p["columns"]],
                acc, convert=False)

    nums = [c for c in p["columns"] if c.get("stats")]
    if nums:
        ns = wb.create_sheet(_sheet_name("Numbers", used))
        write_table(ns, [["Column", "Count", "Sum", "Average", "Median", "Min", "Max", "Std dev", "Unusual values"]] +
                    [[c["name"], s["count"], s["sum"], s["mean"], s["median"], s["min"], s["max"], s["std"], s["outliers"]]
                     for c in nums for s in [c["stats"]]], acc, convert=False)

    g = p.get("group")
    if g and g["rows"]:
        gs = wb.create_sheet(_sheet_name(f"By {g['by']}", used))
        has_value = bool(g["value"])
        header = [g["by"], "Rows"] + ([f"Total {g['value']}", "Average"] if has_value else [])
        body = [[i["key"], i["count"]] + ([i["sum"], i["mean"]] if has_value else []) for i in g["rows"]]
        write_table(gs, [header] + body, acc, convert=False)
        chart = BarChart()
        chart.type, chart.style = "col", 10
        chart.title = f"{g['value'] and 'Total ' + g['value'] or 'Rows'} by {g['by']}"
        value_col = 3 if has_value else 2
        chart.add_data(Reference(gs, min_col=value_col, min_row=1, max_row=len(body) + 1), titles_from_data=True)
        chart.set_categories(Reference(gs, min_col=1, min_row=2, max_row=len(body) + 1))
        chart.legend = None
        chart.height, chart.width = 9, 18
        gs.add_chart(chart, f"{get_column_letter(len(header) + 2)}2")

    date_col = next((c for c in p["columns"] if c.get("by_month") and len(c["by_month"]) >= 2), None)
    if date_col:
        ts = wb.create_sheet(_sheet_name("Trend", used))
        data = [["Month", f"Rows by {date_col['name']}"]] + [[m["month"], m["count"]] for m in date_col["by_month"]]
        write_table(ts, data, acc, convert=False)
        line = LineChart()
        line.title, line.height, line.width, line.legend = f"Rows per month ({date_col['name']})", 9, 18, None
        line.add_data(Reference(ts, min_col=2, min_row=1, max_row=len(data)), titles_from_data=True)
        line.set_categories(Reference(ts, min_col=1, min_row=2, max_row=len(data)))
        ts.add_chart(line, "D2")

    ds = wb.create_sheet(_sheet_name("Data", used))
    ds.append([str(h) for h in headers])
    for i, r in enumerate(rows):
        if i >= max_rows:
            break
        ds.append([_data_value(r.get(h)) for h in headers])
    for row in ds.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
    if headers:
        _style_header(ds, len(headers), acc)
        ds.freeze_panes = "A2"
        ds.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ds.max_row}"
        _fit_columns(ds)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _data_value(v: Any) -> Any:
    """A value Excel can store. MongoDB dates carry a time zone, which Excel files cannot hold: convert to UTC."""
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    if isinstance(v, dt.time):
        return v.replace(tzinfo=None)
    if v is None or isinstance(v, (int, float, bool, str, dt.date)):
        return v
    return str(v)

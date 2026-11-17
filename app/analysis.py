"""Data analysis: read a table (uploaded file or one of your MongoDB collections), measure it, and write reports.

No AI is needed for the numbers, the findings or the files. AI commentary is optional and sees only the measured
profile (counts, totals, top values), never the raw rows, and follows the "Send my data to external AI" setting.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any

from . import docgen, extract, llm, media
from .config import cfg
from .runtime_settings import get_runtime

log = logging.getLogger("rag.analysis")
MAX_ROWS = 100_000
FORMATS = ("xlsx", "docx", "pdf")

AI_SYSTEM = (
    "You are a careful data analyst. You receive a statistical profile of a table (not the raw data). Write 5 to 7 short "
    "Markdown bullet points: the most important patterns, data-quality risks, and two or three concrete next steps. "
    "Use only numbers that appear in the profile and never invent values. If the user asks a question, answer it first. "
    "Reply in the language of the question if one is given, otherwise English. Bullets only, no headings."
)


def rows_from_file(filename: str, data: bytes, max_rows: int = MAX_ROWS) -> tuple[list[dict], bool]:
    """Rows of a .csv .tsv .xlsx .json .jsonl file. Returns (rows, truncated)."""
    if extract.kind_of(filename) != "table":
        raise ValueError("Upload a table file: .xlsx, .csv, .tsv, .json or .jsonl")
    rows: list[dict] = []
    truncated = False
    for rec in extract.table_rows(filename, data):
        if len(rows) >= max_rows:
            truncated = True
            break
        rows.append(rec)
    if not rows:
        raise ValueError("The file has no rows to analyse")
    return rows, truncated


def flatten_doc(doc: dict, depth: int = 2) -> dict:
    """A MongoDB document as one flat row: nested objects become 'a.b' columns, lists become text."""
    out: dict[str, Any] = {}

    def put(key: str, v: Any, d: int) -> None:
        if isinstance(v, dict):
            if d >= depth:
                out[key] = str(v)[:200]
            else:
                for k2, v2 in v.items():
                    put(f"{key}.{k2}", v2, d + 1)
        elif isinstance(v, (list, tuple, set)):
            out[key] = ", ".join(str(x)[:60] for x in list(v)[:20])
        elif isinstance(v, (str, int, float, bool, dt.date, dt.datetime)) or v is None:
            out[key] = v
        else:
            out[key] = str(v)  # ObjectId, Decimal128, Binary ...

    for k, v in doc.items():
        if k == "_id" and len(doc) > 1:
            continue
        put(str(k), v, 1)
    return out


def _title(name: str) -> str:
    stem = re.sub(r"[_\-]+", " ", Path(name).stem).strip() or "data"
    return f"Analysis of {stem}"[:120]


async def _ai_commentary(profile: dict, question: str, settings: dict) -> tuple[str | None, str | None]:
    text = docgen.profile_text(profile)
    user = (f"Question: {question}\n\n" if question else "") + f"PROFILE:\n{text}"
    try:
        res = await llm.chat([{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": user}],
                             require_trusted=not settings["send_context_to_external"], temperature=0.2,
                             max_tokens=700, profile="reasoning")
        return res.text.strip() or None, None
    except llm.AllProvidersFailed as e:
        return None, f"AI commentary skipped: {e}"


async def analyse(rows: list[dict], truncated: bool, name: str, formats: list[str], ai: bool, question: str,
                  owner: str, user_id: str | None = None) -> dict:
    """Profile the rows and make the requested files. Returns the profile, optional AI text and the saved files."""
    formats = [f for f in dict.fromkeys(formats) if f in FORMATS]
    title = _title(name)
    profile = await asyncio.to_thread(docgen.profile_rows, rows, truncated)
    ai_text = ai_error = None
    if ai:
        ai_text, ai_error = await _ai_commentary(profile, question.strip()[:500], await get_runtime())
    accent = "#" + docgen._hex(None)
    headers = [c["name"] for c in profile["columns"]]
    files = []
    for fmt in formats:
        if fmt == "xlsx":
            data = await asyncio.to_thread(docgen.build_analysis_xlsx, profile, headers, rows, title, accent)
            if len(data) > media.MAX_FILE_BYTES:  # very wide data: keep the analysis, shorten the copy of the data
                data = await asyncio.to_thread(docgen.build_analysis_xlsx, profile, headers, rows, title, accent, 5000)
            mime, ext = docgen.XLSX_MIME, "xlsx"
        else:
            md = docgen.analysis_markdown(profile, title, ai_text)
            if fmt == "docx":
                data, mime, ext = await asyncio.to_thread(docgen.build_docx, title, md.split("\n", 2)[2], None, "", accent), docgen.DOCX_MIME, "docx"
            else:
                data = await asyncio.to_thread(media.build_pdf, title, md.split("\n", 2)[2], None, "", accent, None, "classic")
                mime, ext = "application/pdf", "pdf"
        files.append(await media.save_file(owner, user_id, None, fmt, media._slug(title, ext), mime, data, None, None))
    return {"title": title, "profile": profile, "ai": ai_text, "ai_error": ai_error, "files": files}

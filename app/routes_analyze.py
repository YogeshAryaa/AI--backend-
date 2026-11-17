"""Analyze a table: upload an Excel/CSV file, or pick one of your MongoDB collections."""
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import analysis
from .auth import Principal, require, require_admin
from .config import cfg
from .db import db, is_internal, source_db

log = logging.getLogger("rag.routes.analyze")
router = APIRouter(prefix="/analyze", tags=["analyze"])


def _formats(raw: str) -> list[str]:
    out = [f.strip().lower() for f in raw.split(",") if f.strip().lower() in analysis.FORMATS]
    return out or ["xlsx"]


@router.post("/file")
async def analyse_file(file: UploadFile = File(...), formats: str = Form("xlsx,docx"), ai: bool = Form(False),
                       question: str = Form(""), p: Principal = Depends(require("chat"))):
    """Measures the table in the file (column types, empty cells, duplicates, totals, unusual values, trends)
    and returns it with the Excel / Word / PDF reports you asked for."""
    limit = cfg.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File too large (max {cfg.max_upload_mb} MB)")
    try:
        rows, truncated = analysis.rows_from_file(file.filename or "data.csv", data)
        return await analysis.analyse(rows, truncated, file.filename or "data", _formats(formats), ai, question, p.key_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # a broken file should be a clear message, not a server error page
        log.exception("analysis failed")
        raise HTTPException(400, f"Could not read this file: {type(e).__name__}: {e}")


class CollectionIn(BaseModel):
    collection: str = Field(min_length=1, max_length=120)
    database: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=20000, ge=10, le=50000)
    formats: list[str] = ["xlsx", "docx"]
    ai: bool = False
    question: str = Field(default="", max_length=500)


@router.post("/collection")
async def analyse_collection(body: CollectionIn, p: Principal = Depends(require_admin)):
    """Same analysis for one of your MongoDB collections (admin only; this app's own collections are refused)."""
    if is_internal(body.collection, body.database):
        raise HTTPException(403, "That collection belongs to this app and cannot be analysed")
    cursor = source_db(body.database)[body.collection].find({}).limit(body.limit)
    docs = await cursor.to_list(body.limit)
    if not docs:
        raise HTTPException(404, "That collection is empty or does not exist")
    rows = [analysis.flatten_doc(d) for d in docs]
    try:
        return await analysis.analyse(rows, len(docs) >= body.limit, body.collection, [f.lower() for f in body.formats],
                                      body.ai, body.question, p.key_id)
    except Exception as e:
        log.exception("collection analysis failed")
        raise HTTPException(500, f"Could not analyse this collection: {type(e).__name__}: {e}")

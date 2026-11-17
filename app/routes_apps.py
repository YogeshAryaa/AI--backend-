"""Connect other apps from the frontend and send answers / files to them (admin only)."""
import logging
import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import apps
from .auth import require_admin
from .db import db, utcnow
from .security import encrypt

log = logging.getLogger("rag.routes.apps")
router = APIRouter(prefix="/apps", tags=["apps"], dependencies=[Depends(require_admin)])


class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: str
    config: dict = {}


class AppPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    enabled: bool | None = None
    config: dict | None = None


class SendIn(BaseModel):
    text: str = Field(default="", max_length=4000)
    file_id: str | None = Field(default=None, max_length=64)


@router.get("/types")
async def types():
    return {"types": [{"type": k, **v} for k, v in apps.TYPES.items()]}


@router.get("")
async def list_apps():
    docs = await db.rag_apps.find({}).sort("created_at", 1).to_list(apps.MAX_APPS)
    return {"apps": [apps.public(d) for d in docs]}


@router.post("")
async def add_app(body: AppIn):
    if await db.rag_apps.count_documents({}) >= apps.MAX_APPS:
        raise HTTPException(400, f"At most {apps.MAX_APPS} connected apps")
    try:
        plain, secret = apps.split_config(body.type, body.config)
    except apps.AppError as e:
        raise HTTPException(400, str(e))
    doc = {"_id": _secrets.token_hex(6), "name": body.name.strip(), "type": body.type, "enabled": True, "config": plain,
           "secrets_enc": {k: encrypt(v) for k, v in secret.items()}, "created_at": utcnow()}
    await db.rag_apps.insert_one(doc)
    return apps.public(doc)


async def _get(app_id: str) -> dict:
    doc = await db.rag_apps.find_one({"_id": app_id})
    if not doc:
        raise HTTPException(404, "App not found")
    return doc


@router.patch("/{app_id}")
async def patch_app(app_id: str, body: AppPatch):
    doc = await _get(app_id)
    upd: dict = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.enabled is not None:
        upd["enabled"] = body.enabled
    if body.config is not None:
        from .security import decrypt
        try:
            plain, secret = apps.split_config(doc["type"], body.config, doc.get("config"),
                                              {k: decrypt(v) for k, v in (doc.get("secrets_enc") or {}).items()})
        except apps.AppError as e:
            raise HTTPException(400, str(e))
        upd["config"] = plain
        upd["secrets_enc"] = {k: encrypt(v) for k, v in secret.items()}
    if upd:
        await db.rag_apps.update_one({"_id": app_id}, {"$set": upd})
    return apps.public(await _get(app_id))


@router.delete("/{app_id}")
async def delete_app(app_id: str):
    res = await db.rag_apps.delete_one({"_id": app_id})
    if not res.deleted_count:
        raise HTTPException(404, "App not found")
    return {"deleted": True}


async def _deliver(doc: dict, text: str, file: dict | None) -> dict:
    try:
        detail = await apps.send(doc, text, file)
    except apps.AppError as e:
        await db.rag_apps.update_one({"_id": doc["_id"]}, {"$set": {"last_error": str(e), "last_error_at": utcnow()}})
        raise HTTPException(502, str(e))
    await db.rag_apps.update_one({"_id": doc["_id"]}, {"$set": {"last_used": utcnow(), "last_error": None}})
    return {"ok": True, "detail": detail}


@router.post("/{app_id}/test")
async def test_app(app_id: str):
    return await _deliver(await _get(app_id), "✅ Test message from Ask my data. The connection works.", None)


@router.post("/{app_id}/send")
async def send_to_app(app_id: str, body: SendIn):
    doc = await _get(app_id)
    if not doc.get("enabled", True):
        raise HTTPException(400, "This app is switched off")
    file = None
    if body.file_id:
        file = await db.rag_files.find_one({"_id": body.file_id})
        if not file:
            raise HTTPException(404, "File not found (it may have been deleted)")
    if not body.text.strip() and not file:
        raise HTTPException(400, "Nothing to send")
    return await _deliver(doc, body.text, file)

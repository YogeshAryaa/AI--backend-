"""Admin endpoints for the Agent Skills: see them, test them, switch them on/off, install and remove them, link them."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from . import skill_hub, skill_install, skill_registry
from .auth import require_admin
from .db import db

router = APIRouter(prefix="/agent-skills", tags=["agent-skills"], dependencies=[Depends(require_admin)])


async def _usage() -> dict:
    try:
        return {d["_id"]: {"used": d.get("used", 0), "as_linked": d.get("as_linked", 0), "last_used": d.get("last_used")}
                async for d in db.rag_skill_usage.find({})}
    except Exception:
        return {}


async def _state() -> dict:
    doc = await db.settings.find_one({"_id": "skills"}) or {}
    return {"disabled": list(doc.get("disabled") or []), "links": [list(x) for x in doc.get("links") or []]}


@router.get("")
async def list_skills():
    installed = skill_registry.discover_skills()
    catalog = skill_registry.catalog_entries()
    installed_catalog = skill_registry.installed_catalog_ids(installed)
    usage = await _usage()
    state = await _state()
    off = set(state["disabled"])
    return {
        "source": "https://github.com/VoltAgent/awesome-agent-skills",
        "note": "The upstream repository is a catalog. Install individual SKILL.md packages to make them active.",
        "installed_count": len(installed),
        "catalog_count": len(catalog),
        "installed": [{**i, "enabled": i["id"] not in off and i["group"] not in off, "usage": usage.get(i["id"]),
                       "removable": i["id"].startswith("community/")} for i in installed],
        "links": state["links"],
        "catalog": [{**item, "installed": item["id"] in installed_catalog} for item in catalog],
    }


@router.get("/installed")
async def installed_skills():
    return {"skills": skill_registry.discover_skills()}


@router.get("/graph")
async def skill_graph():
    """Which installed skills are connected to which (skills/skill_links.json plus links added in the frontend)."""
    return skill_registry.graph((await _state())["links"])


@router.get("/plan")
async def skill_plan(q: str = Query(min_length=1, max_length=2000),
                     profile: str = Query("coding", pattern="^(coding|reasoning|creative|general)$"),
                     tier: str = Query("local", pattern="^(local|cloud)$")):
    """Shows which skills (and connected skills) would be used for a question, and how much prompt text they add.
    Calls no AI and no database search, so it is instant."""
    state = await _state()
    plan = skill_registry.make_plan(q, profile, tier, set(state["disabled"]), state["links"])
    text = skill_registry.prompt_context(q, profile, tier, plan)
    strip = lambda items: [{k: v for k, v in i.items() if k != "content"} for i in items]
    return {"tier": tier, "prompt_chars": len(text), "primary": strip(plan["primary"]), "linked": strip(plan["linked"])}


# ---------------------------------------------------------------- install / remove
@router.post("/install")
async def install_skill(file: UploadFile | None = File(None), name: str = Form(""), content: str = Form(""),
                        replace: bool = Form(False)):
    """Paste a SKILL.md (content) or upload a .zip / SKILL.md file. The skill is reference text; nothing in it is run."""
    try:
        if file is not None and file.filename:
            data = await file.read(skill_install.MAX_ZIP_BYTES + 1)
            if file.filename.lower().endswith(".zip"):
                return skill_install.install_zip(data, name or None, replace)
            if len(data) > skill_install.MAX_TEXT:
                raise skill_install.SkillInstallError("That file is too large for a SKILL.md")
            return skill_install.install_text(name or None, data.decode("utf-8", "replace"), replace)
        return skill_install.install_text(name or None, content, replace)
    except skill_install.SkillInstallError as e:
        raise HTTPException(400, str(e))


@router.delete("")
async def remove_skill(id: str = Query(min_length=3, max_length=200)):
    try:
        removed = skill_install.remove(id)
    except skill_install.SkillInstallError as e:
        raise HTTPException(400, str(e))
    await db.settings.update_one({"_id": "skills"}, {"$pull": {"disabled": removed}}, upsert=True)
    skill_hub.forget_state()
    return {"removed": removed}


# ---------------------------------------------------------------- on/off and links (kept in MongoDB)
class StateIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)   # a skill id, or a bundle id such as community/api-skill
    enabled: bool


@router.put("/state")
async def set_state(body: StateIn):
    op = "$pull" if body.enabled else "$addToSet"
    await db.settings.update_one({"_id": "skills"}, {op: {"disabled": body.id}}, upsert=True)
    skill_hub.forget_state()
    return {"id": body.id, "enabled": body.enabled}


class LinkIn(BaseModel):
    a: str = Field(min_length=1, max_length=200)
    b: str = Field(min_length=1, max_length=200)
    why: str = Field(default="", max_length=160)


def _ids() -> set[str]:
    return {i["id"] for i in skill_registry.discover_skills()}


@router.post("/links")
async def add_link(body: LinkIn):
    ids = _ids()
    if body.a == body.b or body.a not in ids or body.b not in ids:
        raise HTTPException(400, "Pick two different installed skills")
    link = [body.a, body.b, body.why.strip()]
    await db.settings.update_one({"_id": "skills"}, {"$pull": {"links": {"$in": [link]}}}, upsert=True)
    await db.settings.update_one({"_id": "skills"}, {"$push": {"links": link}}, upsert=True)
    skill_hub.forget_state()
    return {"links": (await _state())["links"]}


@router.delete("/links")
async def delete_link(a: str = Query(min_length=1, max_length=200), b: str = Query(min_length=1, max_length=200)):
    doc = await db.settings.find_one({"_id": "skills"}) or {}
    keep = [l for l in doc.get("links") or [] if not ({l[0], l[1]} == {a, b})]
    await db.settings.update_one({"_id": "skills"}, {"$set": {"links": keep}}, upsert=True)
    skill_hub.forget_state()
    return {"links": keep}

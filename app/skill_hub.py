"""Connects the skills, your database and the model for one question.

For an AI answer it decides, in one step and without extra database queries or AI calls:
  1. whether the model is local (Ollama) or hosted, which sets how much skill text fits;
  2. which skills match, plus the skills linked to them (see skills/skill_links.json);
  3. which records from your database (already found by the search step) go along as context;
and it counts skill usage in MongoDB in the background, so it never slows the answer down.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from pymongo import UpdateOne

from . import intelligence, llm, skill_registry
from .config import cfg
from .db import db, utcnow

log = logging.getLogger("rag.skill_hub")
USAGE_COLLECTION = "rag_skill_usage"
_tasks: set[asyncio.Task] = set()
_STATE: dict = {"at": 0.0, "disabled": set(), "links": []}


async def state() -> dict:
    """Skills switched off and links added in the frontend (stored in MongoDB), re-read at most every 5 seconds."""
    if time.monotonic() - _STATE["at"] < 5:
        return _STATE
    try:
        doc = await db.settings.find_one({"_id": "skills"}) or {}
        _STATE.update(disabled=set(doc.get("disabled") or []), links=[tuple(x) for x in doc.get("links") or []])
    except Exception:
        log.debug("could not read skill settings", exc_info=True)
    _STATE["at"] = time.monotonic()
    return _STATE


def forget_state() -> None:
    _STATE["at"] = 0.0


@dataclass
class Hub:
    system_extra: str = ""            # text appended to the system prompt
    tier: str = "cloud"               # "local" | "cloud"
    skills: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    records: int = 0                  # database records included
    require_trusted: bool = False     # True when records are included and external AI must not see your data

    def meta(self) -> dict:
        return {"skills": self.skills, "linked_skills": self.linked, "records_used": self.records, "prompt_tier": self.tier}


async def _record_usage(primary: list[str], linked: list[str], profile: str) -> None:
    now = utcnow()
    ops = [UpdateOne({"_id": sid}, {"$inc": {"used": 1}, "$set": {"last_used": now, "last_profile": profile}}, upsert=True)
           for sid in primary]
    ops += [UpdateOne({"_id": sid}, {"$inc": {"as_linked": 1}, "$set": {"last_used": now}}, upsert=True) for sid in linked]
    if not ops:
        return
    try:
        await db[USAGE_COLLECTION].bulk_write(ops, ordered=False)
    except Exception:  # statistics only: never let them affect an answer
        log.debug("could not record skill usage", exc_info=True)


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def build(question: str, profile: str, chunks: list[dict], settings: dict, trusted_only: bool) -> Hub:
    use_skills = cfg.skills_enabled
    tier = await llm.provider_class(profile, trusted_only)
    st = await state() if use_skills else _STATE
    plan = (skill_registry.make_plan(question, profile, tier, st["disabled"], st["links"]) if use_skills
            else {"tier": tier, "primary": [], "linked": []})
    guidance = intelligence.guidance_for(profile, question if use_skills else "", tier=tier, plan=plan)

    data, n = "", 0
    if cfg.skills_use_database and chunks:
        floor = max(0.3, float(settings.get("effective_ai_min_score") or 0.5) - 0.12)
        data, n = intelligence.data_context(chunks, floor, tier)

    primary = [i["id"] for i in plan["primary"]]
    linked = [i["id"] for i in plan["linked"]]
    if primary:
        _background(_record_usage(primary, linked, profile))
    return Hub(guidance + data, tier, primary, linked, n, require_trusted=bool(n) and trusted_only)

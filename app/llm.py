"""Task-aware router over OpenAI-compatible chat APIs.

Provider choice is ranked by task profile and configured model-family hints, with configured
priority as a tie-breaker. Cool-downs and rate limits remain shared through MongoDB.
Model-name matching is heuristic; it does not measure or improve the underlying model weights.
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from . import limits, intelligence
from .db import db, utcnow
from .security import decrypt

DEFAULT_TIMEOUT = 60
log = logging.getLogger("rag.llm")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal", "ollama"}


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors) or "no providers available")


def _models_of(p: dict) -> list[str]:
    """`model` may hold several comma-separated models, tried in order."""
    return [m.strip() for m in str(p.get("model", "")).split(",") if m.strip()]



def _skey(pid: str, model: str) -> str:
    return f"{pid}|{model}"


def _timeout(p: dict) -> httpx.Timeout:
    t = float(p.get("timeout_seconds") or DEFAULT_TIMEOUT)
    return httpx.Timeout(t, connect=min(10.0, t))


# ------------------------------------------------------------ shared provider state
async def load_states(pids: list[str] | None = None) -> dict[str, dict]:
    q = {} if pids is None else {"pid": {"$in": pids}}
    return {d["_id"]: d async for d in db.rag_provider_state.find(q)}


def _cooldown_left(states: dict, pid: str, model: str) -> int:
    s = states.get(_skey(pid, model))
    left = (s["open_until"] - time.time()) if s else 0
    return int(left) + 1 if left > 0 else 0


def provider_status(p: dict, states: dict) -> dict:
    """A provider is 'cooling down' only if EVERY one of its models is paused."""
    pid = str(p["_id"])
    lefts = [_cooldown_left(states, pid, m) for m in _models_of(p)]
    if lefts and all(x > 0 for x in lefts):
        return {"state": "cooling_down", "retry_in_seconds": min(lefts)}
    return {"state": "ready", "retry_in_seconds": 0}


async def _available(p: dict, states: dict) -> tuple[bool, str]:
    pid = str(p["_id"])
    left = _cooldown_left(states, pid, p["model"])
    if left > 0:
        return False, f"{p['model']} cooling down for {left}s"
    if p.get("rpm_limit") and (await limits.count(f"prov:{pid}", 60)) >= p["rpm_limit"]:
        return False, "per-minute limit reached"
    if p.get("rpd_limit") and (await limits.count(f"prov:{pid}", 86400)) >= p["rpd_limit"]:
        return False, "daily limit reached"
    return True, ""


async def _count_call(p: dict) -> None:
    pid = str(p["_id"])
    if p.get("rpm_limit"):
        await limits.hit(f"prov:{pid}", 60, 1 << 30)
    if p.get("rpd_limit"):
        await limits.hit(f"prov:{pid}", 86400, 1 << 30)


async def _ok(p: dict, states: dict) -> None:
    key = _skey(str(p["_id"]), p["model"])
    if key in states:
        await db.rag_provider_state.delete_one({"_id": key})
        states.pop(key, None)
    await db.providers.update_one(
        {"_id": p["_id"]}, {"$inc": {"stats.success": 1}, "$set": {"stats.last_used": utcnow()}}
    )


async def _fail(p: dict, states: dict, status: int | None, retry_after: str | None, err: str) -> None:
    pid = str(p["_id"])
    key = _skey(pid, p["model"])
    fails = states.get(key, {}).get("fails", 0) + 1
    cooldown = 0
    if status == 429:
        try:
            cooldown = min(int(float(retry_after)), 3600) if retry_after else 60
        except ValueError:
            cooldown = 60
    elif status in (401, 403):
        cooldown = 600
    elif status == 404:  # model retired / renamed: skip it for a while, try the next one
        cooldown = 1800
    elif fails >= 3:
        cooldown = min(60 * fails, 600)
    state = {"pid": pid, "fails": fails, "open_until": time.time() + cooldown if cooldown else 0}
    states[key] = state
    await db.rag_provider_state.update_one({"_id": key}, {"$set": state}, upsert=True)
    await db.providers.update_one(
        {"_id": p["_id"]},
        {"$inc": {"stats.failure": 1},
         "$set": {"stats.last_error": f"{status or 'ERR'}: {err}"[:300], "stats.last_error_at": datetime.now(timezone.utc)}},
    )


async def load_providers(require_trusted: bool = False) -> list[dict]:
    q: dict = {"enabled": True}
    if require_trusted:
        q["trusted"] = True
    docs = await db.providers.find(q).sort("priority", 1).to_list(100)
    out: list[dict] = []
    for d in docs:
        d["api_key"] = decrypt(d.get("api_key_enc", "")) if d.get("api_key_enc") else ""
        for m in _models_of(d):  # one attempt per model, in the order written
            out.append({**d, "model": m})
    return out


def is_local_provider(p: dict) -> bool:
    """A model served from this machine or network (Ollama, LM Studio ...): slow prompt processing on CPU."""
    try:
        u = urlparse(str(p.get("base_url", "")))
    except ValueError:
        return False
    return (u.hostname or "").lower() in _LOCAL_HOSTS or u.port == 11434 or "ollama" in str(p.get("name", "")).lower()


def _is_ollama(p: dict) -> bool:
    try:
        port = urlparse(str(p.get("base_url", ""))).port
    except ValueError:
        port = None
    return port == 11434 or "ollama" in str(p.get("name", "")).lower()


async def provider_class(profile: str, require_trusted: bool = False) -> str:
    """"local" when the provider that will be tried first runs on this machine, else "cloud".
    Decides how much skill text goes into the prompt."""
    try:
        providers = intelligence.rank_providers(await load_providers(require_trusted), profile)
    except Exception:  # database hiccup: fall back to the full prompt rather than failing the question
        return "cloud"
    return "local" if providers and is_local_provider(providers[0]) else "cloud"


async def warm_local() -> None:
    """Load the local Ollama model into memory at start-up, so the first question does not wait for it.
    (To keep it loaded between questions, start Ollama with OLLAMA_KEEP_ALIVE=-1. See docs/CONNECTED_SKILLS.md.)"""
    try:
        providers = [p for p in await load_providers() if _is_ollama(p)]
    except Exception:
        return
    if not providers:
        return
    p = providers[0]
    base = re.sub(r"/v1/?$", "", str(p["base_url"]).rstrip("/"))
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(base + "/api/generate", json={"model": p["model"], "prompt": "", "keep_alive": -1, "stream": False},
                                  timeout=httpx.Timeout(180.0, connect=3.0))
        log.info("Ollama warm-up for %s: HTTP %s", p["model"], r.status_code)
    except httpx.HTTPError as e:
        log.info("Ollama warm-up skipped (%s)", type(e).__name__)


def _request(p: dict, messages: list[dict], stream: bool, temperature: float, max_tokens: int | None):
    headers = {"Content-Type": "application/json", **(p.get("extra_headers") or {})}
    if p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    body = {"model": p["model"], "messages": messages, "temperature": temperature, "stream": stream}
    if max_tokens:
        body["max_tokens"] = max_tokens
    return p["base_url"].rstrip("/") + "/chat/completions", headers, body


def _no_providers(require_trusted: bool) -> AllProvidersFailed:
    return AllProvidersFailed(["no enabled local/trusted providers" if require_trusted else "no enabled providers"])


# ------------------------------------------------------------ public API
async def chat(messages: list[dict], require_trusted: bool = False, temperature: float = 0.2,
               max_tokens: int | None = 1024, profile: str = "balanced") -> LLMResult:
    providers = intelligence.rank_providers(await load_providers(require_trusted), profile)
    if not providers:
        raise _no_providers(require_trusted)
    states = await load_states()
    errors: list[str] = []
    async with httpx.AsyncClient() as client:
        for p in providers:
            ok, why = await _available(p, states)
            if not ok:
                errors.append(f"{p['name']}: {why}")
                continue
            await _count_call(p)
            try:
                url, headers, body = _request(p, messages, False, temperature, max_tokens)
                r = await client.post(url, headers=headers, json=body, timeout=_timeout(p))
                if r.status_code == 200:
                    text = (r.json()["choices"][0]["message"].get("content") or "").strip()
                    await _ok(p, states)
                    return LLMResult(text, p["name"], p["model"])
                await _fail(p, states, r.status_code, r.headers.get("retry-after"), r.text[:200])
                errors.append(f"{p['name']}: HTTP {r.status_code}")
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
                await _fail(p, states, None, None, repr(e))
                errors.append(f"{p['name']}: {type(e).__name__}")
    raise AllProvidersFailed(errors)


async def stream_chat(messages: list[dict], require_trusted: bool = False, temperature: float = 0.3,
                      max_tokens: int | None = 1500, profile: str = "balanced"):
    """Yields {"type":"meta"|"token"|"error"}. Fails over to the next provider only before the first token."""
    providers = intelligence.rank_providers(await load_providers(require_trusted), profile)
    if not providers:
        raise _no_providers(require_trusted)
    states = await load_states()
    errors: list[str] = []
    async with httpx.AsyncClient() as client:
        for p in providers:
            ok, why = await _available(p, states)
            if not ok:
                errors.append(f"{p['name']}: {why}")
                continue
            await _count_call(p)
            started = False
            try:
                url, headers, body = _request(p, messages, True, temperature, max_tokens)
                async with client.stream("POST", url, headers=headers, json=body, timeout=_timeout(p)) as r:
                    if r.status_code != 200:
                        detail = (await r.aread()).decode(errors="ignore")[:200]
                        await _fail(p, states, r.status_code, r.headers.get("retry-after"), detail)
                        errors.append(f"{p['name']}: HTTP {r.status_code}")
                        continue
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                        except (ValueError, KeyError, IndexError, TypeError):
                            continue
                        if delta:
                            if not started:
                                started = True
                                yield {"type": "meta", "provider": p["name"], "model": p["model"]}
                            yield {"type": "token", "text": delta}
                if started:
                    await _ok(p, states)
                    return
                await _fail(p, states, None, None, "empty stream")
                errors.append(f"{p['name']}: empty response")
            except httpx.HTTPError as e:
                await _fail(p, states, None, None, repr(e))
                if started:
                    yield {"type": "error", "message": f"{p['name']} dropped mid-answer"}
                    return
                errors.append(f"{p['name']}: {type(e).__name__}")
    raise AllProvidersFailed(errors)


async def test_provider(p: dict) -> dict:
    results: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for m in _models_of(p):
            start = time.time()
            try:
                url, headers, body = _request(
                    {**p, "model": m}, [{"role": "user", "content": "Reply with the single word: OK"}], False, 0, 10
                )
                r = await client.post(url, headers=headers, json=body, timeout=_timeout(p))
                ms = int((time.time() - start) * 1000)
                if r.status_code == 200:
                    results[m] = {"ok": True, "latency_ms": ms}
                    await db.rag_provider_state.delete_one({"_id": _skey(str(p["_id"]), m)})
                else:
                    results[m] = {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:150]}"}
            except httpx.HTTPError as e:
                results[m] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not results:
        return {"ok": False, "error": "No model set"}
    good = [m for m, r in results.items() if r["ok"]]
    bad = [f"{m} ({r['error']})" for m, r in results.items() if not r["ok"]]
    if good:
        note = "Working: " + ", ".join(good) + ("" if not bad else " | Failed: " + "; ".join(bad))
        return {"ok": True, "latency_ms": results[good[0]]["latency_ms"], "note": note}
    return {"ok": False, "error": " | ".join(bad)}


async def list_models(p: dict) -> list[str]:
    headers = dict(p.get("extra_headers") or {})
    if p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(p["base_url"].rstrip("/") + "/models", headers=headers)
        r.raise_for_status()
        data = r.json()
    items = data.get("data") or data.get("models") or []
    ids = []
    for m in items:
        mid = (m.get("id") or m.get("name")) if isinstance(m, dict) else m
        if mid:
            ids.append(str(mid).removeprefix("models/"))
    return sorted(ids)

import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("ADMIN_TOKEN", "t" * 32)
os.environ.setdefault("APP_SECRET_KEY", "s" * 32)

from app import chat, intelligence, skill_hub, skill_registry  # noqa: E402


def _chunk(text, cosine=0.45):
    return {"doc_id": "d1", "title": "Payments service", "source": "manual", "namespace": "general", "chunk_index": 0,
            "text": text, "cosine": cosine}


def ids(items):
    return [i["id"].replace("community/", "") for i in items]


class RoutingTests(unittest.TestCase):
    def test_every_link_and_keyword_target_resolves_to_an_installed_skill(self):
        items = skill_registry._index()
        for a, b, _ in skill_registry._edges():
            for key in (a, b):
                self.assertTrue(any(skill_registry._matches(i, key) for i in items), f"link target not installed: {key}")
        for key in skill_registry._links_data()["keywords"]:
            self.assertTrue(any(skill_registry._matches(i, key) for i in items), f"keyword target not installed: {key}")

    def test_right_skill_for_each_domain(self):
        cases = {
            "integrate Stripe payments webhooks": "stripe-best-practices",
            "write pytest fixtures and conftest": "pytest-skill",
            "tune slow postgres query indexes": "postgres-best-practices",
            "review this diff for security regressions": "differential-review",
            "call Gemini API with function calling": "gemini-api-dev",
            "build an MCP server": "mcp-builder",
            "make the landing page UI look professional": "frontend-design",
            "playwright end-to-end test": "playwright-skill",
            "dub my video into Hindi": "voicestudio",
            "transcribe this audio to subtitles": "voicestudio",
            "write openapi spec for my endpoints": "api-skill/openapi-spec-generator",
        }
        for q, want in cases.items():
            top = skill_registry.make_plan(q, "coding", "local")["primary"]
            self.assertTrue(top and top[0]["id"].endswith(want), f"{q!r} -> {ids(top)}")

    def test_business_questions_load_no_skills(self):
        for q in ["what is the leave policy for employees", "hello how are you", "give me java developers candidates list",
                  "what is the notice period for a senior QA engineer", "write a poem about rain"]:
            plan = skill_registry.make_plan(q, intelligence.classify_task(q), "cloud")
            self.assertEqual(plan["primary"], [], q)
            self.assertEqual(skill_registry.prompt_context(q, "general", "cloud", plan), "")

    def test_skills_are_connected(self):
        plan = skill_registry.make_plan("integrate Stripe payments webhooks", "coding", "cloud")
        self.assertEqual(ids(plan["primary"])[0], "stripe-best-practices")
        linked = ids(plan["linked"])
        self.assertTrue({"postgres-best-practices", "pytest-skill", "differential-review"} & set(linked), linked)
        text = skill_registry.prompt_context("", "coding", "cloud", plan)
        self.assertIn("CONNECTED SKILLS", text)

    def test_local_tier_is_short_and_cloud_is_full(self):
        q = "integrate Stripe payments webhooks"
        local = skill_registry.prompt_context(q, "coding", "local")
        cloud = skill_registry.prompt_context(q, "coding", "cloud")
        self.assertLessEqual(len(local), skill_registry.BUDGETS["local"]["total"])
        self.assertGreater(len(cloud), 2 * len(local))
        self.assertEqual(local.count("<agent_skill"), 1)  # one skill in full, the rest only named
        local_guidance = intelligence.guidance_for("coding", q, tier="local")
        self.assertNotIn("BUNDLED ENGINEERING SKILL", local_guidance)
        self.assertLess(len(local_guidance), 7000)

    def test_index_is_cached(self):
        self.assertIs(skill_registry._index(), skill_registry._index())

    def test_compact_keeps_paragraphs_and_lists_dropped_sections(self):
        text = "Intro line.\n\n" + ("para one. " * 30) + "\n\n## Later section\n\nmore text " * 5
        out = skill_registry.compact(text, 200)
        self.assertLess(len(out), 400)
        self.assertIn("Later section", out)


class DataContextTests(unittest.TestCase):
    def test_only_close_records_and_budget(self):
        chunks = [
            {"title": "A", "text": "x" * 5000, "cosine": 0.60},
            {"title": "B", "text": "close", "cosine": 0.55},
            {"title": "C", "text": "far", "cosine": 0.10},
            {"title": "D", "text": "third", "cosine": 0.56},
        ]
        text, n = intelligence.data_context(chunks, 0.4, "local")
        self.assertEqual(n, 2)  # local tier: at most 2 records
        self.assertNotIn("far", text)
        self.assertLessEqual(len(text), skill_registry.BUDGETS["local"]["data_chars"] + 500)
        self.assertEqual(intelligence.data_context([{"title": "C", "text": "far", "cosine": 0.1}], 0.4)[1], 0)
        self.assertEqual(intelligence.data_context([{"title": "E", "text": "id 42", "cosine": 0.0, "exact": True}], 0.4)[1], 1)


class RewriteTests(unittest.TestCase):
    def test_rewrite_only_when_it_can_help(self):
        self.assertFalse(intelligence.needs_rewrite("write a python function to parse csv"))
        self.assertTrue(intelligence.needs_rewrite("python function kaise banao"))
        self.assertTrue(intelligence.needs_rewrite("पायथन फंक्शन बनाओ"))
        self.assertTrue(intelligence.needs_rewrite("show employee leave policy"))  # not a coding question


class ChatPathTests(unittest.IsolatedAsyncioTestCase):
    """Runs the real chat fallback path with a fake database and a fake model that records the prompt."""

    async def run_question(self, question, tier, send_external=True, chunks=None):
        settings = {"cache_enabled": False, "send_context_to_external": send_external, "ai_fallback": True,
                    "effective_min_score": 0.6, "effective_ai_min_score": 0.5, "top_k": 5, "query_rewrite": True,
                    "max_context_chars": 6000}
        seen = {}

        async def fake_stream(messages, **kw):
            seen["messages"], seen["kw"] = messages, kw
            yield {"type": "meta", "provider": "Ollama (local)", "model": "qwen2.5:3b"}
            yield {"type": "token", "text": "ok"}

        ask = AsyncMock(return_value=None)
        weak = chunks if chunks is not None else []
        with patch.object(chat, "get_runtime", AsyncMock(return_value=settings)), \
             patch.object(chat, "embed_query", AsyncMock(return_value=None)), \
             patch.object(chat.knowledge, "retrieve", AsyncMock(return_value=(weak, 0.2, False))), \
             patch.object(chat.knowledge, "cache_store", AsyncMock()), \
             patch.object(chat, "_ask", ask), \
             patch.object(chat.llm, "stream_chat", fake_stream), \
             patch.object(skill_hub.llm, "provider_class", AsyncMock(return_value=tier)), \
             patch.object(skill_hub, "state", AsyncMock(return_value={"disabled": set(), "links": []})), \
             patch.object(skill_hub, "_record_usage", AsyncMock()):
            events = [e async for e in chat.run_chat(question, [], "ai")]
        return events, seen, ask

    async def test_local_model_gets_short_skill_prompt_database_records_and_no_rewrite_call(self):
        chunks = [_chunk("Our stripe webhook endpoint is /pay/hook (FastAPI).")]
        events, seen, ask = await self.run_question("integrate Stripe payments webhooks in our FastAPI app", "local", chunks=chunks)
        system = seen["messages"][0]["content"]
        self.assertIn("stripe-best-practices", system)
        self.assertIn("CONNECTED SKILLS", system)
        self.assertIn("YOUR DATABASE RECORDS", system)
        self.assertIn("/pay/hook", system)
        self.assertLess(len(system), 9000)
        ask.assert_not_called()  # English coding question: the slow AI rewrite is skipped
        meta = next(e for e in events if e["type"] == "meta")
        self.assertEqual(meta["prompt_tier"], "local")
        self.assertEqual(meta["skills"][0].split("/")[-1], "stripe-best-practices")
        self.assertEqual(meta["records_used"], 1)

    async def test_records_stay_on_trusted_providers_when_external_is_off(self):
        chunks = [_chunk("stripe notes")]
        _, seen, _ = await self.run_question("integrate Stripe payments webhooks", "local", send_external=False, chunks=chunks)
        self.assertTrue(seen["kw"]["require_trusted"])
        _, seen, _ = await self.run_question("integrate Stripe payments webhooks", "local", send_external=True, chunks=chunks)
        self.assertFalse(seen["kw"]["require_trusted"])

    async def test_no_records_means_unchanged_privacy_behaviour(self):
        _, seen, _ = await self.run_question("integrate Stripe payments webhooks", "cloud", send_external=False, chunks=[])
        self.assertFalse(seen["kw"]["require_trusted"])  # only the question leaves, as before

    async def test_plain_business_question_gets_no_skill_text(self):
        _, seen, _ = await self.run_question("what is the leave policy for employees", "local")
        system = seen["messages"][0]["content"]
        self.assertNotIn("<agent_skill", system)
        self.assertNotIn("CONNECTED SKILLS", system)




class BudgetTests(unittest.TestCase):
    def test_cloud_prompt_respects_total_budget(self):
        q = "make my landing page UI look professional and test it in the browser"
        text = skill_registry.prompt_context(q, "coding", "cloud")
        self.assertGreater(len(text), 0)
        self.assertLessEqual(len(text), skill_registry.BUDGETS["cloud"]["total"])


if __name__ == "__main__":
    unittest.main()

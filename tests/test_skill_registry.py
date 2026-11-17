import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import skill_registry


class SkillRegistryTests(unittest.TestCase):
    def test_discovers_and_selects_relevant_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "frontend" / "react-ui"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: react-ui\ndescription: Build accessible React frontend components and responsive UI.\n---\n\nUse semantic HTML, keyboard navigation, responsive layouts, and reusable components.",
                encoding="utf-8",
            )
            with patch.object(skill_registry, "SKILLS_ROOT", root):
                selected = skill_registry.select_skills("Build an accessible React frontend dashboard", "coding")
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0]["name"], "react-ui")
                prompt = skill_registry.prompt_context("Build an accessible React frontend dashboard", "coding")
                self.assertIn("<agent_skill", prompt)
                self.assertIn("semantic HTML", prompt)

    def test_catalog_is_loaded_from_json(self):
        entries = skill_registry.catalog_entries()
        self.assertGreaterEqual(len(entries), 15)
        self.assertTrue(any(item["id"] == "anthropics/frontend-design" for item in entries))

    def test_folded_frontmatter_description_is_parsed(self):
        meta, body = skill_registry._frontmatter("---\nname: x\ndescription: >\n  Writes pytest tests\n  with fixtures.\nlicense: MIT\n---\nbody")
        self.assertEqual(meta["description"], "Writes pytest tests with fixtures.")
        self.assertEqual(meta["license"], "MIT")

    def test_requested_community_skills_are_installed(self):
        ids = skill_registry.installed_catalog_ids()
        for wanted in [
            "anthropics/frontend-design", "anthropics/webapp-testing", "anthropics/mcp-builder",
            "testmu-ai/pytest-skill", "testmu-ai/playwright-skill", "testmu-ai/api-skill",
            "supabase/postgres-best-practices", "stripe/stripe-best-practices",
            "google-gemini/gemini-api-dev", "trailofbits/differential-review",
            "debpalash/voicestudio",
        ]:
            self.assertIn(wanted, ids)


if __name__ == "__main__":
    unittest.main()

import unittest

from app.intelligence import classify_task, guidance_for
from app.intelligence import rank_providers


class IntelligenceTests(unittest.TestCase):
    def test_task_classification(self):
        self.assertEqual(classify_task("Write a Python function to parse CSV"), "coding")
        self.assertEqual(classify_task("Build a mobile app with React Native"), "coding")
        self.assertEqual(classify_task("Analyze the architecture trade-offs"), "reasoning")
        self.assertEqual(classify_task("Write a poem about rain"), "creative")
        self.assertEqual(classify_task("What time is the meeting?"), "general")

    def test_guidance_is_task_specific(self):
        self.assertIn("executable", guidance_for("coding"))
        self.assertIn("Ground before mutating", guidance_for("coding"))
        self.assertIn("Completion needs fresh evidence", guidance_for("coding"))
        self.assertIn("assumptions", guidance_for("reasoning"))

    def test_coding_router_prefers_coding_model(self):
        providers = [
            {"name": "Local", "model": "qwen2.5:3b", "priority": 1},
            {"name": "Coder", "model": "qwen2.5-coder:14b", "priority": 50},
        ]
        ranked = rank_providers(providers, "coding")
        self.assertEqual(ranked[0]["name"], "Coder")

    def test_unknown_models_keep_priority(self):
        providers = [
            {"name": "Second", "model": "unknown-model-b", "priority": 20},
            {"name": "First", "model": "unknown-model-a", "priority": 10},
        ]
        ranked = rank_providers(providers, "general")
        self.assertEqual(ranked[0]["name"], "First")


if __name__ == "__main__":
    unittest.main()

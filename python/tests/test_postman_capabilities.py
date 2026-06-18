"""Tests for MCP prompts and resources (Postman capabilities)."""

from __future__ import annotations

import unittest

from neo_mcp import postman_capabilities as pc


class TestPostmanCapabilities(unittest.TestCase):
    def test_list_prompts_returns_five(self):
        names = {p.name for p in pc.list_prompts()}
        self.assertEqual(
            names,
            {
                "train-model",
                "fine-tune-classifier",
                "fix-training-run",
                "build-ml-pipeline",
                "benchmark-prompts",
            },
        )

    def test_get_prompt_train_model(self):
        result = pc.get_prompt(
            "train-model",
            {"dataset_path": "data/fraud.csv", "goal": "optimize for recall"},
        )
        text = result.messages[0].content.text  # type: ignore[union-attr]
        self.assertIn("data/fraud.csv", text)
        self.assertIn("optimize for recall", text)

    def test_get_prompt_unknown_raises(self):
        with self.assertRaises(ValueError):
            pc.get_prompt("nonexistent", {})

    def test_get_prompt_missing_required_arg(self):
        with self.assertRaises(ValueError):
            pc.get_prompt("train-model", {})

    def test_list_resources_returns_four(self):
        uris = {str(r.uri) for r in pc.list_resources()}
        self.assertEqual(
            uris,
            {
                "neo://docs/overview",
                "neo://docs/tools",
                "neo://docs/workflow",
                "neo://docs/env",
            },
        )

    def test_read_resource_tools(self):
        contents = list(pc.read_resource("neo://docs/tools"))
        self.assertEqual(len(contents), 1)
        self.assertIn("neo_submit_task", contents[0].text)
        self.assertEqual(contents[0].mimeType, "text/markdown")

    def test_read_resource_unknown_raises(self):
        with self.assertRaises(ValueError):
            list(pc.read_resource("neo://docs/missing"))


if __name__ == "__main__":
    unittest.main()

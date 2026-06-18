"""Tests for MCP prompts and resources (Postman capabilities)."""

from __future__ import annotations

import unittest

from neo_mcp import postman_capabilities as pc

EXPECTED_PROMPTS = {
    "train-model",
    "fine-tune-classifier",
    "fine-tune-llm",
    "build-rag-pipeline",
    "build-ai-agent",
    "fix-training-run",
    "build-ml-pipeline",
    "benchmark-prompts",
    "run-eda",
    "train-vision-model",
}

EXPECTED_RESOURCES = {
    "neo://docs/overview",
    "neo://docs/tools",
    "neo://docs/workflow",
    "neo://docs/env",
    "neo://docs/prompts",
}


class TestPostmanCapabilities(unittest.TestCase):
    def test_list_prompts_returns_ten(self):
        names = {p.name for p in pc.list_prompts()}
        self.assertEqual(names, EXPECTED_PROMPTS)

    def test_get_prompt_train_model_with_path(self):
        result = pc.get_prompt(
            "train-model",
            {"path": "data/fraud.csv", "goal": "optimize for recall"},
        )
        text = result.messages[0].content.text  # type: ignore[union-attr]
        self.assertIn("data/fraud.csv", text)
        self.assertIn("optimize for recall", text)

    def test_get_prompt_train_model_default_path(self):
        result = pc.get_prompt("train-model", {})
        text = result.messages[0].content.text  # type: ignore[union-attr]
        self.assertIn("main dataset in the workspace", text)

    def test_get_prompt_rag_default_path(self):
        result = pc.get_prompt("build-rag-pipeline", {})
        text = result.messages[0].content.text  # type: ignore[union-attr]
        self.assertIn("./docs", text)

    def test_get_prompt_unknown_raises(self):
        with self.assertRaises(ValueError):
            pc.get_prompt("nonexistent", {})

    def test_get_prompt_build_ai_agent_requires_description(self):
        with self.assertRaises(ValueError):
            pc.get_prompt("build-ai-agent", {})

    def test_list_resources_returns_five(self):
        uris = {str(r.uri) for r in pc.list_resources()}
        self.assertEqual(uris, EXPECTED_RESOURCES)

    def test_read_resource_tools_returns_read_resource_contents(self):
        contents = list(pc.read_resource("neo://docs/tools"))
        self.assertEqual(len(contents), 1)
        self.assertIn("neo_submit_task", contents[0].content)
        self.assertEqual(contents[0].mime_type, "text/markdown")

    def test_read_resource_prompts_catalog(self):
        contents = list(pc.read_resource("neo://docs/prompts"))
        self.assertIn("build-rag-pipeline", contents[0].content)

    def test_read_resource_unknown_raises(self):
        with self.assertRaises(ValueError):
            list(pc.read_resource("neo://docs/missing"))


if __name__ == "__main__":
    unittest.main()

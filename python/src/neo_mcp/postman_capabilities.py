"""MCP prompts and resources for Postman and other MCP clients."""

from __future__ import annotations

from importlib import resources
from typing import Callable, Iterable

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

_RESOURCE_FILES: dict[str, tuple[str, str]] = {
    "neo://docs/overview": ("Neo MCP Overview", "overview.md"),
    "neo://docs/tools": ("Tool Reference", "tools.md"),
    "neo://docs/workflow": ("Typical Workflow", "workflow.md"),
    "neo://docs/env": ("Environment Variables", "env.md"),
    "neo://docs/prompts": ("Example Prompts", "prompts.md"),
}

_PROMPT_BUILDERS: dict[str, tuple[types.Prompt, Callable[[dict[str, str]], str]]] = {}


def _opt_path(args: dict[str, str], default: str) -> str:
    return args.get("path") or default


def _register_prompt(
    spec: types.Prompt,
    builder: Callable[[dict[str, str]], str],
) -> None:
    _PROMPT_BUILDERS[spec.name] = (spec, builder)


def _path_arg(description: str) -> types.PromptArgument:
    return types.PromptArgument(
        name="path",
        description=description,
        required=False,
    )


def _goal_arg(description: str) -> types.PromptArgument:
    return types.PromptArgument(
        name="goal",
        description=description,
        required=False,
    )


_register_prompt(
    types.Prompt(
        name="train-model",
        description="Train an ML model on tabular data (classification, regression, fraud detection)",
        arguments=[
            _path_arg("Workspace-relative dataset path, e.g. data/fraud.csv"),
            _goal_arg("Metric or objective, e.g. optimize for recall"),
        ],
    ),
    lambda a: (
        f"Use Neo to train a machine learning model on {_opt_path(a, 'the main dataset in the workspace')}. "
        f"{a.get('goal') or 'Evaluate metrics, save the model and a short report to the workspace.'}"
    ),
)

_register_prompt(
    types.Prompt(
        name="fine-tune-classifier",
        description="Fine-tune a text classifier with cross-validation",
        arguments=[_path_arg("Workspace-relative path to labeled text training data")],
    ),
    lambda a: (
        f"Use Neo to fine-tune a text classifier on {_opt_path(a, 'labeled text data in the workspace')} "
        "with 5-fold cross-validation. Report accuracy, F1, and save the best checkpoint."
    ),
)

_register_prompt(
    types.Prompt(
        name="fine-tune-llm",
        description="Fine-tune an open LLM (Llama, Qwen, Gemma) on local instruction or completion data",
        arguments=[
            types.PromptArgument(
                name="base_model",
                description="Exact HuggingFace or model ID to fine-tune",
                required=False,
            ),
            _path_arg("Workspace-relative training JSONL/CSV or dataset folder"),
            _goal_arg("Training goal or eval metric"),
        ],
    ),
    lambda a: (
        f"Use Neo to fine-tune {a.get('base_model') or 'an open LLM you select from the workspace context'} "
        f"on {_opt_path(a, 'instruction or completion data in the workspace')}. "
        f"{a.get('goal') or 'Use LoRA or QLoRA where appropriate, log loss, and save adapters to the workspace.'}"
    ),
)

_register_prompt(
    types.Prompt(
        name="build-rag-pipeline",
        description="Build a RAG pipeline: ingest documents, chunk, embed, retrieve, and answer questions",
        arguments=[
            _path_arg("Workspace-relative docs folder (PDF, markdown, etc.)"),
            _goal_arg("Embedding model, vector store, or eval criteria"),
        ],
    ),
    lambda a: (
        f"Use Neo to build a RAG pipeline over documents at {_opt_path(a, './docs')}. "
        f"{a.get('goal') or 'Include ingestion, chunking, embeddings, vector store, and a minimal query API with citations.'}"
    ),
)

_register_prompt(
    types.Prompt(
        name="build-ai-agent",
        description="Build an AI agent workflow with tools, memory, and evaluation hooks",
        arguments=[
            types.PromptArgument(
                name="description",
                description="Agent goal, tools, data sources, and constraints",
                required=True,
            ),
        ],
    ),
    lambda a: (
        f"Use Neo to build an AI agent: {a['description']}. "
        "Include tool definitions, a runnable entrypoint, and basic eval or smoke tests."
    ),
)

_register_prompt(
    types.Prompt(
        name="fix-training-run",
        description="Debug and re-run a failing ML or LLM training job with logging",
        arguments=[
            types.PromptArgument(
                name="context",
                description="Error logs, stack trace, or what failed",
                required=True,
            ),
        ],
    ),
    lambda a: (
        "Use Neo to fix the failing training run and re-run with full logging. "
        f"Context: {a['context']}"
    ),
)

_register_prompt(
    types.Prompt(
        name="build-ml-pipeline",
        description="Build or debug an end-to-end ML pipeline (ETL → train → evaluate → export)",
        arguments=[
            types.PromptArgument(
                name="description",
                description="Pipeline goal, data sources, and constraints",
                required=True,
            ),
        ],
    ),
    lambda a: f"Use Neo to build or debug an end-to-end ML pipeline: {a['description']}",
)

_register_prompt(
    types.Prompt(
        name="benchmark-prompts",
        description="Benchmark LLM prompts on an evaluation set (accuracy, latency, cost)",
        arguments=[_path_arg("Workspace-relative eval JSON/CSV with prompts and expected outputs")],
    ),
    lambda a: (
        f"Use Neo to benchmark these prompts on our eval set at "
        f"{_opt_path(a, 'the evaluation dataset in the workspace')}. "
        "Report metrics and save results to the workspace."
    ),
)

_register_prompt(
    types.Prompt(
        name="run-eda",
        description="Exploratory data analysis with visualizations and a written summary",
        arguments=[
            _path_arg("Workspace-relative CSV, Parquet, or dataset file"),
            _goal_arg("Focus, e.g. missing values, correlations, outliers"),
        ],
    ),
    lambda a: (
        f"Use Neo to run exploratory data analysis on {_opt_path(a, 'the main dataset in the workspace')}. "
        f"{a.get('goal') or 'Produce summary stats, key plots, and a markdown report in the workspace.'}"
    ),
)

_register_prompt(
    types.Prompt(
        name="train-vision-model",
        description="Train a computer vision model (classification, detection, or OCR)",
        arguments=[
            _path_arg("Workspace-relative image dataset or annotations"),
            _goal_arg("Task type or metric, e.g. mAP, top-1 accuracy"),
        ],
    ),
    lambda a: (
        f"Use Neo to train a computer vision model on {_opt_path(a, 'image data in the workspace')}. "
        f"{a.get('goal') or 'Train, evaluate, and save weights plus a brief eval report.'}"
    ),
)


def list_prompts() -> list[types.Prompt]:
    return [spec for spec, _ in _PROMPT_BUILDERS.values()]


def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    if name not in _PROMPT_BUILDERS:
        raise ValueError(f"Unknown prompt: {name}")
    spec, builder = _PROMPT_BUILDERS[name]
    args = arguments or {}
    for arg in spec.arguments or []:
        if arg.required and not args.get(arg.name):
            raise ValueError(f"Missing required argument: {arg.name}")
    text = builder(args)
    return types.GetPromptResult(
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )
        ]
    )


def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri=uri,
            name=title,
            description=title,
            mimeType="text/markdown",
        )
        for uri, (title, _) in _RESOURCE_FILES.items()
    ]


def _load_resource_markdown(filename: str) -> str:
    return (
        resources.files("neo_mcp.postman").joinpath(filename).read_text(encoding="utf-8")
    )


def read_resource(uri: str) -> Iterable[ReadResourceContents]:
    entry = _RESOURCE_FILES.get(uri)
    if entry is None:
        raise ValueError(f"Unknown resource: {uri}")
    _, filename = entry
    return [
        ReadResourceContents(
            content=_load_resource_markdown(filename),
            mime_type="text/markdown",
        )
    ]


def read_resource_url(uri: AnyUrl) -> Iterable[ReadResourceContents]:
    return read_resource(str(uri))

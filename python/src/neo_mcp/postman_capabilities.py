"""MCP prompts and resources for Postman and other MCP clients."""

from __future__ import annotations

from importlib import resources
from typing import Callable, Iterable

import mcp.types as types
from pydantic import AnyUrl

_RESOURCE_FILES: dict[str, tuple[str, str]] = {
    "neo://docs/overview": ("Neo MCP Overview", "overview.md"),
    "neo://docs/tools": ("Tool Reference", "tools.md"),
    "neo://docs/workflow": ("Typical Workflow", "workflow.md"),
    "neo://docs/env": ("Environment Variables", "env.md"),
}

_PROMPT_SPECS: list[types.Prompt] = [
    types.Prompt(
        name="train-model",
        description="Train an ML model on a local dataset using Neo",
        arguments=[
            types.PromptArgument(
                name="dataset_path",
                description="Path to CSV or data file",
                required=True,
            ),
            types.PromptArgument(
                name="goal",
                description="Optimization goal, e.g. optimize for recall",
                required=False,
            ),
        ],
    ),
    types.Prompt(
        name="fine-tune-classifier",
        description="Fine-tune a text classifier with cross-validation",
        arguments=[
            types.PromptArgument(
                name="data_path",
                description="Path to training data",
                required=True,
            ),
        ],
    ),
    types.Prompt(
        name="fix-training-run",
        description="Debug and re-run a failing training job with logging",
        arguments=[
            types.PromptArgument(
                name="context",
                description="What failed or relevant error output",
                required=True,
            ),
        ],
    ),
    types.Prompt(
        name="build-ml-pipeline",
        description="Build or debug an end-to-end ML pipeline",
        arguments=[
            types.PromptArgument(
                name="description",
                description="Pipeline goal, data sources, and constraints",
                required=True,
            ),
        ],
    ),
    types.Prompt(
        name="benchmark-prompts",
        description="Benchmark prompts on an evaluation set",
        arguments=[
            types.PromptArgument(
                name="eval_set_path",
                description="Path to eval dataset or prompt set",
                required=True,
            ),
        ],
    ),
]


def list_prompts() -> list[types.Prompt]:
    return list(_PROMPT_SPECS)


def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    args = arguments or {}
    spec_by_name = {p.name: p for p in _PROMPT_SPECS}
    if name not in spec_by_name:
        raise ValueError(f"Unknown prompt: {name}")
    for arg in spec_by_name[name].arguments or []:
        if arg.required and not args.get(arg.name):
            raise ValueError(f"Missing required argument: {arg.name}")

    builders: dict[str, Callable[[], str]] = {
        "train-model": lambda: (
            f"Use Neo to train a model on data at {args['dataset_path']}. "
            f"{args.get('goal', 'Evaluate metrics and save the model to the workspace.')}"
        ),
        "fine-tune-classifier": lambda: (
            f"Use Neo to fine-tune a text classifier on {args['data_path']} "
            "with 5-fold cross-validation."
        ),
        "fix-training-run": lambda: (
            "Use Neo to fix the failing training run and re-run with logging. "
            f"Context: {args['context']}"
        ),
        "build-ml-pipeline": lambda: (
            f"Use Neo to build or debug an end-to-end ML pipeline: {args['description']}"
        ),
        "benchmark-prompts": lambda: (
            f"Use Neo to benchmark these prompts on our eval set at {args['eval_set_path']}"
        ),
    }
    text = builders[name]()
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


def read_resource(uri: str) -> Iterable[types.TextResourceContents]:
    entry = _RESOURCE_FILES.get(uri)
    if entry is None:
        raise ValueError(f"Unknown resource: {uri}")
    _, filename = entry
    return [
        types.TextResourceContents(
            uri=uri,
            mimeType="text/markdown",
            text=_load_resource_markdown(filename),
        )
    ]


def read_resource_url(uri: AnyUrl) -> Iterable[types.TextResourceContents]:
    return read_resource(str(uri))

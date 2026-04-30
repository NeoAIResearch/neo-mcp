"""Schema definitions per integration provider — validation + metadata.

Mirrors vscode_extension/.../integrations/registry.ts in shape. Add a new
provider by appending an ``IntegrationSchema`` entry to ``PROVIDERS`` and a
matching module under ``providers/``.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IntegrationSchema:
    name: str
    description: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()
    validators: dict[str, str] = field(default_factory=dict)
    method: str = "api_key"


PROVIDERS: dict[str, IntegrationSchema] = {
    "github": IntegrationSchema(
        name="github",
        description="GitHub PAT for cloning private repos and pushing commits",
        required_fields=("pat",),
        optional_fields=("username",),
        validators={"pat": r"^(ghp_|github_pat_)[A-Za-z0-9_]+$"},
        method="pat",
    ),
    "huggingface": IntegrationSchema(
        name="huggingface",
        description="Hugging Face Hub token for private models and datasets",
        required_fields=("token",),
        validators={"token": r"^hf_[A-Za-z0-9_\-]+$"},
        method="token",
    ),
    "anthropic": IntegrationSchema(
        name="anthropic",
        description="Anthropic API key for Claude models",
        required_fields=("api_key",),
        validators={"api_key": r"^sk-ant-[A-Za-z0-9\-_]+$"},
        method="api_key",
    ),
    "openrouter": IntegrationSchema(
        name="openrouter",
        description="OpenRouter API key for multi-provider LLM access",
        required_fields=("api_key",),
        validators={"api_key": r"^sk-or-[A-Za-z0-9\-_]+$"},
        method="api_key",
    ),
    "s3": IntegrationSchema(
        name="s3",
        description="AWS S3 credentials for accessing buckets and datasets",
        required_fields=("aws_access_key_id", "aws_secret_access_key"),
        optional_fields=("region",),
        validators={"aws_access_key_id": r"^AKIA[0-9A-Z]{16}$"},
        method="access_key",
    ),
    "wandb": IntegrationSchema(
        name="wandb",
        description="Weights & Biases API key for experiment tracking",
        required_fields=("api_key",),
        validators={"api_key": r"^[A-Za-z0-9]{40}$"},
        method="api_key",
    ),
    "kaggle": IntegrationSchema(
        name="kaggle",
        description="Kaggle API credentials for datasets and competitions",
        required_fields=("username", "key"),
        validators={"key": r"^[a-f0-9]{32}$"},
        method="api_key",
    ),
    "openai": IntegrationSchema(
        name="openai",
        description="OpenAI API key for GPT models and embeddings",
        required_fields=("api_key",),
        validators={"api_key": r"^sk-[A-Za-z0-9_\-]+$"},
        method="api_key",
    ),
}

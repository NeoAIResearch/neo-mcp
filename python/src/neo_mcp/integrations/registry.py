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
}

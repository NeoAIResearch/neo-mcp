"""Per-provider credential modules. Each exposes:

- ``write_secret(credentials) -> dict`` — persist to native credential file(s)
- ``remove_secret() -> list[str]`` — delete those files, return removed paths
- ``load_env() -> dict[str, str]`` — env vars to inject into Neo subprocesses
- ``async test_connection() -> tuple[bool, str, int]`` — (ok, message, latency_ms)
"""

from . import anthropic, github, huggingface, kaggle, openai, openrouter, s3, wandb

MODULES = {
    "github": github,
    "huggingface": huggingface,
    "anthropic": anthropic,
    "openrouter": openrouter,
    "s3": s3,
    "wandb": wandb,
    "kaggle": kaggle,
    "openai": openai,
}

__all__ = ["MODULES", "github", "huggingface", "anthropic", "openrouter", "s3", "wandb", "kaggle", "openai"]

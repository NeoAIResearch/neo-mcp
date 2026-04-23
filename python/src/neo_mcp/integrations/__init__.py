"""Third-party integration credentials (GitHub, HuggingFace, Anthropic, OpenRouter, ...).

Mirrors the shape of the VS Code extension's IntegrationManager but stores
secrets in each provider's native credential file rather than VS Code's
SecretStorage. Metadata about *what* is configured lives in the shared
file ``~/.neo/integrations.json`` so the extension and pip server see the
same list.
"""

from .manager import IntegrationManager, ValidationError
from .registry import PROVIDERS, IntegrationSchema

__all__ = ["IntegrationManager", "ValidationError", "PROVIDERS", "IntegrationSchema"]

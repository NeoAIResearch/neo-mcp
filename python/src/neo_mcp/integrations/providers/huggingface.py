"""HuggingFace token.

Canonical store: SecretStore (keyring or 0o600 file). In addition we also
write the native file at ``~/.cache/huggingface/token`` so ``huggingface-cli``,
``datasets``, and ``transformers`` pick it up automatically without needing
us to inject env vars. The SecretStore remains the source of truth for
``remove_secret`` and for rebuilding the native file.
"""

from pathlib import Path

from .._fsutil import atomic_write_secret
from ..secret_store import get_secret_store

PROVIDER = "huggingface"
FIELDS = ("token",)

TOKEN_FILE: Path = Path.home() / ".cache" / "huggingface" / "token"


def _write_native_file(token: str) -> None:
    atomic_write_secret(TOKEN_FILE, token)


def write_secret(credentials: dict) -> dict:
    token = credentials["token"]
    store = get_secret_store()
    store.write(PROVIDER, {"token": token})
    _write_native_file(token)
    return {
        "files_written": [store.location(PROVIDER), str(TOKEN_FILE)],
        "backend": store.backend,
    }


def remove_secret() -> list[str]:
    store = get_secret_store()
    removed: list[str] = []
    if store.delete(PROVIDER, FIELDS):
        removed.append(store.location(PROVIDER))
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        removed.append(str(TOKEN_FILE))
    return removed


def load_env() -> dict[str, str]:
    creds = get_secret_store().read(PROVIDER, FIELDS)
    token = creds.get("token")
    if not token and TOKEN_FILE.exists():
        # Fallback: user provisioned the native file directly (e.g. via
        # `huggingface-cli login`) without going through neo_add_integration.
        token = TOKEN_FILE.read_text().strip() or None
    if not token:
        return {}
    return {"HF_TOKEN": token, "HUGGING_FACE_HUB_TOKEN": token}


async def test_connection() -> tuple[bool, str, int]:
    from ._http import probe
    env = load_env()
    token = env.get("HF_TOKEN")
    if not token:
        return False, "huggingface not configured", 0
    return await probe(
        "GET",
        "https://huggingface.co/api/whoami-v2",
        {"Authorization": f"Bearer {token}"},
    )

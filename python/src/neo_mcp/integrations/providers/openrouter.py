"""OpenRouter API key — stored via SecretStore (keyring or 0o600 file)."""

from ..secret_store import get_secret_store

PROVIDER = "openrouter"
FIELDS = ("api_key",)


def write_secret(credentials: dict) -> dict:
    store = get_secret_store()
    store.write(PROVIDER, {"api_key": credentials["api_key"]})
    return {"files_written": [store.location(PROVIDER)], "backend": store.backend}


def remove_secret() -> list[str]:
    store = get_secret_store()
    loc = store.location(PROVIDER)
    removed = store.delete(PROVIDER, FIELDS)
    return [loc] if removed else []


def load_env() -> dict[str, str]:
    creds = get_secret_store().read(PROVIDER, FIELDS)
    key = creds.get("api_key")
    return {"OPENROUTER_API_KEY": key} if key else {}


async def test_connection() -> tuple[bool, str, int]:
    from ._http import probe
    env = load_env()
    key = env.get("OPENROUTER_API_KEY")
    if not key:
        return False, "openrouter not configured", 0
    return await probe(
        "GET",
        "https://openrouter.ai/api/v1/auth/key",
        {"Authorization": f"Bearer {key}"},
    )

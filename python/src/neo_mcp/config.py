"""Runtime configuration derived from environment variables and ~/.neo/settings.json."""

import json
import os
import sys

from .paths import SETTINGS_FILE

_PROD_URL = "https://master.heyneo.com"
_STAGING_URL = "https://alpha.heyneo.com"


def _url_for_env(value: str) -> str | None:
    v = value.strip().lower()
    if v == "staging":
        return _STAGING_URL
    if v in ("prod", "production"):
        return _PROD_URL
    return None


def _env_from_settings_file() -> str | None:
    """Read `env` key from ~/.neo/settings.json. Missing/malformed → None."""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        print(f"[neo-mcp] Warning: could not parse {SETTINGS_FILE}: {e}", file=sys.stderr)
        return None
    if isinstance(data, dict):
        env = data.get("env")
        if isinstance(env, str):
            return env
    return None


def _resolve_api_url() -> str:
    """Resolve API base URL with precedence: settings.json > NEO_ENVIRONMENT/NEO_ENV > NEO_API_URL > prod default."""
    settings_env = _env_from_settings_file()
    if settings_env is not None:
        url = _url_for_env(settings_env)
        if url is not None:
            return url
    env_var = os.environ.get("NEO_ENVIRONMENT") or os.environ.get("NEO_ENV")
    if env_var:
        url = _url_for_env(env_var)
        if url is not None:
            return url
    explicit = os.environ.get("NEO_API_URL")
    if explicit:
        return explicit
    return _PROD_URL


API_URL: str = _resolve_api_url()

# Poll parameters (mirroring BackendPoller.ts defaults)
POLL_MAX_MESSAGES: int = 10
POLL_WAIT_TIME: int = 5           # seconds — backend long-poll window
POLL_BASE_INTERVAL: float = 2.0   # seconds between poll cycles
POLL_MAX_INTERVAL: float = 60.0   # cap for exponential backoff
POLL_BACKOFF_FACTOR: float = 1.5  # multiplier per consecutive error

# Request timeout for poll calls (must exceed POLL_WAIT_TIME)
POLL_TIMEOUT: float = 12.0        # seconds
REQUEST_TIMEOUT: float = 30.0     # seconds for all other requests

# Auto-pause: threads still RUNNING or WAITING_FOR_FEEDBACK after this many hours
# are automatically paused. Set NEO_TASK_TIMEOUT_HOURS=0 to disable.
TASK_TIMEOUT_HOURS: float = float(os.environ.get("NEO_TASK_TIMEOUT_HOURS", "6"))
TASK_TIMEOUT_CHECK_INTERVAL: float = 300.0  # seconds between checks (5 min)

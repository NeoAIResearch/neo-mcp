"""Runtime configuration derived from environment variables."""

import os

# API base URL — production by default, staging via NEO_ENVIRONMENT=staging
_env = os.environ.get("NEO_ENVIRONMENT", os.environ.get("NEO_ENV", "prod")).lower()
API_URL: str = (
    "https://alpha.heyneo.so" if _env == "staging" else "https://master.heyneo.so"
)

# Poll parameters (mirroring BackendPoller.ts defaults)
POLL_MAX_MESSAGES: int = 10
POLL_WAIT_TIME: int = 5           # seconds — backend long-poll window
POLL_BASE_INTERVAL: float = 2.0   # seconds between poll cycles
POLL_MAX_INTERVAL: float = 60.0   # cap for exponential backoff
POLL_BACKOFF_FACTOR: float = 1.5  # multiplier per consecutive error

# Request timeout for poll calls (must exceed POLL_WAIT_TIME)
POLL_TIMEOUT: float = 12.0        # seconds
REQUEST_TIMEOUT: float = 30.0     # seconds for all other requests

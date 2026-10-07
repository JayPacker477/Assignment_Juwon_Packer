import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
ANTHROPIC_PARSE_MODEL = os.getenv("ANTHROPIC_PARSE_MODEL", "claude-haiku-4-5")

# api.weather.gov requires an identifying User-Agent.
USER_AGENT = os.getenv(
    "HTTP_USER_AGENT", "travel-disruption-prototype/0.1 (ops-demo; contact: ops@example.com)"
)
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "12"))
CACHE_TTL_S = int(os.getenv("CACHE_TTL_S", "300"))

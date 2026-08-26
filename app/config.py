"""Runtime settings + policy loading."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Load .env from the project root when running outside Docker (uvicorn, pytest).
# In Docker, compose injects the environment directly and this is a no-op.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # python-dotenv is optional
    pass


class Settings:
    def __init__(self) -> None:
        self.policy_path = Path(os.getenv("HERMES_POLICY", ROOT / "config" / "policy.yaml"))
        # Text and vision reasoning — the evidence layer. NOT image generation.
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "")
        self.llm_enabled = os.getenv("HERMES_LLM_ENABLED", "true").lower() == "true"

        # IMAGE GENERATION runs on its own key, and only on this one.
        #
        # Separate from GEMINI_API_KEY because the two have separate quotas and
        # separate costs: a rules-only deployment needs no image key at all, and
        # exhausting the image quota must not take the evidence layer down with
        # it. vnyx-api draws the same line — `GOOGLE_NANO_BANANA_API_KEY` in
        # utils/rate-limiter.ts, distinct from every other Gemini call it makes.
        #
        # COMMA-SEPARATED, rotated round-robin, exactly as that file parses it.
        # The rotation is load-bearing here rather than decorative: four views
        # generate concurrently and each retry adds another call, so a single key
        # meets its per-minute limit quickly.
        self.nano_banana_keys = [
            k.strip()
            for k in os.getenv("GOOGLE_NANO_BANANA_API_KEY", "").split(",")
            if k.strip()
        ]

        # VNYX write-back.
        #
        # The API is a SEPARATE HOST with no path prefix: `api-dev.vnyx.ai`, whose
        # routes are `/products`, `/categories`, `/sizes`, `/brands`. The previous
        # default of `https://dev.vnyx.ai/api` pointed at the Next.js frontend on
        # a path that does not exist there, so every fetch and write 404'd.
        # dev.vnyx.ai serves the UI; api-dev.vnyx.ai serves this.
        self.vnyx_base_url = os.getenv("VNYX_BASE_URL", "https://api-dev.vnyx.ai")
        self.vnyx_token = os.getenv("VNYX_API_TOKEN", "")
        self.vnyx_timeout_s = float(os.getenv("VNYX_TIMEOUT_S", "20"))

        # Safety switches
        self.dry_run = os.getenv("HERMES_DRY_RUN", "false").lower() == "true"
        # Relative default so this works on Windows without an absolute path.
        # Docker overrides it to /var/log/hermes/audit.jsonl via compose.
        self.audit_path = Path(os.getenv("HERMES_AUDIT_PATH", ROOT / "logs" / "audit.jsonl"))
        self.webhook_secret = os.getenv("HERMES_WEBHOOK_SECRET", "")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def policy() -> dict[str, Any]:
    with open(settings().policy_path) as fh:
        return yaml.safe_load(fh)


def reload_policy() -> dict[str, Any]:
    policy.cache_clear()
    return policy()

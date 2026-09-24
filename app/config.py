"""Runtime settings + policy loading."""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

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

        # Direct Postgres, for /v1/product-audit only.
        #
        # Every other endpoint takes its data in the request body and holds no
        # credentials — that is the contract that keeps Hermes unable to reach a
        # product the caller was not already authorised to read. The audit
        # endpoint deliberately breaks it: its whole purpose is to answer "what
        # is wrong with this id" with nothing but a connection string, no running
        # vnyx-api and no JWT. Unset means the endpoint 503s rather than the
        # service failing to boot.
        self.database_url = os.getenv("DATABASE_URL", "")

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
def _base_policy() -> dict[str, Any]:
    with open(settings().policy_path) as fh:
        return yaml.safe_load(fh)


# ─── Per-run policy overlay ─────────────────────────────────────────────────
#
# THE TENANT'S BRAIN, applied to one verification. policy.yaml is shared by
# every tenant; vnyx-api compiles a tenant's Brain checks into a DEEP OVERLAY
# of the same keys (services/auto-approval/checks.ts — the gate's block_on, the
# photo audit's actions, the copy step's trigger fields) and the worker sets it
# around repair(). Every `policy()` call inside that block sees the merged
# policy; every call outside it sees policy.yaml unchanged.
#
# A ContextVar, not a global: `run_from_sheet --workers` verifies several
# products on threads at once, possibly for different tenants, and each thread
# starts from the default context — one tenant's overlay cannot leak into
# another's product. Merged ONCE on entry, so the hot path (policy() is called
# per rule, per product) stays a single lookup.
_OVERLAY: ContextVar[dict[str, Any] | None] = ContextVar("policy_overlay", default=None)


def policy() -> dict[str, Any]:
    merged = _OVERLAY.get()
    return merged if merged is not None else _base_policy()


# reload_policy and the tests clear the cache through the public name.
policy.cache_clear = _base_policy.cache_clear  # type: ignore[attr-defined]


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """`overlay` onto `base`, recursively for dicts; anything else replaces.

    Lists REPLACE rather than extend — `quality_gate.block_on` from a tenant is
    the whole list it wants, not additions to policy.yaml's. Neither input is
    mutated; untouched subtrees are shared with `base`, which is what
    `policy()` returned before and is equally read-only by convention.
    """
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@contextmanager
def policy_overlay(overlay: dict[str, Any] | None) -> Iterator[dict[str, Any]]:
    """Apply `overlay` to every policy() read inside the block. Nests.

    An empty or absent overlay is a no-op, so a snapshot written before the
    Brain checks existed runs on policy.yaml exactly as it always did.
    """
    if not overlay:
        yield policy()
        return
    token = _OVERLAY.set(deep_merge(policy(), overlay))
    try:
        yield _OVERLAY.get()  # type: ignore[misc]
    finally:
        _OVERLAY.reset(token)


def reload_policy() -> dict[str, Any]:
    policy.cache_clear()  # type: ignore[attr-defined]
    return policy()

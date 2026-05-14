"""Supabase client wrapper.

Two flavors of client:
  - service_client(): full access, used by ingest/seed/analyze. Reads
    SUPABASE_SERVICE_ROLE_KEY. Never use this in anything that runs in a browser.
  - anon_client(): read-only (per RLS policies). Used when we want to dry-run
    what the dashboard will see.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(override=True)


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing env var {name}. Copy .env.example to .env and fill it in."
        )
    return val


@lru_cache(maxsize=1)
def service_client() -> Client:
    return create_client(_require("SUPABASE_URL"), _require("SUPABASE_SERVICE_ROLE_KEY"))


@lru_cache(maxsize=1)
def anon_client() -> Client:
    return create_client(_require("SUPABASE_URL"), _require("SUPABASE_ANON_KEY"))

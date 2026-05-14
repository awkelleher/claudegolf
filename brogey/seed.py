"""One-shot script: load the existing trackman_shots.csv into Supabase.

Idempotent — re-running won't create duplicate sessions thanks to the
(source, external_id) unique constraint, and shots have (session_id, shot_num)
unique. We upsert on those keys.

Run:  python -m brogey.seed [path/to/trackman_shots.csv]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

from brogey.db import service_client

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "trackman_shots.csv"


def _clean(v):
    """Convert pandas NaN to None so Postgres gets real NULLs."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def seed(csv_path: Path) -> None:
    df = pd.read_csv(csv_path)
    print(f"Loaded {len(df)} rows from {csv_path}")

    sb = service_client()

    # --- sessions: one row per unique (session_date, trackman session_id) ---
    sessions_df = (
        df[["session_date", "session_id"]]
        .drop_duplicates()
        .sort_values("session_date")
        .reset_index(drop=True)
    )
    print(f"Found {len(sessions_df)} unique sessions")

    session_rows = [
        {
            "session_date": row.session_date,
            "source": "trackman",
            "external_id": row.session_id,
        }
        for row in sessions_df.itertuples(index=False)
    ]

    # Upsert on (source, external_id). Supabase returns the inserted/updated rows.
    resp = (
        sb.table("sessions")
        .upsert(session_rows, on_conflict="source,external_id")
        .execute()
    )
    # Build map: trackman_session_uuid -> our sessions.id
    external_to_id = {r["external_id"]: r["id"] for r in resp.data}
    print(f"Upserted {len(resp.data)} sessions")

    # --- shots ---
    shot_rows = []
    for r in df.itertuples(index=False):
        sid = external_to_id.get(r.session_id)
        if not sid:
            print(f"  warn: no session id for trackman uuid {r.session_id}; skipping")
            continue
        shot_rows.append(
            {
                "session_id": sid,
                "shot_num": int(r.shot_num),
                "club": str(r.club),
                "club_speed_mps": _clean(r.club_speed_mps),
                "attack_angle_deg": _clean(r.attack_angle_deg),
                "ball_speed_mps": _clean(r.ball_speed_mps),
                "spin_rate_rpm": _clean(r.spin_rate_rpm),
                "carry_m": _clean(r.carry_m),
                "side_m": _clean(r.side_m),
                "smash_factor": _clean(r.smash_factor),
            }
        )

    # Chunk to keep requests modest.
    CHUNK = 200
    total = 0
    for i in range(0, len(shot_rows), CHUNK):
        chunk = shot_rows[i : i + CHUNK]
        sb.table("shots").upsert(chunk, on_conflict="session_id,club,shot_num").execute()
        total += len(chunk)
        print(f"  upserted {total}/{len(shot_rows)} shots")

    print("done.")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    seed(path)

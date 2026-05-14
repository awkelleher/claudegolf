"""TopTracer CSV ingester.

TopTracer doesn't expose a share API (yet), so the workflow is:
  1. Screenshot shots in the TopTracer app.
  2. Have a model (ChatGPT, or eventually Claude vision here) parse into CSV.
  3. Drop the CSV into the project and run `brogey ingest-toptracer <csv>`.

The CSV is expected to have these columns (all imperial — TopTracer's native units):
  shot_number, flat_carry_yd, distance_trend_yd, ball_speed_mph,
  launch_angle_deg, height_yd, landing_angle_deg, hang_time_s,
  curve_direction, curve_yd, offline_direction, offline_yd,
  curve_signed_yd, offline_signed_yd

Optional: a `club` column. If absent, the club is inferred from the
filename (e.g. `..._driver_shots_...csv` -> Dr).

What TopTracer doesn't measure (vs TrackMan): club speed, attack angle,
spin rate, club path, face angle, dynamic loft, smash factor. Those
stay NULL in the DB — Brogey treats them as missing, not zero.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date
from pathlib import Path

import pandas as pd

from brogey.db import service_client

# Unit conversions (imperial -> metric, the canonical storage units)
YD_TO_M = 0.9144
MPH_TO_MPS = 0.44704


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stable_external_id(csv_path: Path) -> str:
    """Deterministic ID from filename — same file re-ingests cleanly."""
    h = hashlib.md5(csv_path.name.lower().encode()).hexdigest()
    return f"tt-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


_DATE_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[_-]?(\d{1,2})[_-]?(\d{4})", re.I)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _date_from_filename(name: str) -> str | None:
    m = _DATE_RE.search(name.lower())
    if not m:
        return None
    mon, day, year = m.groups()
    return date(int(year), _MONTHS[mon[:3]], int(day)).isoformat()


def _club_from_filename(name: str) -> str:
    n = name.lower()
    for token, club in [
        ("driver", "Dr"),
        ("3wood", "5W"),  # user mislabel correction (see prior chat)
        ("5wood", "5W"),
        ("7wood", "7i"),  # user mislabel correction
        ("3w", "5W"),
        ("5w", "5W"),
        ("7w", "7i"),
        ("9iron", "9i"),
        ("8iron", "8i"),
        ("7iron", "7i"),
        ("6iron", "6i"),
        ("5iron", "5i"),
        ("4iron", "4i"),
        ("3iron", "3i"),
        ("pw", "PW"),
        ("sw", "SW"),
        ("gw", "GW"),
        ("lw", "LW"),
    ]:
        if token in n:
            return club
    return "Unknown"


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------
def ingest_toptracer_csv(
    csv_path: Path,
    session_date: str | None = None,
    club_override: str | None = None,
) -> tuple[str, int]:
    """Load a TopTracer CSV into Supabase. Returns (session_id, n_shots)."""
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    df = pd.read_csv(csv_path)

    session_date = session_date or _date_from_filename(csv_path.name)
    if not session_date:
        raise ValueError(
            f"Couldn't infer session date from filename {csv_path.name!r}. "
            "Pass session_date='YYYY-MM-DD'."
        )

    default_club = club_override or _club_from_filename(csv_path.name)
    has_club_col = "club" in df.columns

    sb = service_client()
    external_id = _stable_external_id(csv_path)

    sess = (
        sb.table("sessions")
        .upsert(
            {
                "session_date": session_date,
                "source": "toptracer",
                "external_id": external_id,
                "pdf_filename": csv_path.name,  # repurpose: filename of origin
            },
            on_conflict="source,external_id",
        )
        .execute()
    )
    session_id = sess.data[0]["id"]

    # Wipe + reinsert so re-ingest stays clean
    sb.table("shots").delete().eq("session_id", session_id).execute()

    # Normalize each shot. TopTracer's offline_signed_yd already encodes
    # the L/R sign (negative=left). Same for curve_signed_yd. We store
    # offline as side_m. Curve goes into raw_measurement.
    rows = []
    for r in df.itertuples(index=False):
        club = getattr(r, "club", default_club) if has_club_col else default_club
        shot_num = int(getattr(r, "shot_number"))

        flat_carry_yd = _clean(getattr(r, "flat_carry_yd", None))
        distance_trend_yd = _clean(getattr(r, "distance_trend_yd", None))
        ball_speed_mph = _clean(getattr(r, "ball_speed_mph", None))
        launch_angle = _clean(getattr(r, "launch_angle_deg", None))
        height_yd = _clean(getattr(r, "height_yd", None))
        landing_angle = _clean(getattr(r, "landing_angle_deg", None))
        hang_time = _clean(getattr(r, "hang_time_s", None))
        curve_signed_yd = _clean(getattr(r, "curve_signed_yd", None))
        offline_signed_yd = _clean(getattr(r, "offline_signed_yd", None))

        # raw_measurement mirrors what we store for TrackMan, but only the
        # fields TopTracer actually provides — everything else stays absent.
        raw = {
            "source": "toptracer",
            "club_full": club,
            "measurement": {
                "BallSpeed": ball_speed_mph * MPH_TO_MPS if ball_speed_mph is not None else None,
                "LaunchAngle": launch_angle,
                "Carry": flat_carry_yd * YD_TO_M if flat_carry_yd is not None else None,
                "Total": distance_trend_yd * YD_TO_M if distance_trend_yd is not None else None,
                "MaxHeight": height_yd * YD_TO_M if height_yd is not None else None,
                "LandingAngle": landing_angle,
                "HangTime": hang_time,
                "CarrySide": offline_signed_yd * YD_TO_M if offline_signed_yd is not None else None,
                "Curve": curve_signed_yd * YD_TO_M if curve_signed_yd is not None else None,
            },
        }

        rows.append(
            {
                "session_id": session_id,
                "shot_num": shot_num,
                "club": club,
                "club_speed_mps": None,           # TopTracer doesn't measure
                "attack_angle_deg": None,
                "ball_speed_mps": (ball_speed_mph * MPH_TO_MPS) if ball_speed_mph is not None else None,
                "spin_rate_rpm": None,            # not measured
                "carry_m": (flat_carry_yd * YD_TO_M) if flat_carry_yd is not None else None,
                "side_m": (offline_signed_yd * YD_TO_M) if offline_signed_yd is not None else None,
                "smash_factor": None,             # needs club speed
                # Cross-source first-class fields
                "total_m": (distance_trend_yd * YD_TO_M) if distance_trend_yd is not None else None,
                "launch_angle_deg": launch_angle,
                "max_height_m": (height_yd * YD_TO_M) if height_yd is not None else None,
                "landing_angle_deg": landing_angle,
                "hang_time_s": hang_time,
                "curve_m": (curve_signed_yd * YD_TO_M) if curve_signed_yd is not None else None,
                "raw_measurement": raw,
            }
        )

    CHUNK = 200
    for i in range(0, len(rows), CHUNK):
        sb.table("shots").upsert(
            rows[i : i + CHUNK], on_conflict="session_id,club,shot_num"
        ).execute()

    return session_id, len(rows)

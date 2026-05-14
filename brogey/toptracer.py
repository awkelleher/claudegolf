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
def _external_id_for_date(session_date: str) -> str:
    """One TopTracer session per date — multiple CSVs (one per club) all
    get merged into the same session. A range visit is one session even
    if you hit 5 clubs and ChatGPT-parsed each into its own file.
    """
    return f"toptracer-{session_date}"


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
    """Infer club from filename. Longest tokens first so '5iron' doesn't
    short-circuit on '5i' in a different file's name."""
    n = name.lower()
    for token, club in [
        ("driver", "Dr"),
        ("3wood", "5W"),  # user mislabel correction (see prior chat)
        ("5wood", "5W"),
        ("7wood", "7i"),  # user mislabel correction
        ("9iron", "9i"), ("8iron", "8i"), ("7iron", "7i"),
        ("6iron", "6i"), ("5iron", "5i"), ("4iron", "4i"), ("3iron", "3i"),
        ("pwedge", "PW"), ("gwedge", "GW"), ("swedge", "SW"), ("lwedge", "LW"),
        # short codes — segment-bounded with underscores so we don't false-match
        ("_3w_", "5W"), ("_5w_", "5W"), ("_7w_", "7i"),
        ("_3i_", "3i"), ("_4i_", "4i"), ("_5i_", "5i"), ("_6i_", "6i"),
        ("_7i_", "7i"), ("_8i_", "8i"), ("_9i_", "9i"),
        ("_pw_", "PW"), ("_sw_", "SW"), ("_gw_", "GW"), ("_lw_", "LW"),
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


# Full-name club aliases that can appear in a CSV's `club` column.
_CLUB_FULLNAME_TO_SHORT = {
    "driver": "Dr",
    "3 wood": "3W", "3wood": "3W",
    "5 wood": "5W", "5wood": "5W",
    "7 wood": "7W", "7wood": "7W",
    "3 iron": "3i", "3iron": "3i",
    "4 iron": "4i", "4iron": "4i",
    "5 iron": "5i", "5iron": "5i",
    "6 iron": "6i", "6iron": "6i",
    "7 iron": "7i", "7iron": "7i",
    "8 iron": "8i", "8iron": "8i",
    "9 iron": "9i", "9iron": "9i",
    "pitching wedge": "PW", "pitchingwedge": "PW", "pw": "PW",
    "gap wedge": "GW", "gapwedge": "GW", "gw": "GW",
    "sand wedge": "SW", "sandwedge": "SW", "sw": "SW",
    "lob wedge": "LW", "lobwedge": "LW", "lw": "LW",
}


def _normalize_club(raw: str | None, default: str) -> str:
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return default
    key = str(raw).strip().lower()
    if not key:
        return default
    # Exact match to short codes too
    upper = str(raw).strip()
    if upper in {"Dr", "3W", "5W", "7W", "3i", "4i", "5i", "6i", "7i", "8i", "9i", "PW", "GW", "SW", "LW"}:
        return upper
    return _CLUB_FULLNAME_TO_SHORT.get(key, default)


def _parse_directional(curve_val, direction_val=None) -> float | None:
    """Parse TopTracer's curve/offline fields. Handles:
      - pre-signed numeric: 15.0 or -7.0
      - "L 15" / "R 10" / "0" string
      - paired direction + magnitude: direction="L", magnitude=15
    Returns yards as a signed float. Negative = left, positive = right.
    """
    # Already signed numeric? (the original driver CSV format)
    if isinstance(curve_val, (int, float)) and not (isinstance(curve_val, float) and math.isnan(curve_val)):
        # If a direction column is also present, trust its sign over the numeric one.
        if direction_val is not None and isinstance(direction_val, str) and direction_val.strip():
            mag = abs(float(curve_val))
            return -mag if direction_val.strip().upper().startswith("L") else mag
        return float(curve_val)

    if not isinstance(curve_val, str):
        return None
    s = curve_val.strip()
    if not s:
        return None
    # "0" with no direction
    if s in ("0", "0.0", "0 ", "-0"):
        return 0.0
    # "L 15" / "R 10" / "L15"
    head = s[0].upper()
    if head in ("L", "R"):
        try:
            mag = float(s[1:].strip())
        except ValueError:
            return None
        return -mag if head == "L" else mag
    # Plain number string
    try:
        v = float(s)
    except ValueError:
        return None
    if direction_val and isinstance(direction_val, str) and direction_val.strip():
        return -abs(v) if direction_val.strip().upper().startswith("L") else abs(v)
    return v


def _get(row_dict: dict, *names):
    """Return the first matching key's value from a row dict (case-sensitive
    as pandas reads them)."""
    for n in names:
        if n in row_dict:
            return row_dict[n]
    return None


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
    # Normalize headers so case/whitespace differences don't matter
    df.columns = [c.strip() for c in df.columns]

    sb = service_client()
    external_id = _external_id_for_date(session_date)

    sess = (
        sb.table("sessions")
        .upsert(
            {
                "session_date": session_date,
                "source": "toptracer",
                "external_id": external_id,
                "pdf_filename": csv_path.name,  # repurpose: filename of last CSV ingested into this session
            },
            on_conflict="source,external_id",
        )
        .execute()
    )
    session_id = sess.data[0]["id"]

    # Normalize each shot. TopTracer's offline_signed_yd already encodes
    # the L/R sign (negative=left). Same for curve_signed_yd. We store
    # offline as side_m. Curve goes into raw_measurement.
    rows = []
    skipped_unknown = 0
    skipped_nonnumeric = 0
    for _, r in df.iterrows():
        rd = r.to_dict()

        # Club: prefer per-row `club` column when present, else filename default.
        club = _normalize_club(rd.get("club"), default_club) if has_club_col else default_club
        if club == "Unknown":
            skipped_unknown += 1
            continue

        # Shot identifier: column may be `shot_number` or `shot`. Some CSVs have
        # an "AVG" summary row — skip anything that isn't an integer.
        shot_raw = rd.get("shot_number", rd.get("shot"))
        shot_num = _to_int(shot_raw)
        if shot_num is None:
            skipped_nonnumeric += 1
            continue

        flat_carry_yd = _to_float(rd.get("flat_carry_yd"))
        distance_trend_yd = _to_float(rd.get("distance_trend_yd"))
        ball_speed_mph = _to_float(rd.get("ball_speed_mph"))
        launch_angle = _to_float(rd.get("launch_angle_deg"))
        height_yd = _to_float(rd.get("height_yd"))
        landing_angle = _to_float(rd.get("landing_angle_deg"))
        hang_time = _to_float(rd.get("hang_time_s"))

        # Curve / offline: prefer the pre-signed columns; fall back to parsing
        # whatever shape the CSV ended up with.
        curve_signed_yd = _to_float(rd.get("curve_signed_yd"))
        if curve_signed_yd is None:
            curve_signed_yd = _parse_directional(rd.get("curve_yd"), rd.get("curve_direction"))
        offline_signed_yd = _to_float(rd.get("offline_signed_yd"))
        if offline_signed_yd is None:
            offline_signed_yd = _parse_directional(rd.get("offline_yd"), rd.get("offline_direction"))

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

    # Replace only the (session, club) pairs this CSV provides — other
    # clubs in the same session (from other CSVs) stay untouched.
    clubs_in_csv = {row["club"] for row in rows}
    for club in clubs_in_csv:
        sb.table("shots").delete().eq("session_id", session_id).eq("club", club).execute()

    CHUNK = 200
    for i in range(0, len(rows), CHUNK):
        sb.table("shots").upsert(
            rows[i : i + CHUNK], on_conflict="session_id,club,shot_num"
        ).execute()

    return session_id, len(rows)

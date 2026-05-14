"""TrackMan activity-report API ingester.

Direct replacement for the old PDF parser. Calls TrackMan's public-ish
report endpoint with a session's ActivityId and pulls the full per-shot
data — 30+ measurement fields plus the full 3D ball trajectory.

The endpoint accepts no auth header; the ActivityId itself is the access
token. (TrackMan's share-by-link is keyed on the same UUID.)

Persists primary metrics into typed columns (carry_m, smash_factor, etc.)
and stashes the complete Measurement + ImpactLocation + Videos blob into
the `raw_measurement` jsonb column for future use without further
migrations.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlparse

import requests

from brogey.db import service_client

API_URL = "https://golf-player-activities.trackmangolf.com/api/reports/getactivityreport"

# Map TrackMan's Club enum to our short-code convention.
CLUB_MAP = {
    "Driver": "Dr",
    "1Wood": "Dr",
    "3Wood": "3W",
    "5Wood": "5W",
    "7Wood": "7W",
    "3Iron": "3i",
    "4Iron": "4i",
    "5Iron": "5i",
    "6Iron": "6i",
    "7Iron": "7i",
    "8Iron": "8i",
    "9Iron": "9i",
    "PitchingWedge": "PW",
    "SandWedge": "SW",
    "GapWedge": "GW",
    "LobWedge": "LW",
}


def _short_club(club_name: str | None) -> str:
    if not club_name:
        return "Unknown"
    # Normalize: "5 Iron" -> "5Iron"
    key = club_name.replace(" ", "")
    return CLUB_MAP.get(key, club_name or "Unknown")


def extract_activity_id(url_or_id: str) -> str:
    """Pull an ActivityId out of either a raw UUID or a TrackMan share URL."""
    s = url_or_id.strip()
    # Already a UUID?
    if re.fullmatch(r"[0-9a-f-]{32,36}", s, flags=re.IGNORECASE):
        return s
    # Try parsing as URL
    try:
        q = parse_qs(urlparse(s).query)
        if "a" in q:
            return q["a"][0]
        if "ActivityId" in q:
            return q["ActivityId"][0]
    except Exception:
        pass
    raise ValueError(f"Could not extract ActivityId from: {url_or_id!r}")


def fetch_activity(activity_id: str, altitude_m: int = 0, temp_c: int = 25) -> dict:
    """Hit the TrackMan API. Returns the parsed JSON dict."""
    resp = requests.post(
        API_URL,
        headers={"content-type": "application/json"},
        json={
            "ActivityId": activity_id,
            "Altitude": altitude_m,
            "Temperature": temp_c,
            "BallType": "Premium",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _scrub_player(payload: dict) -> dict:
    """Strip PII (name, email) before persisting. We keep the player Id only."""
    for sg in payload.get("StrokeGroups", []) or []:
        player = sg.get("Player") or {}
        for k in ("Name", "Email"):
            player.pop(k, None)
    user = payload.get("User") or {}
    for k in ("Name", "Email"):
        user.pop(k, None)
    return payload


def _row_for_stroke(session_id: str, club_short: str, shot_num: int, stroke: dict) -> dict:
    m = stroke.get("Measurement") or {}
    impact = stroke.get("ImpactLocation") or {}

    # Compose the raw blob we stash in jsonb. Trajectory is in m.
    raw = {
        "stroke_id": stroke.get("Id"),
        "time": stroke.get("Time"),
        "club_full": stroke.get("Club"),
        "ball": stroke.get("Ball"),
        "measurement": m,
        "impact_location": impact,
        # MeasurementDetails is large; keep it. NormalizedMeasurement we drop
        # since we already capture the raw values and dashboards can normalize.
        "measurement_details": stroke.get("MeasurementDetails"),
    }

    return {
        "session_id": session_id,
        "shot_num": shot_num,
        "club": club_short,
        "club_speed_mps": m.get("ClubSpeed"),
        "attack_angle_deg": m.get("AttackAngle"),
        "ball_speed_mps": m.get("BallSpeed"),
        "spin_rate_rpm": int(m["SpinRate"]) if m.get("SpinRate") is not None else None,
        "carry_m": m.get("Carry"),
        "side_m": m.get("CarrySide"),
        "smash_factor": (
            round(m["SmashFactor"], 3) if m.get("SmashFactor") is not None else None
        ),
        "raw_measurement": raw,
    }


def pull_activity(activity_id: str) -> tuple[str, int]:
    """End-to-end: hit the API, upsert session + shots into Supabase.

    Returns (session_id, n_shots).
    """
    payload = fetch_activity(activity_id)
    payload = _scrub_player(payload)

    # Session: derive date + label
    stroke_groups = payload.get("StrokeGroups") or []
    if not stroke_groups:
        raise RuntimeError(f"No StrokeGroups in API response for {activity_id}")

    # All groups in one activity share a date. Use the first.
    session_date = stroke_groups[0].get("Date") or payload.get("Time", "")[:10]

    # Facility / location lives in the Groups metadata, e.g. "Hudson Golf, Bay 5".
    groups = payload.get("Groups") or []
    location = " / ".join(
        g["Name"] for g in groups
        if g.get("Kind") in ("Facility:2", "Location", "Bay") and g.get("Name") and not g["Name"].startswith(g.get("Id", ""))
    ) or None

    sb = service_client()
    sess_resp = (
        sb.table("sessions")
        .upsert(
            {
                "session_date": session_date,
                "source": "trackman",
                "external_id": activity_id,
                "location": location,
            },
            on_conflict="source,external_id",
        )
        .execute()
    )
    session_id = sess_resp.data[0]["id"]

    # Wipe and reinsert shots — TrackMan is source of truth, our DB mirrors it.
    sb.table("shots").delete().eq("session_id", session_id).execute()

    rows: list[dict] = []
    for sg in stroke_groups:
        club_short = _short_club(sg.get("Club"))
        for i, stroke in enumerate(sg.get("Strokes") or [], start=1):
            rows.append(_row_for_stroke(session_id, club_short, i, stroke))

    # Chunked upsert
    CHUNK = 200
    for i in range(0, len(rows), CHUNK):
        sb.table("shots").upsert(
            rows[i : i + CHUNK], on_conflict="session_id,club,shot_num"
        ).execute()

    return session_id, len(rows)


def existing_activity_ids() -> list[str]:
    """All TrackMan external_ids already in the DB."""
    sb = service_client()
    rows = (
        sb.table("sessions")
        .select("external_id")
        .eq("source", "trackman")
        .execute()
        .data
    )
    return [r["external_id"] for r in rows if r.get("external_id")]

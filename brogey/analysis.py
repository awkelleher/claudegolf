"""Pure-Python session analysis. No LLM. Produces an InsightBundle
that the coach layer feeds to Claude alongside the raw shots.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import pandas as pd

from brogey.benchmarks import benchmark_for
from brogey.db import service_client


@dataclass
class ClubSummary:
    club: str
    n_shots: int
    carry_m_median: float | None
    carry_m_p25: float | None
    carry_m_p75: float | None
    side_m_stddev: float | None        # dispersion proxy
    smash_factor_median: float | None
    spin_rate_rpm_median: float | None
    # Comparison to tour benchmarks (deltas, positive = better than tour)
    carry_vs_tour_m: float | None
    smash_vs_tour: float | None
    # --- Diagnostic fields from raw_measurement (the "why" behind the result) ---
    club_path_deg_median: float | None        # negative = out-to-in (over the top)
    face_angle_deg_median: float | None       # negative = closed at impact
    face_to_path_deg_median: float | None     # face - path; the curve driver
    dynamic_loft_deg_median: float | None     # actual loft delivered to ball
    spin_loft_deg_median: float | None        # dynamic loft - attack angle (spin driver)
    impact_offset_m_stddev: float | None      # heel/toe strike consistency
    impact_height_m_stddev: float | None      # high/low strike consistency
    # --- Cross-source flight metrics (TrackMan + TopTracer) ---
    launch_angle_deg_median: float | None
    max_height_m_median: float | None
    landing_angle_deg_median: float | None
    hang_time_s_median: float | None
    total_m_median: float | None
    curve_m_median: float | None


@dataclass
class SessionInsightBundle:
    session_id: str
    session_date: str
    source: str                              # 'trackman' | 'toptracer' | etc.
    n_shots: int
    clubs_used: list[str]
    per_club: list[ClubSummary]
    # Cross-club observations
    flags: list[str]   # short human-readable notes the stats engine produced
    # Comparison context against the user's all-time history
    all_time_carry_by_club: dict[str, float]   # median carry across all sessions

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "per_club"},
            "per_club": [asdict(c) for c in self.per_club],
        }


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
    except (TypeError, ValueError):
        return None


def _raw_series(group: pd.DataFrame, *path: str) -> pd.Series:
    """Pull a numeric series from the nested raw_measurement jsonb column.

    Example: _raw_series(group, "measurement", "ClubPath") returns each
    shot's ClubPath as a numeric Series with NaNs dropped.
    """
    def get(row):
        d = row
        for p in path:
            if not isinstance(d, dict):
                return None
            d = d.get(p)
        return d if isinstance(d, (int, float)) else None

    return group["raw_measurement"].apply(get).dropna().astype(float)


def _median_or_none(s: pd.Series) -> float | None:
    return float(s.median()) if len(s) else None


def _stddev_or_none(s: pd.Series) -> float | None:
    return float(s.std()) if len(s) > 1 else None


def _summarize_club(club: str, group: pd.DataFrame) -> ClubSummary:
    bench = benchmark_for(club)
    carry = group["carry_m"].dropna()
    side = group["side_m"].dropna()
    smash = group["smash_factor"].dropna()
    spin = group["spin_rate_rpm"].dropna()

    carry_median = float(carry.median()) if len(carry) else None
    smash_median = float(smash.median()) if len(smash) else None

    # Cross-source flight metrics (first-class columns)
    launch = group.get("launch_angle_deg", pd.Series(dtype=float)).dropna()
    max_h = group.get("max_height_m", pd.Series(dtype=float)).dropna()
    landing = group.get("landing_angle_deg", pd.Series(dtype=float)).dropna()
    hang = group.get("hang_time_s", pd.Series(dtype=float)).dropna()
    total = group.get("total_m", pd.Series(dtype=float)).dropna()
    curve = group.get("curve_m", pd.Series(dtype=float)).dropna()

    # TrackMan-only diagnostics still come from raw_measurement
    club_path = _raw_series(group, "measurement", "ClubPath")
    face_angle = _raw_series(group, "measurement", "FaceAngle")
    face_to_path = _raw_series(group, "measurement", "FaceToPath")
    dynamic_loft = _raw_series(group, "measurement", "DynamicLoft")
    spin_loft = _raw_series(group, "measurement", "SpinLoft")
    impact_off = _raw_series(group, "impact_location", "ImpactOffset")
    impact_h = _raw_series(group, "impact_location", "ImpactHeight")

    return ClubSummary(
        club=club,
        n_shots=len(group),
        carry_m_median=carry_median,
        carry_m_p25=float(carry.quantile(0.25)) if len(carry) else None,
        carry_m_p75=float(carry.quantile(0.75)) if len(carry) else None,
        side_m_stddev=float(side.std()) if len(side) > 1 else None,
        smash_factor_median=smash_median,
        spin_rate_rpm_median=float(spin.median()) if len(spin) else None,
        carry_vs_tour_m=(carry_median - bench["carry_m"]) if (bench and carry_median is not None) else None,
        smash_vs_tour=(smash_median - bench["smash"]) if (bench and smash_median is not None) else None,
        club_path_deg_median=_median_or_none(club_path),
        face_angle_deg_median=_median_or_none(face_angle),
        face_to_path_deg_median=_median_or_none(face_to_path),
        dynamic_loft_deg_median=_median_or_none(dynamic_loft),
        spin_loft_deg_median=_median_or_none(spin_loft),
        impact_offset_m_stddev=_stddev_or_none(impact_off),
        impact_height_m_stddev=_stddev_or_none(impact_h),
        launch_angle_deg_median=_median_or_none(launch),
        max_height_m_median=_median_or_none(max_h),
        landing_angle_deg_median=_median_or_none(landing),
        hang_time_s_median=_median_or_none(hang),
        total_m_median=_median_or_none(total),
        curve_m_median=_median_or_none(curve),
    )


def _flag_observations(per_club: list[ClubSummary]) -> list[str]:
    """Pure-stats flags. Brogey can either lean on these or override."""
    flags: list[str] = []
    for c in per_club:
        if c.club == "Unknown":
            continue
        # Dispersion flag — only if we have enough shots to estimate
        if c.side_m_stddev is not None and c.n_shots >= 5 and c.side_m_stddev > 15:
            flags.append(f"{c.club}: high side dispersion ({c.side_m_stddev:.0f}m stddev)")
        # Smash vs tour
        if c.smash_vs_tour is not None and c.smash_vs_tour < -0.05 and c.n_shots >= 3:
            flags.append(
                f"{c.club}: smash factor {c.smash_factor_median:.2f} is {abs(c.smash_vs_tour):.2f} below tour"
            )
        # Carry consistency: IQR > 20m on a single club is a wide spread
        if (
            c.carry_m_p75 is not None
            and c.carry_m_p25 is not None
            and (c.carry_m_p75 - c.carry_m_p25) > 20
            and c.n_shots >= 5
        ):
            flags.append(
                f"{c.club}: carry IQR is {c.carry_m_p75 - c.carry_m_p25:.0f}m — inconsistent strike"
            )
        # --- Rich-data diagnostics (only with adequate sample size) ---
        if c.n_shots >= 5:
            # Over-the-top path (out-to-in) on a driver/wood is the slice setup
            if c.club_path_deg_median is not None and c.club_path_deg_median < -2.0 \
               and c.club in ("Dr", "3W", "5W", "7W"):
                flags.append(
                    f"{c.club}: club path {c.club_path_deg_median:+.1f}° (out-to-in / over-the-top)"
                )
            # Face significantly closed or open
            if c.face_angle_deg_median is not None and abs(c.face_angle_deg_median) > 3.0:
                direction = "closed" if c.face_angle_deg_median < 0 else "open"
                flags.append(
                    f"{c.club}: face {c.face_angle_deg_median:+.1f}° {direction} at impact"
                )
            # Face-to-path divergence — the real curve driver
            if c.face_to_path_deg_median is not None and abs(c.face_to_path_deg_median) > 3.0:
                flags.append(
                    f"{c.club}: face-to-path {c.face_to_path_deg_median:+.1f}° — expect curving ball flight"
                )
            # Strike consistency from impact location
            if c.impact_offset_m_stddev is not None and c.impact_offset_m_stddev > 0.012:
                flags.append(
                    f"{c.club}: toe/heel strike inconsistent (±{c.impact_offset_m_stddev*1000:.0f}mm stddev)"
                )
            if c.impact_height_m_stddev is not None and c.impact_height_m_stddev > 0.010:
                flags.append(
                    f"{c.club}: high/low strike inconsistent (±{c.impact_height_m_stddev*1000:.0f}mm stddev)"
                )
    return flags


def analyze_session(session_id: str) -> SessionInsightBundle:
    sb = service_client()
    session_row = (
        sb.table("sessions").select("*").eq("id", session_id).single().execute().data
    )
    shots = (
        sb.table("shots")
        .select("*")
        .eq("session_id", session_id)
        .execute()
        .data
    )
    if not shots:
        raise ValueError(f"No shots found for session {session_id}")

    df = pd.DataFrame(shots)
    per_club = [
        _summarize_club(club, grp) for club, grp in df.groupby("club")
    ]
    per_club.sort(key=lambda c: c.n_shots, reverse=True)
    flags = _flag_observations(per_club)

    # All-time medians for context
    all_shots = sb.table("shots").select("club,carry_m").execute().data
    all_df = pd.DataFrame(all_shots)
    all_time_median = (
        all_df.dropna(subset=["carry_m"]).groupby("club")["carry_m"].median().to_dict()
        if len(all_df)
        else {}
    )

    return SessionInsightBundle(
        session_id=session_id,
        session_date=session_row["session_date"],
        source=session_row.get("source") or "trackman",
        n_shots=len(df),
        clubs_used=sorted(df["club"].unique().tolist()),
        per_club=per_club,
        flags=flags,
        all_time_carry_by_club={k: float(v) for k, v in all_time_median.items()},
    )


def latest_session_id() -> str:
    sb = service_client()
    rows = (
        sb.table("sessions")
        .select("id,session_date")
        .order("session_date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise RuntimeError("No sessions in DB.")
    return rows[0]["id"]

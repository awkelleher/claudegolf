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


@dataclass
class SessionInsightBundle:
    session_id: str
    session_date: str
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


def _summarize_club(club: str, group: pd.DataFrame) -> ClubSummary:
    bench = benchmark_for(club)
    carry = group["carry_m"].dropna()
    side = group["side_m"].dropna()
    smash = group["smash_factor"].dropna()
    spin = group["spin_rate_rpm"].dropna()

    carry_median = float(carry.median()) if len(carry) else None
    smash_median = float(smash.median()) if len(smash) else None

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

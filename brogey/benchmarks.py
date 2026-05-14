"""Tour benchmarks for comparison. Approximate PGA Tour averages."""
from __future__ import annotations

# Carry (m) and smash factor by club.
# Source: aggregated public TrackMan averages. Numbers are deliberately
# approximate — Brogey uses them as a frame of reference, not a verdict.
TOUR_BENCHMARKS: dict[str, dict[str, float]] = {
    "Dr": {"carry_m": 256.0, "smash": 1.49, "spin_rpm": 2700},
    "5W": {"carry_m": 219.0, "smash": 1.48, "spin_rpm": 3300},
    "5i": {"carry_m": 183.0, "smash": 1.41, "spin_rpm": 5300},
    "7i": {"carry_m": 163.0, "smash": 1.38, "spin_rpm": 7000},
    "8i": {"carry_m": 151.0, "smash": 1.35, "spin_rpm": 7900},
    "PW": {"carry_m": 124.0, "smash": 1.27, "spin_rpm": 9300},
    "SW": {"carry_m": 89.0, "smash": 1.20, "spin_rpm": 10000},
}


def benchmark_for(club: str) -> dict[str, float] | None:
    return TOUR_BENCHMARKS.get(club)

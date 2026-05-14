"""Tour benchmarks for comparison. Approximate PGA Tour averages."""
from __future__ import annotations

# Carry (m) and smash factor by club.
# Source: aggregated public TrackMan averages. Numbers are deliberately
# approximate — Brogey uses them as a frame of reference, not a verdict.
TOUR_BENCHMARKS: dict[str, dict[str, float]] = {
    "Dr": {"carry_m": 256.0, "smash": 1.49, "spin_rpm": 2700},
    "3W": {"carry_m": 232.0, "smash": 1.48, "spin_rpm": 3300},
    "5W": {"carry_m": 219.0, "smash": 1.48, "spin_rpm": 3500},
    "7W": {"carry_m": 200.0, "smash": 1.46, "spin_rpm": 4200},
    "3i": {"carry_m": 198.0, "smash": 1.44, "spin_rpm": 4500},
    "4i": {"carry_m": 191.0, "smash": 1.43, "spin_rpm": 4800},
    "5i": {"carry_m": 183.0, "smash": 1.41, "spin_rpm": 5300},
    "6i": {"carry_m": 174.0, "smash": 1.40, "spin_rpm": 6200},
    "7i": {"carry_m": 163.0, "smash": 1.38, "spin_rpm": 7000},
    "8i": {"carry_m": 151.0, "smash": 1.35, "spin_rpm": 7900},
    "9i": {"carry_m": 137.0, "smash": 1.32, "spin_rpm": 8500},
    "PW": {"carry_m": 124.0, "smash": 1.27, "spin_rpm": 9300},
    "GW": {"carry_m": 110.0, "smash": 1.24, "spin_rpm": 9700},
    "SW": {"carry_m": 89.0, "smash": 1.20, "spin_rpm": 10000},
    "LW": {"carry_m": 73.0, "smash": 1.18, "spin_rpm": 10200},
}


def benchmark_for(club: str) -> dict[str, float] | None:
    return TOUR_BENCHMARKS.get(club)

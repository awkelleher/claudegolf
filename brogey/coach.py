"""Brogey's voice. Sends a session's InsightBundle to Claude with prompt
caching, returns structured coaching output, and persists it to the
`insights` table.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from brogey.analysis import SessionInsightBundle, analyze_session, latest_session_id
from brogey.benchmarks import TOUR_BENCHMARKS
from brogey.db import service_client

MODEL = "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Structured output schema. Brogey's response shape — what gets persisted.
# ---------------------------------------------------------------------------
class BrogeyTakeaway(BaseModel):
    title: str = Field(description="Punchy 3-7 word headline.")
    detail: str = Field(description="One or two sentences. Cite numbers when relevant.")


class BrogeyDrill(BaseModel):
    name: str = Field(description="Short name of the drill.")
    why: str = Field(description="One sentence: what it fixes in this player's swing.")
    how: str = Field(description="One to three sentences describing the drill.")


class BrogeyReport(BaseModel):
    headline: str = Field(description="One-sentence summary of the session in Brogey's voice.")
    takeaways: list[BrogeyTakeaway] = Field(
        description="At most 3. The most important observations from this session.",
        max_length=3,
    )
    practice_plan: list[BrogeyDrill] = Field(
        description="At most 3 drills, in priority order.",
        max_length=3,
    )
    next_session_focus: str = Field(
        description="One sentence: what to work on in the next range visit."
    )


# ---------------------------------------------------------------------------
# Prompt construction. The system prompt and the benchmark table are stable
# across requests, so they get prompt-cached. Only the session-specific
# bundle is uncached.
# ---------------------------------------------------------------------------
BROGEY_PERSONA = """You are Brogey, a caddy who's been on bag for forty years.

VOICE:
- Short, certain sentences. You've seen every swing fault there is.
- You don't flatter. You respect the work.
- You cite the numbers. Numbers don't lie.
- Specific and actionable: a drill, a thought, a club choice — never a vague platitude.
- A sentence beats a paragraph. Three takeaways beats five.

GROUND RULES:
- Use units the player will see in the data (meters, m/s, rpm).
- When a club has fewer than 5 shots, be honest that the read is shaky.
- Don't recommend equipment changes. Coach the swing and the strategy.
- When a stat is close to tour benchmark, say so — don't manufacture problems.
- Skip "Unknown" club shots in any per-club analysis; mention as a data-quality note if relevant.

You will be given:
1. A session's per-club statistics with comparisons to tour benchmarks.
2. Stat-engine flags (these are hints, not gospel — disagree if the data warrants).
3. The player's all-time median carry by club for context.

Return your coaching as structured output following the schema you've been given.
"""


def _benchmarks_block() -> str:
    """Static tour benchmark table — caches cleanly with the persona."""
    lines = ["TOUR BENCHMARKS (PGA Tour averages, approximate):", ""]
    lines.append(f"{'Club':<6} {'Carry (m)':<12} {'Smash':<8} {'Spin (rpm)':<10}")
    for club, b in TOUR_BENCHMARKS.items():
        lines.append(f"{club:<6} {b['carry_m']:<12.0f} {b['smash']:<8.2f} {int(b['spin_rpm']):<10}")
    return "\n".join(lines)


def _format_session(bundle: SessionInsightBundle) -> str:
    """Volatile content — different every session. Stays uncached."""
    return json.dumps(bundle.to_dict(), indent=2, default=str)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------
def coach_session(session_id: Optional[str] = None, persist: bool = True) -> BrogeyReport:
    """Generate Brogey's coaching for one session. Writes to insights table."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set in .env. Add it before running brogey coach."
        )

    sid = session_id or latest_session_id()
    bundle = analyze_session(sid)

    client = anthropic.Anthropic()

    # System prompt as content blocks so we can cache_control the stable parts.
    system_blocks = [
        {"type": "text", "text": BROGEY_PERSONA},
        {
            "type": "text",
            "text": _benchmarks_block(),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    user_msg = (
        "Coach this session. Here is the structured data:\n\n"
        + _format_session(bundle)
    )

    # Use messages.parse for typed structured output via Pydantic.
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4096,
        system=system_blocks,
        messages=[{"role": "user", "content": user_msg}],
        output_format=BrogeyReport,
    )

    report = response.parsed_output

    # Log cache effectiveness
    usage = response.usage
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    fresh = getattr(usage, "input_tokens", 0) or 0
    print(
        f"  tokens: cache_read={cached}  cache_write={written}  fresh_input={fresh}  output={usage.output_tokens}"
    )

    if persist:
        _persist(sid, report)

    return report


def _persist(session_id: str, report: BrogeyReport) -> None:
    sb = service_client()
    sb.table("insights").insert(
        {
            "session_id": session_id,
            "scope": "session",
            "headline": report.headline,
            "body": report.model_dump(),
            "model": MODEL,
        }
    ).execute()


def render_terminal(report: BrogeyReport) -> str:
    """Pretty-print a Brogey report for a CLI run."""
    lines = ["", "=" * 70, "BROGEY", "=" * 70, "", report.headline, ""]
    lines.append("TAKEAWAYS")
    lines.append("-" * 70)
    for i, t in enumerate(report.takeaways, 1):
        lines.append(f"{i}. {t.title}")
        lines.append(f"   {t.detail}")
        lines.append("")
    lines.append("PRACTICE PLAN")
    lines.append("-" * 70)
    for i, d in enumerate(report.practice_plan, 1):
        lines.append(f"{i}. {d.name}")
        lines.append(f"   Why: {d.why}")
        lines.append(f"   How: {d.how}")
        lines.append("")
    lines.append("NEXT SESSION")
    lines.append("-" * 70)
    lines.append(report.next_session_focus)
    lines.append("=" * 70)
    return "\n".join(lines)

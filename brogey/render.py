"""Build the Supabase-backed dashboard from the existing static HTML.

Reads trackman_dashboard.html, swaps the inline shot data for a Supabase
fetch, replaces the auto-generated takeaways block with a Brogey-from-DB
renderer, and writes the result to dashboards/index.html. The output is
self-contained (Supabase JS client via CDN) — drop it on any static host.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "trackman_dashboard.html"
OUT = ROOT / "dashboards" / "index.html"


# ---------------------------------------------------------------------------
# Snippets we inject into the source HTML
# ---------------------------------------------------------------------------
SUPABASE_CDN = (
    '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
)


def _supabase_config_script(url: str, anon_key: str) -> str:
    return (
        "<script>\n"
        f'const SUPABASE_URL = "{url}";\n'
        f'const SUPABASE_ANON_KEY = "{anon_key}";\n'
        "const sb = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);\n"
        "</script>\n"
    )


# Block prepended to the existing dashboard's main <script> body.
# Defines an async init() that pulls shots + insights from Supabase and
# rebinds the variables the existing code expects.
INIT_SHIM = """
// === Brogey/Supabase data layer (injected) ===
let BROGEY_INSIGHTS = [];   // rows from `insights` table, newest first

async function loadFromSupabase() {
  // Pull shots joined with session_date.
  // Use range pagination to be safe if/when we exceed 1k rows.
  const PAGE = 1000;
  let from = 0;
  const all = [];
  while (true) {
    const { data, error } = await sb
      .from('shots')
      .select('shot_num,club,club_speed_mps,attack_angle_deg,ball_speed_mps,spin_rate_rpm,carry_m,side_m,smash_factor,session_id,sessions(session_date)')
      .range(from, from + PAGE - 1);
    if (error) throw error;
    if (!data || !data.length) break;
    data.forEach(row => {
      // flatten the session_date so existing code keeps working
      const sd = row.sessions ? row.sessions.session_date : null;
      RAW.push({
        session_date: sd,
        session_id: row.session_id,
        shot_num: row.shot_num,
        club: row.club,
        club_speed_mps: row.club_speed_mps,
        attack_angle_deg: row.attack_angle_deg,
        ball_speed_mps: row.ball_speed_mps,
        spin_rate_rpm: row.spin_rate_rpm,
        carry_m: row.carry_m,
        side_m: row.side_m,
        smash_factor: row.smash_factor,
      });
    });
    if (data.length < PAGE) break;
    from += PAGE;
  }

  // Insights: latest per session
  const { data: ins } = await sb
    .from('insights')
    .select('id,session_id,headline,body,created_at,sessions(session_date)')
    .eq('scope', 'session')
    .order('created_at', { ascending: false });
  BROGEY_INSIGHTS = ins || [];
}

async function init() {
  document.getElementById('shot-count').innerHTML = 'Loading from Supabase\\u2026';
  try {
    await loadFromSupabase();
  } catch (e) {
    document.getElementById('shot-count').innerHTML =
      '<span style="color:var(--bad)">Failed to load: ' + (e.message || e) + '</span>';
    console.error(e);
    return;
  }

  // Same one-pass computations that used to run at top level.
  RAW.forEach(s => {
    if (s.smash_factor == null && s.ball_speed_mps != null && s.club_speed_mps != null && s.club_speed_mps > 0) {
      s.smash_factor = +(s.ball_speed_mps / s.club_speed_mps).toFixed(3);
    }
  });

  allDates = [...new Set(RAW.map(s => s.session_date))].sort();
  allClubs = [...new Set(RAW.map(s => s.club))].sort((a, b) => {
    const ia = CLUB_ORDER.indexOf(a), ib = CLUB_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  selectedDates = new Set(allDates);
  selectedClubs = new Set(allClubs);

  // Populate session dropdown
  const sel = document.getElementById('session-select');
  // Clear any prior options (in case of re-init)
  Array.from(sel.querySelectorAll('option')).forEach(o => { if (o.value !== 'all') o.remove(); });
  allDates.slice().reverse().forEach(d => {
    const opt = document.createElement('option');
    opt.value = d;
    opt.textContent = d;
    sel.appendChild(opt);
  });

  // Build club checkboxes
  document.getElementById('club-filters').innerHTML = '';
  buildChecks('club-filters', allClubs, selectedClubs, CLUB_COLORS);

  refresh();
}

// Override the auto-generated takeaways with Brogey's persisted insights.
function renderInsightsFromBrogey(shots) {
  const twEl = document.getElementById('takeaways-content');
  const ppEl = document.getElementById('practice-plan-content');

  // Filter Brogey insights to currently selected sessions.
  const visible = BROGEY_INSIGHTS.filter(ins => {
    const d = ins.sessions ? ins.sessions.session_date : null;
    return d && selectedDates.has(d);
  });

  if (!visible.length) {
    twEl.innerHTML =
      '<p style="color:var(--text-dim);font-size:14px;margin-top:10px">' +
      'No Brogey insights yet for this selection. Run <code>brogey coach</code> on a session to generate one.</p>';
    ppEl.innerHTML = '';
    return;
  }

  // Render most-recent first.
  const takeawayHtml = [];
  const drillHtml = [];
  visible.forEach(ins => {
    const body = ins.body || {};
    const date = ins.sessions ? ins.sessions.session_date : '';
    takeawayHtml.push(
      '<div class="takeaway" style="margin-top:18px">' +
        '<h4>' + (ins.headline || 'Brogey says') + ' <span style="color:var(--text-dim);font-weight:400">\\u00b7 ' + date + '</span></h4>'
    );
    (body.takeaways || []).forEach((t, i) => {
      takeawayHtml.push(
        '<p style="margin-top:8px"><strong>' + (i + 1) + '. ' + (t.title || '') + '</strong><br>' +
        (t.detail || '') + '</p>'
      );
    });
    takeawayHtml.push('</div>');

    if (body.practice_plan && body.practice_plan.length) {
      drillHtml.push(
        '<div class="practice-plan" style="margin-top:18px">' +
        '<div style="color:var(--text-dim);font-size:12px;margin-bottom:6px">Drills from ' + date + '</div>' +
        '<ol>'
      );
      body.practice_plan.forEach(d => {
        drillHtml.push(
          '<li><strong>' + (d.name || '') + '</strong> &mdash; ' +
          (d.how || '') +
          ' <span style="color:var(--text-dim)">(' + (d.why || '') + ')</span></li>'
        );
      });
      drillHtml.push('</ol>');
      if (body.next_session_focus) {
        drillHtml.push(
          '<p style="margin-top:10px;color:var(--text-dim);font-size:13px">' +
          '<strong style="color:var(--accent)">Next session:</strong> ' + body.next_session_focus + '</p>'
        );
      }
      drillHtml.push('</div>');
    }
  });

  twEl.innerHTML = takeawayHtml.join('');
  ppEl.innerHTML = drillHtml.join('');
}
"""


def build() -> Path:
    url = os.environ["SUPABASE_URL"]
    anon = os.environ["SUPABASE_ANON_KEY"]

    src = SOURCE.read_text(encoding="utf-8")

    # 1. Strip the inline RAW data (everything from `const RAW = [` to the next `];`)
    src = re.sub(
        r"const RAW = \[\n.*?\n\];",
        "let RAW = [];",
        src,
        count=1,
        flags=re.DOTALL,
    )

    # 2. Make allDates/allClubs reassignable
    src = src.replace(
        "const allDates = [...new Set(RAW.map(s => s.session_date))].sort();",
        "let allDates = [];",
    )
    src = src.replace(
        "const allClubs = [...new Set(RAW.map(s => s.club))].sort((a, b) => {\n"
        "  const ia = CLUB_ORDER.indexOf(a), ib = CLUB_ORDER.indexOf(b);\n"
        "  return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);\n"
        "});",
        "let allClubs = [];",
    )

    # 3. Remove the original top-level smash-factor recompute (now run inside init())
    src = re.sub(
        r"// Compute smash_factor where missing\nRAW\.forEach\(s => \{.*?\}\);\n",
        "",
        src,
        count=1,
        flags=re.DOTALL,
    )

    # 4. Remove the original session-dropdown-population IIFE and buildChecks call
    #    (init() does both, with allDates/allClubs available).
    src = re.sub(
        r"// Populate session dropdown\n\(function\(\) \{.*?\}\)\(\);\n",
        "",
        src,
        count=1,
        flags=re.DOTALL,
    )
    src = src.replace(
        "buildChecks('club-filters', allClubs, selectedClubs, CLUB_COLORS);\n",
        "",
    )

    # 5. In refresh(): the existing code calls renderInsights(active). Redirect that
    #    to renderInsightsFromBrogey so the dashboard shows DB-backed takeaways
    #    instead of the hand-coded JS heuristics.
    src = src.replace("renderInsights(active);", "renderInsightsFromBrogey(active);")

    # 6. Replace `refresh();` at the very end with `init();`
    src = re.sub(r"// Initial render\nrefresh\(\);", "// Initial render\ninit();", src)

    # 7. Inject Supabase config + CDN into <head>, and INIT_SHIM at the start of <script>
    src = src.replace(
        "<script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js\"></script>",
        SUPABASE_CDN
        + "\n<script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js\"></script>\n"
        + _supabase_config_script(url, anon),
    )
    src = src.replace(
        "<script>\nconst M_TO_YD = 1.0936133;",
        "<script>\n" + INIT_SHIM + "\nconst M_TO_YD = 1.0936133;",
    )

    # 8. Update title
    src = src.replace("<title>Trackman - All Sessions</title>", "<title>Brogey</title>")
    src = src.replace(
        "<h1>Trackman <span>&#183;</span> All Sessions</h1>",
        "<h1>Brogey <span>&#183;</span> caddy mode</h1>",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(src, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    p = build()
    print(f"wrote {p}")

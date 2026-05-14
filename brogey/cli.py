"""brogey CLI entry point."""
from __future__ import annotations

import click

from brogey import render as render_module
from brogey.coach import coach_session, render_terminal
from brogey.db import service_client
from brogey.ingest import (
    existing_activity_ids,
    extract_activity_id,
    pull_activity,
)
from brogey.toptracer import ingest_toptracer_csv


@click.group()
def cli():
    """Brogey — your AI golf caddy."""


@cli.command()
def sessions():
    """List all sessions in the DB, newest first."""
    sb = service_client()
    rows = (
        sb.table("sessions")
        .select("id,session_date,source,external_id")
        .order("session_date", desc=True)
        .execute()
        .data
    )
    if not rows:
        click.echo("No sessions in DB.")
        return
    for r in rows:
        click.echo(f"{r['session_date']}  {r['source']:<10}  {r['id']}")


@cli.command()
@click.option(
    "--session-id",
    default=None,
    help="UUID of the session. Defaults to the most recent session.",
)
@click.option(
    "--no-persist",
    is_flag=True,
    help="Skip writing the result to the insights table.",
)
def coach(session_id: str | None, no_persist: bool):
    """Run Brogey on a session and print his coaching."""
    click.echo("Asking Brogey for his read…")
    report = coach_session(session_id=session_id, persist=not no_persist)
    click.echo(render_terminal(report))


@cli.command()
def dashboard():
    """Build dashboards/index.html with embedded Supabase config."""
    path = render_module.build()
    click.echo(f"wrote {path}")
    click.echo("Open in browser, or git push to redeploy on Vercel.")


@cli.command()
@click.argument("url_or_id")
def pull(url_or_id: str):
    """Fetch a TrackMan activity from the public API and store it.

    Accepts either a raw ActivityId UUID or a share URL.
    """
    activity_id = extract_activity_id(url_or_id)
    click.echo(f"Pulling activity {activity_id}…")
    session_id, n = pull_activity(activity_id)
    click.echo(f"  -> session {session_id}  ({n} shots)")


@cli.command("pull-all")
def pull_all_cmd():
    """Re-pull every TrackMan session already in the DB, refreshing rich data."""
    ids = existing_activity_ids()
    click.echo(f"Refreshing {len(ids)} sessions from TrackMan API…")
    total_shots = 0
    failed: list[tuple[str, str]] = []
    for aid in ids:
        try:
            sid, n = pull_activity(aid)
            total_shots += n
            click.echo(f"  ok  {aid}  ({n} shots)")
        except Exception as e:  # noqa: BLE001
            failed.append((aid, str(e)))
            click.echo(f"  FAIL {aid}: {e}")
    click.echo(f"\nDone. {total_shots} shots across {len(ids) - len(failed)} sessions.")
    if failed:
        click.echo(f"{len(failed)} sessions failed — see above.")


@cli.command("ingest-toptracer")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=__import__('pathlib').Path))
@click.option("--date", "session_date", default=None, help="YYYY-MM-DD (overrides date inferred from filename).")
@click.option("--club", default=None, help="Club to assign all shots in this CSV. Overrides filename inference.")
def ingest_toptracer(csv_path, session_date: str | None, club: str | None):
    """Ingest a TopTracer CSV into Supabase as a new session."""
    from pathlib import Path as _Path
    session_id, n = ingest_toptracer_csv(_Path(csv_path), session_date=session_date, club_override=club)
    click.echo(f"stored {n} shots  session={session_id}")


@cli.command("new-session")
@click.argument("url_or_id")
@click.option("--skip-coach", is_flag=True, help="Pull only; don't ask Brogey yet.")
def new_session(url_or_id: str, skip_coach: bool):
    """Pull a new TrackMan activity + ask Brogey + rebuild dashboard."""
    activity_id = extract_activity_id(url_or_id)
    click.echo(f"Pulling activity {activity_id}…")
    session_id, n = pull_activity(activity_id)
    click.echo(f"  -> stored {n} shots")
    if not skip_coach:
        click.echo("\nAsking Brogey for his read…")
        report = coach_session(session_id=session_id)
        click.echo(render_terminal(report))
    render_module.build()
    click.echo("\nDashboard rebuilt. git push to redeploy on Vercel.")


if __name__ == "__main__":
    cli()

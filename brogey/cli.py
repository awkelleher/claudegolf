"""brogey CLI entry point."""
from __future__ import annotations

import click

from brogey.coach import coach_session, render_terminal
from brogey.db import service_client
from brogey import render as render_module


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
    click.echo("Open in browser, or deploy the dashboards/ folder to any static host.")


if __name__ == "__main__":
    cli()

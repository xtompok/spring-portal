"""Skalná data portal CLI: `portal <command>`."""

import json
import os
from datetime import date

import click

from . import flow as flow_mod
from . import load as load_mod
from . import migrate as migrate_mod
from . import station as station_mod
from . import weather as weather_mod


def _station_default():
    return os.environ.get("STATION", station_mod.DEFAULT_STATION)


@click.group()
def cli():
    """Ingestion, weather scraping and flow derivation for the Skalná portal."""


@cli.command()
def migrate():
    """Apply pending SQL migrations."""
    migrate_mod.run(echo=click.echo)


@cli.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
def load(path):
    """Load a lab SD backup tree from PATH (idempotent)."""
    load_mod.load(path, echo=click.echo)


@cli.group()
def weather():
    """Weather scraping / backfill."""


@weather.command("fetch")
@click.option("--from", "start", required=True, type=weather_mod.parse_date,
              help="start date YYYY-MM-DD (inclusive)")
@click.option("--to", "end", required=True, type=weather_mod.parse_date,
              help="end date YYYY-MM-DD (inclusive)")
@click.option("--station", default=lambda: os.environ.get("WEATHER_STATION", "cheb"))
@click.option("--source", default=lambda: os.environ.get("WEATHER_SOURCE", "in-pocasi"))
def weather_fetch(start, end, station, source):
    """Scrape and upsert weather for a date range."""
    weather_mod.fetch(start, end, station, source, echo=click.echo)


@weather.command("fetch-recent")
@click.option("--days", default=3, show_default=True,
              help="fetch the last N days up to today")
@click.option("--station", default=lambda: os.environ.get("WEATHER_STATION", "cheb"))
@click.option("--source", default=lambda: os.environ.get("WEATHER_SOURCE", "in-pocasi"))
def weather_fetch_recent(days, station, source):
    """Scrape the last N days (used by the scheduler)."""
    from datetime import timedelta
    end = date.today()
    start = end - timedelta(days=days)
    weather_mod.fetch(start, end, station, source, echo=click.echo)


@weather.command("load-csv")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--station", default=lambda: os.environ.get("WEATHER_STATION", "cheb"))
@click.option("--source", default=lambda: os.environ.get("WEATHER_SOURCE", "in-pocasi"))
def weather_load_csv(csv_path, station, source):
    """Backfill weather from an existing download_weather.py CSV."""
    weather_mod.load_csv(csv_path, station, source, echo=click.echo)


@cli.command("session-info")
@click.option("--station", default=_station_default)
def session_info(station):
    """Print the router's upload cursor + maintenance flag as JSON.

    Called over SSH by the router at the start of each upload session.
    """
    click.echo(json.dumps(station_mod.session_info(station)))


@cli.group()
def event():
    """Station events / alerts (station_events table)."""


@event.command("add")
@click.option("--kind", required=True, help="event kind, e.g. rtc_backward_jump")
@click.option("--severity", default="warning",
              type=click.Choice(["info", "warning", "critical"]))
@click.option("--detail", default=None, help="JSON object string with context")
@click.option("--station", default=_station_default)
def event_add(kind, severity, detail, station):
    """Record a station event (open-event deduped by station+kind)."""
    parsed = json.loads(detail) if detail else None
    click.echo(json.dumps(station_mod.add_event(kind, severity, parsed, station)))


@event.command("resolve")
@click.option("--kind", required=True)
@click.option("--station", default=_station_default)
def event_resolve(kind, station):
    """Close any open event(s) of the given kind (operator action after a fix)."""
    click.echo(json.dumps({"resolved": station_mod.resolve_event(kind, station)}))


@event.command("list")
@click.option("--all", "show_all", is_flag=True, help="include resolved events")
@click.option("--station", default=None)
def event_list(show_all, station):
    """List events, most recent first."""
    for e in station_mod.list_events(open_only=not show_all, station=station):
        click.echo(json.dumps(e, default=str))


@cli.group()
def flow():
    """Derived flow series."""


@flow.command("rebuild")
@click.option("--since", default=None, help="only recompute lab rows at/after this UTC timestamp")
def flow_rebuild(since):
    """Recompute the derived flow table from lab."""
    flow_mod.rebuild(since=since, echo=click.echo)


if __name__ == "__main__":
    cli()

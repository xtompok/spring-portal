"""Skalná data portal CLI: `portal <command>`."""

import os
from datetime import date

import click

from . import flow as flow_mod
from . import load as load_mod
from . import migrate as migrate_mod
from . import weather as weather_mod


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

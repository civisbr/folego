"""Daily holiday indicators per health region (national and state calendars)."""

import json
from datetime import date
from pathlib import Path

import polars as pl


def region_states(regions_geojson: Path) -> dict[str, str]:
    features = json.loads(Path(regions_geojson).read_text())["features"]
    return {str(f["properties"]["code"]): f["properties"]["state"] for f in features}


def holiday_calendar(start: date, end: date, states: dict[str, str]) -> pl.DataFrame:
    """One row per region and day with public and optional (ponto facultativo) flags.

    Municipal holidays are not covered: the `holidays` package has no municipal
    calendars, and a regional flag would need a population-weighted share anyway.
    """
    import holidays

    known = set(holidays.Brazil.subdivisions)
    unknown = set(states.values()) - known
    if unknown:
        raise ValueError(f"Unknown Brazilian state code(s): {sorted(unknown)}")
    years = range(start.year, end.year + 1)
    days = pl.date_range(start, end, "1d", eager=True)
    frames = []
    for state in sorted(set(states.values())):
        public = holidays.Brazil(years=years, subdiv=state, categories=("public",))
        optional = holidays.Brazil(years=years, subdiv=state, categories=("optional",))
        calendar = pl.DataFrame(
            {
                "date": days,
                "holiday": [int(day in public) for day in days],
                "optional_holiday": [int(day in optional and day not in public) for day in days],
            }
        )
        codes = [code for code, uf in states.items() if uf == state]
        frames.append(pl.DataFrame({"health_region_code": codes}).join(calendar, how="cross"))
    return (
        pl.concat(frames)
        .select("date", "health_region_code", "holiday", "optional_holiday")
        .sort("health_region_code", "date")
    )

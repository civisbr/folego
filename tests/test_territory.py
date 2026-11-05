import json
from pathlib import Path

import polars as pl

from folego.transform.codes import municipality_code


def test_frozen_national_lookup_covers_every_official_municipality():
    directory = Path("web/public/data")
    rows = json.loads((directory / "municipalities.json").read_text())
    report = json.loads((directory / "territory-coverage.json").read_text())
    assert len(rows) == report["ibge_municipalities"] == report["matched"] == 5571
    assert report["coverage"] == 1 and not report["unmapped"]
    assert len({r["municipality_code"] for r in rows}) == len(rows)
    for row in rows:
        assert municipality_code(row["ibge_code"]) == row["municipality_code"]
        assert row["health_region_code"] and row["health_region"]
    geography = json.loads((directory / "regions.geojson").read_text())
    assert {f["properties"]["code"] for f in geography["features"]} == {
        r["health_region_code"] for r in rows
    }
    correction = next(r for r in rows if r["municipality_code"] == "510183")
    assert correction["health_region_code"] == "51014"
    assert correction["population_2022"] is None  # never fabricate population for a new city


def test_demo_is_explicit_and_every_region_has_all_age_day_cells():
    root = Path("web/public/data")
    payload = json.loads((root / "forecast.json").read_text())
    assert payload["mode"] == "demonstration"
    data = pl.DataFrame(payload["rows"])
    assert data.height == 439 * 4 * 5
    assert not data.select("health_region_code", "age_group", "day").is_duplicated().any()
    assert ((data["lower"] <= data["excess"]) & (data["excess"] <= data["upper"])).all()

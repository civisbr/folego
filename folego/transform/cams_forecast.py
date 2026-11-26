"""Archived CAMS PM2.5 forecasts → population-weighted daily series per region and lead day.

Lead day k of the forecast issued at 00 UTC on day d is local day d + k (UTC-3, the same
local day as the reanalysis series). With 3-hourly steps from 3 to 120 hours, local days
d..d+4 each get their eight values; any day short of eight is dropped, never averaged.
"""

import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from ..ingest.cams import pm25_to_micrograms
from .cams_exposure import (
    local_daily_means,
    population_cell_weights,
    regional_daily_means,
    write_series,
)

FORECAST_ATTRIBUTION = (
    "Generated using Copernicus Atmosphere Monitoring Service information {year}. "
    "CAMS global atmospheric composition forecasts."
)
REFERENCE = "forecast_reference_time"
STEP = "forecast_period"
# A monthly forecast zip holds one or two NetCDF files of a few hundred MB at most;
# anything far larger is not ours and could fill the temporary directory.
MAX_MEMBERS = 4
MAX_ARCHIVE_BYTES = 2 * 1024**3
# Values held in memory at once (pm2p5 of one member): ~4 GB of float64 at most.
MAX_FIELD_VALUES = 500_000_000


def _netcdf_members(archive: Path, directory: Path) -> list[Path]:
    paths = []
    with zipfile.ZipFile(archive) as bundle:
        members = [
            m for m in bundle.infolist() if not m.is_dir() and Path(m.filename).name.endswith(".nc")
        ]
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"Too many NetCDF members in {archive.name}")
        if sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES:
            raise ValueError(f"Oversized members in {archive.name}")
        for member in members:
            name = Path(member.filename).name
            # Only the base name is used, so a crafted path cannot escape the directory.
            target = directory / f"{len(paths)}-{name}"
            with bundle.open(member) as source, open(target, "wb") as sink:
                while chunk := source.read(1 << 20):
                    sink.write(chunk)
            paths.append(target)
    if not paths:
        raise ValueError(f"No NetCDF file in {archive.name}")
    return paths


def forecast_daily_cell_means(field, *, utc_offset_hours: int = -3):
    """Yield (UTC issue date, daily DataArray) for every forecast reference time."""
    if REFERENCE not in field.dims or STEP not in field.dims:
        raise ValueError(f"Expected {REFERENCE} and {STEP} dimensions, got {field.dims}")
    extra = [d for d in field.dims if d not in (REFERENCE, STEP, "latitude", "longitude")]
    if any(field.sizes[d] != 1 for d in extra):
        raise ValueError(f"Unexpected dimensions {extra}")
    field = field.isel({d: 0 for d in extra}, drop=True)
    units = field.attrs.get("units")
    steps = _steps(field[STEP])
    for reference in field[REFERENCE].values:
        if reference != reference.astype("datetime64[D]"):
            # A 12 UTC run would shift every lead day by half a day without any error.
            raise ValueError(f"Forecast run {reference} is not a 00 UTC run")
        run = field.sel({REFERENCE: reference}, drop=True)
        valid = reference + steps
        run = (
            run.drop_vars("valid_time", errors="ignore")
            .rename({STEP: "valid_time"})
            .assign_coords(valid_time=valid)
        )
        means = local_daily_means(run, utc_offset_hours=utc_offset_hours, name="CAMS forecast")
        issue = np.datetime64(reference, "D")
        yield issue, pm25_to_micrograms(means, units)


def _steps(coordinate) -> np.ndarray:
    """Forecast steps as timedelta64; ADS files may store them as integer hours."""
    values = coordinate.values
    if np.issubdtype(values.dtype, np.timedelta64):
        return values.astype("timedelta64[ns]")
    units = str(coordinate.attrs.get("units", "")).strip().lower()
    if np.issubdtype(values.dtype, np.number) and units in ("h", "hour", "hours"):
        hours = values.astype("float64")
        if not np.all(hours == np.round(hours)):
            raise ValueError("Forecast steps are not whole hours")
        return hours.astype("int64").astype("timedelta64[h]").astype("timedelta64[ns]")
    raise ValueError(f"Unrecognized forecast step units: {units or values.dtype}")


def regional_forecasts(paths: list[Path], population_raster: Path, regions_geojson: Path):
    import xarray as xr

    weights_by_grid: dict[tuple, pl.DataFrame] = {}
    snapping: dict = {}
    frames = []
    for archive in paths:
        with tempfile.TemporaryDirectory() as directory:
            for member in _netcdf_members(Path(archive), Path(directory)):
                with xr.open_dataset(member) as ds:
                    if "pm2p5" not in ds:
                        raise ValueError(f"No pm2p5 variable in {Path(archive).name}")
                    if ds["pm2p5"].size > MAX_FIELD_VALUES:
                        raise ValueError(f"Forecast field too large in {Path(archive).name}")
                    field = ds["pm2p5"].load()
                for issue, daily in forecast_daily_cell_means(field):
                    grid = (
                        tuple(daily["latitude"].values.round(6)),
                        tuple(daily["longitude"].values.round(6)),
                    )
                    if grid not in weights_by_grid:
                        # The forecast grid changes with system upgrades; weights follow it.
                        weights_by_grid[grid] = population_cell_weights(
                            population_raster,
                            regions_geojson,
                            latitudes=daily["latitude"].values,
                            longitudes=daily["longitude"].values,
                            max_snap_steps=1.0,
                            report=snapping,
                        )
                    regional = regional_daily_means(
                        daily, weights_by_grid[grid], column="pm25_forecast", name="CAMS forecast"
                    )
                    issue_date = pl.lit(issue.item())
                    frames.append(
                        regional.with_columns(issue_date.alias("issue_date")).with_columns(
                            (pl.col("date") - pl.col("issue_date"))
                            .dt.total_days()
                            .cast(pl.Int64)
                            .alias("lead_day")
                        )
                    )
    if not frames:
        raise ValueError("No forecast in the input files")
    series = pl.concat(frames).select(
        "issue_date", "date", "lead_day", "health_region_code", "population", "pm25_forecast"
    )
    if series.select(
        pl.struct("issue_date", "lead_day", "health_region_code").is_duplicated().any()
    ).item():
        raise ValueError("Repeated forecast issue dates; check overlapping files")
    return (
        series.sort("health_region_code", "issue_date", "lead_day"),
        snapping,
        len(weights_by_grid),
    )


def build_forecast_exposure(
    paths: list[Path],
    population_raster: Path,
    regions_geojson: Path,
    output: Path,
    *,
    attribution: tuple[str, ...] = (),
) -> pl.DataFrame:
    series, snapping, grids = regional_forecasts(paths, population_raster, regions_geojson)
    write_series(
        series,
        output,
        inputs={
            "cams_forecasts": list(paths),
            "population": [population_raster],
            "regions": [regions_geojson],
        },
        details={
            "local_day": "UTC-3, mean of 8 three-hourly forecast steps; lead day = local day"
            " minus the UTC date of the 00 UTC run",
            "issue_dates": [str(series["issue_date"].min()), str(series["issue_date"].max())],
            "grids": grids,
            "snapped_beyond_grid": snapping,
            "attribution": [
                FORECAST_ATTRIBUTION.format(year=datetime.now(UTC).year),
                *attribution,
            ],
        },
    )
    return series

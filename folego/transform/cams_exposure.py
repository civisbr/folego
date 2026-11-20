"""CAMS PM2.5 grid → population-weighted daily series per health region."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from ..ingest.cams import pm25_to_micrograms
from ..ingest.common import sha256_file

STEPS_PER_DAY = 8
COPERNICUS_ATTRIBUTION = (
    "Generated using Copernicus Atmosphere Monitoring Service information {year}. "
    "CAMS global reanalysis (EAC4), DOI 10.24381/d58bbf47."
)


def daily_cell_means(path: Path, *, utc_offset_hours: int = -3):
    """Daily CAMS PM2.5 in µg/m³ per grid point; see ``local_daily_means``."""
    import xarray as xr

    with xr.open_dataset(path) as ds:
        field = ds["pm2p5"].load()
    means = local_daily_means(field, utc_offset_hours=utc_offset_hours, name="CAMS PM2.5")
    return pm25_to_micrograms(means, field.attrs.get("units"))


def local_daily_means(field, *, utc_offset_hours: int = -3, name: str = "field"):
    """Mean of the eight 3-hourly values within each local day, per grid point.

    Returns a float64 DataArray (date, latitude, longitude) with ascending coordinates
    and longitudes in −180..180. Days without all eight values are dropped, never
    averaged from fewer.
    """
    field = field.transpose("valid_time", "latitude", "longitude")
    # xarray's mean skips NaN, so a gap would otherwise shrink a day silently.
    if not np.isfinite(field.values).all():
        raise ValueError(f"Missing {name} values")
    if field.indexes["valid_time"].has_duplicates:
        raise ValueError(f"Repeated {name} times; check overlapping files")
    field = field.assign_coords(
        latitude=field["latitude"].astype("float64"),
        longitude=_normalize_longitude(field["longitude"].values.astype("float64")),
    )
    local_date = (field["valid_time"] + np.timedelta64(utc_offset_hours, "h")).dt.floor("D")
    field = field.assign_coords(date=("valid_time", local_date.values))
    grouped = field.groupby("date")
    counts = grouped.count("valid_time").isel(latitude=0, longitude=0)
    complete = counts["date"].values[counts.values == STEPS_PER_DAY]
    # Sorting after the reduction avoids copying the full 3-hourly field.
    means = grouped.mean("valid_time").sel(date=complete).sortby(["latitude", "longitude"])
    return means.astype("float64")


def population_cell_weights(
    population_raster: Path,
    regions_geojson: Path,
    *,
    latitudes,
    longitudes,
    max_snap_steps: float = 0.5,
    report: dict | None = None,
) -> pl.DataFrame:
    """Population of each (CAMS grid point × health region) intersection.

    Each population pixel goes to the region containing its centre and to the nearest
    CAMS grid point, so the weights follow where people live, not the area. Pixels
    beyond half a grid step from every point (e.g. islands past the request area) are
    snapped only up to ``max_snap_steps`` steps and counted in ``report``.
    """
    import rasterio
    from rasterio.features import rasterize

    features = json.loads(Path(regions_geojson).read_text())["features"]
    codes = [str(f["properties"]["code"]) for f in features]
    if len(set(codes)) != len(codes):
        raise ValueError("Duplicate health region codes in the geometry file")
    with rasterio.open(population_raster) as source:
        if source.crs is None or source.crs.to_epsg() != 4326:
            raise ValueError("Population raster must be in EPSG:4326 like the regions")
        transform = source.transform
        if transform.b or transform.d:
            raise ValueError("Rotated population rasters are not supported")
        population = source.read(1, masked=True).filled(0).astype("float64")
    region_index = rasterize(
        ((f["geometry"], i + 1) for i, f in enumerate(features)),
        out_shape=population.shape,
        transform=transform,
        fill=0,
        dtype="int32",
    )
    population[~np.isfinite(population) | (population < 0)] = 0
    height, width = population.shape
    # Without rotation every pixel in a row shares a latitude and every pixel in a
    # column a longitude, so the nearest grid point is found once per row/column.
    row_lat = transform.f + (np.arange(height) + 0.5) * transform.e
    col_lon = _normalize_longitude(transform.c + (np.arange(width) + 0.5) * transform.a)
    lat_axis = np.asarray(latitudes, dtype="float64")
    lon_axis = _normalize_longitude(np.asarray(longitudes, dtype="float64"))
    lat_index, lat_steps = _nearest(row_lat, lat_axis)
    lon_index, lon_steps = _nearest(col_lon, lon_axis)
    rows, cols = np.nonzero((region_index > 0) & (population > 0))
    distance = np.maximum(lat_steps[rows], lon_steps[cols])
    if (distance > max_snap_steps + 1e-9).any():
        raise ValueError("Populated pixels fall outside the CAMS grid; enlarge the request area")
    snapped = distance > 0.5 + 1e-9
    if report is not None:
        report["snapped_pixels"] = int(snapped.sum())
        report["snapped_population"] = float(population[rows[snapped], cols[snapped]].sum())
        report["snapped_regions"] = sorted(
            {codes[i - 1] for i in np.unique(region_index[rows[snapped], cols[snapped]])}
        )
    frame = pl.DataFrame(
        {
            "latitude": lat_axis[lat_index][rows],
            "longitude": lon_axis[lon_index][cols],
            "region": region_index[rows, cols],
            "population": population[rows, cols],
        }
    )
    del population, region_index, rows, cols
    weights = (
        frame.group_by("latitude", "longitude", "region")
        .agg(pl.col("population").sum())
        .with_columns(
            pl.col("region")
            .replace_strict(dict(enumerate(codes, start=1)), return_dtype=pl.String)
            .alias("health_region_code")
        )
        .select("latitude", "longitude", "health_region_code", "population")
        .sort("health_region_code", "latitude", "longitude")
    )
    missing = set(codes) - set(weights["health_region_code"])
    if missing:
        raise ValueError(f"{len(missing)} health regions have no populated pixel")
    return weights


def regional_daily_pm25(daily, weights: pl.DataFrame) -> pl.DataFrame:
    return regional_daily_means(daily, weights, column="pm25", name="CAMS")


def regional_daily_means(
    daily, weights: pl.DataFrame, *, column: str, name: str = "source"
) -> pl.DataFrame:
    """Population-weighted mean of the daily grid for every region and date."""
    lat_axis = daily["latitude"].values
    lon_axis = daily["longitude"].values
    lat_index = _exact_index(weights["latitude"].to_numpy(), lat_axis)
    lon_index = _exact_index(weights["longitude"].to_numpy(), lon_axis)
    if (lat_index < 0).any() or (lon_index < 0).any():
        raise ValueError(f"Weights refer to points missing from the {name} grid")
    if daily.sizes["date"] == 0:
        raise ValueError(f"No complete local day in the {name} data")
    codes, region = np.unique(weights["health_region_code"].to_numpy(), return_inverse=True)
    population = weights["population"].to_numpy()
    if not (np.isfinite(population).all() and (population >= 0).all()):
        raise ValueError("Invalid population weights")
    matrix = np.zeros((len(codes), lat_axis.size * lon_axis.size))
    np.add.at(matrix, (region, lat_index * lon_axis.size + lon_index), population)
    totals = matrix.sum(axis=1)
    if (totals <= 0).any():
        raise ValueError("Zero regional population")
    values = daily.values.reshape(daily.sizes["date"], -1)
    regional = (values @ matrix.T) / totals
    dates = daily["date"].values.astype("datetime64[D]")
    return pl.DataFrame(
        {
            "date": np.repeat(dates, len(codes)),
            "health_region_code": np.tile(codes, len(dates)),
            "population": np.tile(totals, len(dates)),
            column: regional.ravel(),
        }
    ).sort("health_region_code", "date")


def build_exposure(
    cams_file: Path,
    population_raster: Path,
    regions_geojson: Path,
    output: Path,
    *,
    attribution: tuple[str, ...] = (),
) -> pl.DataFrame:
    daily = daily_cell_means(cams_file)
    snapping = {}
    weights = population_cell_weights(
        population_raster,
        regions_geojson,
        latitudes=daily["latitude"].values,
        longitudes=daily["longitude"].values,
        # One step admits Fernando de Noronha (lon −32.4) when the request stops at −33.
        max_snap_steps=1.0,
        report=snapping,
    )
    series = regional_daily_pm25(daily, weights)
    write_series(
        series,
        output,
        inputs={
            "cams": [cams_file],
            "population": [population_raster],
            "regions": [regions_geojson],
        },
        details={
            "local_day": "UTC-3, mean of 8 three-hourly analyses",
            "snapped_beyond_grid": snapping,
            "attribution": [
                COPERNICUS_ATTRIBUTION.format(year=datetime.now(UTC).year),
                *attribution,
            ],
        },
    )
    return series


def write_series(
    series: pl.DataFrame, output: Path, *, inputs: dict[str, list[Path]], details: dict
) -> None:
    """Write a regional daily series with a manifest of every input's hash and source."""
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "rows": series.height,
        "regions": series["health_region_code"].n_unique(),
        "dates": [str(series["date"].min()), str(series["date"].max())],
        **details,
        "inputs": {
            name: [describe_input(Path(p)) for p in paths] for name, paths in inputs.items()
        },
    }
    write_with_manifest(output, series.write_parquet, manifest)


def write_with_manifest(output: Path, write, manifest: dict) -> None:
    """Replace the output and its manifest together, so neither outlives the other."""
    sidecar = output.with_suffix(output.suffix + ".source.json")
    staged = output.with_name(output.name + ".tmp")
    staged_sidecar = sidecar.with_name(sidecar.name + ".tmp")
    try:
        write(staged)
        staged_sidecar.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(staged, output)
        os.replace(staged_sidecar, sidecar)
    finally:
        staged.unlink(missing_ok=True)
        staged_sidecar.unlink(missing_ok=True)


def describe_input(path: Path) -> dict:
    # cached_download appends ".source.json"; the ADS/CDS helpers replace the suffix.
    candidates = (
        path.with_suffix(path.suffix + ".source.json"),
        path.with_suffix(".source.json"),
    )
    provenance = next((p for p in candidates if p.exists()), None)
    return {
        # Relative paths keep the operator's home directory out of shareable manifests.
        "path": _portable(path),
        "sha256": sha256_file(path),
        "source": json.loads(provenance.read_text()) if provenance else None,
    }


def _portable(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path.name


def _normalize_longitude(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype="float64")
    # Only rewrite 0..360 grids: the arithmetic can flip the last bit of exact values.
    if (values >= 180.0).any():
        return ((values + 180.0) % 360.0) - 180.0
    return values


def _nearest(values: np.ndarray, axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index of the nearest axis point and the distance to it in grid steps."""
    order = np.argsort(axis)
    ordered = axis[order]
    if len(ordered) < 2:
        raise ValueError("CAMS grid needs at least two points per axis")
    right = np.clip(np.searchsorted(ordered, values), 1, len(ordered) - 1)
    left = right - 1
    pick_left = np.abs(values - ordered[left]) <= np.abs(values - ordered[right])
    index = np.where(pick_left, left, right)
    steps = np.abs(values - ordered[index]) / np.min(np.diff(ordered))
    return order[index], steps


def _exact_index(values: np.ndarray, axis: np.ndarray) -> np.ndarray:
    lookup = {float(v): i for i, v in enumerate(axis)}
    return np.array([lookup.get(float(v), -1) for v in values], dtype="int64")

"""ERA5 2 m temperature and humidity → daily local means per grid point."""

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import polars as pl

from .cams_exposure import (
    local_daily_means,
    population_cell_weights,
    regional_daily_means,
    write_series,
)

ERA5_ATTRIBUTION = (
    "Contains modified Copernicus Climate Change Service information {year}. "
    "Hersbach et al., ERA5 hourly data on single levels, DOI 10.24381/cds.adbb2d47."
)
# Fog and sea points make dew point exceed temperature slightly; those are clamped and
# counted. Excesses this large only come from a corrupt or mislabelled file.
DEWPOINT_ABORT_K = 5.0


def relative_humidity(temperature_c, dewpoint_c):
    """Relative humidity (%) from the Magnus formula of Alduchov & Eskridge (1996)."""

    def saturation(t):
        return 6.1094 * np.exp(17.625 * t / (t + 243.04))

    return 100.0 * saturation(dewpoint_c) / saturation(temperature_c)


def daily_meteorology(paths: list[Path], *, utc_offset_hours: int = -3) -> dict:
    """Daily mean temperature (°C) and relative humidity (%) from yearly ERA5 files.

    Humidity is computed for every 3-hourly step before averaging, because the humidity
    of mean temperature and dew point overstates dry afternoons. Each file is processed
    with the first step of the next one, so the last local day of a year stays complete
    without loading every year at once.
    """
    import xarray as xr

    datasets = [xr.open_dataset(path) for path in paths]
    try:
        datasets.sort(key=lambda ds: ds["valid_time"].values[0])
        temperature, humidity = [], []
        clamped = {"steps": 0, "max_excess_k": 0.0}
        for index, ds in enumerate(datasets):
            frame = ds[["t2m", "d2m"]].load()
            if index + 1 < len(datasets):
                carry = datasets[index + 1][["t2m", "d2m"]].isel(valid_time=[0]).load()
                frame = xr.concat([frame, carry], dim="valid_time")
            t = _celsius(frame["t2m"])
            d = _celsius(frame["d2m"])
            excess = float((d - t).max())
            if excess > DEWPOINT_ABORT_K:
                raise ValueError("ERA5 dew point exceeds temperature by more than 5 K")
            clamped["steps"] += int((d > t).sum())
            clamped["max_excess_k"] = max(clamped["max_excess_k"], excess, 0.0)
            rh = relative_humidity(t, np.minimum(d, t))
            temperature.append(
                local_daily_means(t, utc_offset_hours=utc_offset_hours, name="ERA5 temperature")
            )
            humidity.append(
                local_daily_means(rh, utc_offset_hours=utc_offset_hours, name="ERA5 humidity")
            )
    finally:
        for ds in datasets:
            ds.close()
    result = {
        "temperature": xr.concat(temperature, dim="date"),
        "humidity": xr.concat(humidity, dim="date"),
        "dewpoint_clamped": clamped,
    }
    dates = result["temperature"].indexes["date"]
    if dates.has_duplicates:
        raise ValueError("Overlapping ERA5 files produce the same local day twice")
    gaps = np.diff(dates.values).astype("timedelta64[D]").astype(int)
    if (gaps != 1).any():
        raise ValueError("ERA5 files are not contiguous; a year is missing")
    return result


def build_meteorology(
    era5_files: list[Path],
    population_raster: Path,
    regions_geojson: Path,
    output: Path,
    *,
    attribution: tuple[str, ...] = (),
) -> pl.DataFrame:
    """Population-weighted daily temperature and humidity per health region."""
    daily = daily_meteorology(era5_files)
    grid = daily["temperature"]
    snapping = {}
    weights = population_cell_weights(
        population_raster,
        regions_geojson,
        latitudes=grid["latitude"].values,
        longitudes=grid["longitude"].values,
        max_snap_steps=1.0,
        report=snapping,
    )
    temperature = regional_daily_means(
        daily["temperature"], weights, column="temperature", name="ERA5"
    )
    humidity = regional_daily_means(daily["humidity"], weights, column="humidity", name="ERA5")
    series = temperature.join(
        humidity.drop("population"),
        on=["health_region_code", "date"],
        how="inner",
        validate="1:1",
    )
    write_series(
        series,
        output,
        inputs={
            "era5": list(era5_files),
            "population": [population_raster],
            "regions": [regions_geojson],
        },
        details={
            "local_day": "UTC-3, mean of 8 three-hourly steps",
            "humidity": "Magnus formula per step, then daily mean",
            "dewpoint_clamped_to_temperature": daily["dewpoint_clamped"],
            "snapped_beyond_grid": snapping,
            "attribution": [
                ERA5_ATTRIBUTION.format(year=datetime.now(UTC).year),
                *attribution,
            ],
        },
    )
    return series


def _celsius(field):
    if field.attrs.get("units") != "K":
        raise ValueError(f"Unexpected ERA5 units: {field.attrs.get('units')}")
    return field - 273.15

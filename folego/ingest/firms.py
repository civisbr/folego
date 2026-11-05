import os
from pathlib import Path

import polars as pl

from .common import cached_download


def download_firms(day: str, days=1, root=Path("data")):
    from datetime import date

    date.fromisoformat(day)
    if not 1 <= days <= 10:
        raise ValueError("FIRMS supports a 1–10 day range")
    key = os.environ["FIRMS_MAP_KEY"]
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_SNPP_NRT/-74,-34,-34,6/{days}/{day}"
    return cached_download(url, root / "raw" / "firms" / f"{day}-{days}.csv", sensitive=True)


def aggregate_fires(assigned_pixels: pl.DataFrame):
    """Pixels must be spatially assigned to a verified health-region polygon."""
    return assigned_pixels.group_by("date", "health_region_code").agg(
        pl.len().alias("fire_count"), pl.col("frp").sum().alias("frp")
    )

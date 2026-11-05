"""ERA5 history; operational prediction needs archived meteorological forecasts."""

import hashlib
import json
from pathlib import Path


def retrieve_era5(request: dict, root=Path("data")):
    import cdsapi

    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:20]
    target = root / "raw" / "era5" / f"{key}.nc"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.nc")
    try:
        cdsapi.Client(url="https://cds.climate.copernicus.eu/api").retrieve(
            "reanalysis-era5-single-levels", request, str(temporary)
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    target.with_suffix(".request.json").write_text(json.dumps(request, indent=2))
    return target

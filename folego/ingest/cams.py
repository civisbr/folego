"""Use requests exported from the ADS UI, preserving the exact dataset vintage."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ALLOWED = {"cams-global-reanalysis-eac4", "cams-global-atmospheric-composition-forecasts"}


def retrieve_cams(dataset: str, request: dict, root=Path("data")) -> Path:
    if dataset not in ALLOWED:
        raise ValueError("Unsupported CAMS dataset")
    import cdsapi

    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:20]
    # The forecast dataset only offers netcdf inside a zip; naming it .nc would mislead readers.
    suffix = ".zip" if request.get("data_format") == "netcdf_zip" else ".nc"
    target = root / "raw" / "cams" / dataset / f"{digest}{suffix}"
    if target.exists():
        # A swapped or truncated cache file must not pass silently into a new analysis.
        recorded = target.with_suffix(".source.json")
        if recorded.exists():
            expected = json.loads(recorded.read_text()).get("sha256")
            if expected != hashlib.sha256(target.read_bytes()).hexdigest():
                raise ValueError(f"Cached {target.name} does not match its recorded sha256")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f".tmp{suffix}")
    # ADS credentials belong in ~/.cdsapirc or environment, never in this repository.
    try:
        cdsapi.Client(url="https://ads.atmosphere.copernicus.eu/api").retrieve(
            dataset, request, str(temporary)
        )
        temporary.replace(target)
        target.with_suffix(".source.json").write_text(
            json.dumps(
                {
                    "dataset": dataset,
                    "request": request,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                },
                indent=2,
            )
        )
    finally:
        temporary.unlink(missing_ok=True)
    return target


def pm25_to_micrograms(values, unit: str):
    if unit in ("kg m**-3", "kg m-3", "kg m^-3"):
        return values * 1e9
    if unit in ("µg/m³", "ug m-3", "ug m**-3"):
        return values
    raise ValueError(f"Unknown PM2.5 units: {unit}")

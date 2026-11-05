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
    target = root / "raw" / "cams" / dataset / f"{digest}.nc"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp.nc")
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

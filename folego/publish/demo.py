"""Deterministic interface fixtures. These numbers are NEVER scientific outputs."""

import json
import math
from pathlib import Path

AGE_FACTORS = {"0-4": 0.27, "5-14": 0.12, "15-59": 0.23, "60+": 0.38}


def build_demo(geography=Path("web/public/data/regions.geojson"), output=Path("web/public/data")):
    features = json.loads(geography.read_text())["features"]
    rows = []
    for feature in features:
        p = feature["properties"]
        code = p["code"]
        lon, lat = p["center"]
        seed = int(code)
        # Geographic pattern solely makes the reference design explorable.
        intensity = max(0.02, math.exp(-((lon + 62) ** 2 / 160 + (lat + 10) ** 2 / 100)))
        for age, factor in AGE_FACTORS.items():
            for day in range(5):
                excess = round((2 + 27 * intensity + seed % 7) * factor * (1 + day * 0.1), 2)
                rows.append(
                    {
                        "health_region_code": code,
                        "age_group": age,
                        "day": day,
                        "excess": excess,
                        "lower": round(excess * 0.68, 2),
                        "upper": round(excess * 1.38, 2),
                        "payment": round(excess * (1250 + seed % 600), 2),
                        "pm25": round(8 + intensity * 75 + day * 2, 1),
                        "baseline": round(120 * factor, 2),
                    }
                )
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "mode": "demonstration",
        "generated_at": "2026-11-04T00:00:00Z",
        "forecast_start": "2026-11-05",
        "interval": "Faixa ilustrativa; não é intervalo calibrado",
        "geography_source": "IBGE / Ministério da Saúde",
        "disclaimer": "Dados sintéticos para demonstração. Não usar para decisões de saúde.",
        "data_license": "CC-BY-4.0",
        "rows": rows,
    }
    (output / "forecast.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
    return len(rows)

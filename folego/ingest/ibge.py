"""Versioned IBGE/Ministry of Health correspondence; never infer missing regions."""

import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import polars as pl

from .common import cached_download

IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
HEALTH_URL = "https://apidadosabertos.saude.gov.br/macrorregiao-e-regiao-de-saude/municipio"
BOUNDARIES_URL = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?formato=application/vnd.geo%2Bjson&qualidade=minima&intrarregiao=municipio"
)


def build_correspondence(root=Path("data"), *, refresh=False, require_complete=True):
    raw = root / "raw" / "territory"
    cities = json.loads(cached_download(IBGE_URL, raw / "ibge.json", refresh=refresh).read_text())
    regions = []
    # API caps large limit values silently. Use small pages and advance by actual length.
    offset = 0
    while True:
        url = f"{HEALTH_URL}?limit=500&offset={offset}"
        page = json.loads(
            cached_download(url, raw / f"health-{offset}.json", refresh=refresh).read_text()
        )
        page = page["macrorregiao_regiao_saude_municipios"]
        if not page:
            break
        regions.extend(page)
        offset += len(page)
        if offset > 20000:
            raise ValueError("Unexpected pagination; refusing partial lookup")
    health = {str(r["codigo_municipio"]): r for r in regions}
    if len(health) != len(regions):
        raise ValueError("Duplicate Ministry of Health municipalities")
    overrides = json.loads(
        files("folego").joinpath("resources/territory-overrides.json").read_text()
    )
    applied = []
    rows = []
    for city in cities:
        code = str(city["id"])
        # Some official IBGE historical codes have exceptional check digits.
        normalized = code[:6]
        region = health.get(normalized, {})
        if not region and normalized in overrides:
            region = overrides[normalized]
            applied.append({"municipality_code": normalized, **region})
        uf = city["regiao-imediata"]["regiao-intermediaria"]["UF"]["sigla"]
        rows.append(
            {
                "ibge_code": code,
                "municipality_code": normalized,
                "municipality": city["nome"],
                "state": uf,
                "health_region_code": region.get("codigo_regiao_saude"),
                "health_region": region.get("regiao_saude"),
                "population_2022": region.get("populacao_estimada_ibge_2022"),
            }
        )
    frame = pl.DataFrame(rows)
    output = root / "staging"
    output.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(output / "municipalities.parquet")
    missing = frame.filter(pl.col("health_region_code").is_null())
    report = {
        "ibge_municipalities": frame.height,
        "matched": frame.height - missing.height,
        "coverage": (frame.height - missing.height) / frame.height,
        "unmapped": missing.to_dicts(),
        "sources": [IBGE_URL, HEALTH_URL],
        "overrides": applied,
        "snapshot_date": datetime.now(UTC).date().isoformat(),
    }
    (output / "territory-coverage.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    if require_complete and missing.height:
        raise ValueError(
            f"{missing.height} municipality mappings missing; see {output}/territory-coverage.json"
        )
    return frame, report


def download_boundaries(root=Path("data")):
    return cached_download(BOUNDARIES_URL, root / "raw" / "territory" / "boundaries.geojson")

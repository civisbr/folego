"""Versioned IBGE/Ministry of Health correspondence; never infer missing regions."""

import json
import re
import shutil
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import polars as pl

from .common import cached_download

IBGE_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
HEALTH_URL = "https://apidadosabertos.saude.gov.br/macrorregiao-e-regiao-de-saude/municipio"
HEALTH_PAGE_SIZE = 500
# ~5,570 municipalities need 12 pages; the cap only stops a server that never ends.
MAX_HEALTH_PAGES = 40
BOUNDARIES_URL = (
    "https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
    "?formato=application/vnd.geo%2Bjson&qualidade=minima&intrarregiao=municipio"
)


def build_correspondence(root=Path("data"), *, refresh=False, require_complete=True):
    raw = root / "raw" / "territory"
    cities = json.loads(cached_download(IBGE_URL, raw / "ibge.json", refresh=refresh).read_text())
    health = _health_regions(raw, refresh=refresh)
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
    # Written only after the check so a failed run cannot leave a partial lookup behind.
    frame.write_parquet(output / "municipalities.parquet")
    return frame, report


def _health_regions(raw: Path, *, refresh: bool) -> dict:
    """Fetch all pages as one set so a cache never mixes pages from different runs."""
    pages_dir = raw / "health-pages"
    marker = pages_dir / "complete.json"
    if not refresh and marker.exists():
        count = json.loads(marker.read_text())["pages"]
        pages = [_read_page(pages_dir / f"page-{n}.json") for n in range(count)]
        return _index_health_pages(pages)
    staging = raw / "health-pages.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    pages = []
    # The API treats `offset` as a page index, not a record offset (offset=1 with
    # limit=500 starts at record 500); record offsets broke the daily audit in Nov 2026.
    for page_number in range(MAX_HEALTH_PAGES):
        url = f"{HEALTH_URL}?limit={HEALTH_PAGE_SIZE}&offset={page_number}"
        page = _read_page(cached_download(url, staging / f"page-{page_number}.json", refresh=True))
        if not page:
            break
        pages.append(page)
        # Validating every page stops a server that ignores `offset` on the second request.
        health = _index_health_pages(pages)
    else:
        raise ValueError("Unexpected pagination; refusing partial lookup")
    health = _index_health_pages(pages)
    (staging / "complete.json").write_text(json.dumps({"pages": len(pages)}))
    shutil.rmtree(pages_dir, ignore_errors=True)
    staging.rename(pages_dir)
    return health


def _read_page(path: Path) -> list:
    page = json.loads(path.read_text())["macrorregiao_regiao_saude_municipios"]
    if not isinstance(page, list):
        raise ValueError("Ministry of Health page is not a list")
    return page


def _index_health_pages(pages: list[list]) -> dict:
    health = {}
    for number, page in enumerate(pages):
        if len(page) < HEALTH_PAGE_SIZE and number != len(pages) - 1:
            raise ValueError("Short Ministry of Health page before the last; records skipped")
        for record in page:
            code = str(record["codigo_municipio"])
            if not re.fullmatch(r"\d{6}", code):
                raise ValueError(f"Invalid Ministry of Health municipality code: {code!r}")
            if code in health:
                raise ValueError(
                    "Ministry of Health pagination returned overlapping pages; "
                    "offset semantics may have changed again"
                )
            health[code] = record
    return health


def download_boundaries(root=Path("data")):
    return cached_download(BOUNDARIES_URL, root / "raw" / "territory" / "boundaries.geojson")

"""IPCA monthly price index from IBGE/SIDRA, used to deflate AIH payments."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from .common import cached_download

# Table 1737, variable 2266: IPCA number index, base December 1993 = 100.
SIDRA_IPCA_URL = "https://apisidra.ibge.gov.br/values/t/1737/n1/all/v/2266/p/all"
IPCA_VARIABLE = "2266"
IPCA_ATTRIBUTION = "Fonte: IBGE, Sistema Nacional de Índices de Preços ao Consumidor (SIDRA 1737)."
# Column order in SIDRA responses follows the dimensions in the URL; check the labels.
SNAPSHOT_NAME = re.compile(r"sidra-1737-\d{8}\.json")
EXPECTED_HEADER = {"V": "Valor", "D2C": "Variável (Código)", "D3C": "Mês (Código)"}


def download_ipca(root=Path("data"), *, refresh=False) -> tuple[pl.DataFrame, Path]:
    """Latest local snapshot, or a new dated one when refreshing.

    Each snapshot keeps its own file and hash, so payments deflated earlier can still be
    traced to the exact series used.
    """
    folder = root / "raw" / "ipca"
    # The glob alone would also match the ".json.source.json" manifests.
    snapshots = sorted(
        p for p in folder.glob("sidra-1737-*.json") if SNAPSHOT_NAME.fullmatch(p.name)
    )
    if refresh or not snapshots:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        path = cached_download(SIDRA_IPCA_URL, folder / f"sidra-1737-{stamp}.json")
    else:
        path = snapshots[-1]
    return parse_sidra_ipca(path), path


def parse_sidra_ipca(path: Path) -> pl.DataFrame:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("Unexpected SIDRA response structure")
    header, records = payload[0], payload[1:]
    if any(header.get(key) != label for key, label in EXPECTED_HEADER.items()):
        raise ValueError("SIDRA columns are not value/variable/month as expected")
    if not records:
        raise ValueError("Empty IPCA response")
    months, values = [], []
    for record in records:
        if not isinstance(record, dict) or not EXPECTED_HEADER.keys() <= record.keys():
            raise ValueError("Unexpected SIDRA record structure")
        if record["D2C"] != IPCA_VARIABLE:
            raise ValueError("SIDRA response is not the IPCA number index")
        code = record["D3C"]
        if not re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", code):
            raise ValueError(f"Unexpected SIDRA month code: {code!r}")
        try:
            value = float(record["V"])
        except ValueError as error:
            raise ValueError(f"Missing IPCA value for {code}") from error
        if not value > 0:
            raise ValueError(f"Non-positive IPCA value for {code}")
        months.append(date(int(code[:4]), int(code[4:]), 1))
        values.append(value)
    frame = pl.DataFrame({"month": months, "ipca_index": values}).sort("month")
    expected = pl.date_range(frame["month"][0], frame["month"][-1], "1mo", eager=True)
    if frame.height != expected.len() or not (frame["month"] == expected).all():
        raise ValueError("IPCA months are repeated or have gaps")
    return frame

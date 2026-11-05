"""SIH reduced AIH downloads by competence; microdata remain local."""

from pathlib import Path

import polars as pl

from .common import cached_download

STATES = "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()


def download_sih(state: str, year: int, month: int, root=Path("data")) -> Path:
    if state not in STATES or not 2012 <= year <= 2099 or not 1 <= month <= 12:
        raise ValueError("Invalid SIH partition")
    filename = f"RD{state}{year % 100:02d}{month:02d}.dbc"
    url = f"ftp://ftp.datasus.gov.br/dissemin/publicos/SIHSUS/200801_/Dados/{filename}"
    return cached_download(url, root / "raw" / "sih" / str(year) / state / filename)


def convert_sih(source: Path, destination: Path) -> Path:
    if destination.exists():
        return destination
    # PySUS is optional: heavy native DBC decompression dependencies stay out of core.
    from pysus.utilities.readdbc import read_dbc

    frame = pl.from_pandas(read_dbc(str(source), encoding="latin-1"))
    required = [
        "MUNIC_RES",
        "DT_INTER",
        "DIAG_PRINC",
        "IDADE",
        "COD_IDADE",
        "VAL_TOT",
        "N_AIH",
        "IDENT",
    ]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing SIH fields: {missing}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.parquet")
    frame.select(required).write_parquet(temporary)
    temporary.replace(destination)
    return destination

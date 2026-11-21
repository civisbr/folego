"""SIH reduced AIH downloads by competence; microdata remain local."""

import shutil
import struct
from pathlib import Path

import polars as pl

from .common import cached_download

FIELDS = [
    "ANO_CMPT",
    "MES_CMPT",
    "N_AIH",
    "IDENT",
    "MUNIC_RES",
    "MUNIC_MOV",
    "DT_INTER",
    "DT_SAIDA",
    "DIAG_PRINC",
    "COD_IDADE",
    "IDADE",
    "VAL_TOT",
]
CLINICAL_FIELDS = [
    "N_AIH",
    "MUNIC_RES",
    "MUNIC_MOV",
    "DT_INTER",
    "DT_SAIDA",
    "DIAG_PRINC",
    "COD_IDADE",
    "IDADE",
    "VAL_TOT",
]
SCHEMA = {name: pl.String for name in FIELDS if name not in ("IDADE", "VAL_TOT")}
SCHEMA.update(IDADE=pl.Int64, VAL_TOT=pl.Float64)


def ensure_dbc_reserve(source: Path, directory: Path) -> int:
    """Return the DBF size the DBC header declares; refuse if it plus 512 MiB won't fit."""
    with source.open("rb") as stream:
        header = stream.read(32)
    if len(header) != 32:
        raise ValueError("Truncated DBC header")
    count, header_size, record_size = struct.unpack_from("<IHH", header, 4)
    expected = header_size + count * record_size
    if shutil.disk_usage(directory).free < expected + 512 * 1024**2:
        raise ValueError("Insufficient disk reserve for DBC decompression")
    return expected


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
    # Use the maintained standalone PySUS reader without its DuckDB <2 dependency.
    from tempfile import TemporaryDirectory

    from dbfread import DBF
    from pyreaddbc import dbc2dbf

    with TemporaryDirectory(prefix="folego-dbf-") as directory:
        dbf = Path(directory) / "admissions.dbf"
        dbc2dbf(str(source), str(dbf))
        frame = pl.from_dicts(list(DBF(str(dbf), encoding="latin-1")))
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

"""National SIH extraction: diagnosis projection, provenance and DBC deletion."""

import json
import shutil
import struct
import sys
import types
from datetime import date
from urllib.error import URLError

import polars as pl
import pytest

pytest.importorskip("dbfread")

from folego.ingest import sih_national  # noqa: E402
from folego.ingest.sih import FIELDS  # noqa: E402

WIDTHS = {"N_AIH": 13, "DIAG_PRINC": 4, "MUNIC_RES": 6, "MUNIC_MOV": 6}


def make_sih_dbf(path, rows):
    fields = [
        (name, "N" if name in ("IDADE", "VAL_TOT") else "C", WIDTHS.get(name, 8)) for name in FIELDS
    ]
    length = 1 + sum(size for _, _, size in fields)
    header = bytearray(32)
    header[0] = 3
    struct.pack_into("<IHH", header, 4, len(rows), 32 + 32 * len(fields) + 1, length)
    result = bytes(header)
    for name, kind, size in fields:
        field = bytearray(32)
        field[: len(name)] = name.encode()
        field[11] = ord(kind)
        field[16] = size
        result += bytes(field)
    result += b"\r"
    for row in rows:
        result += b" "
        for name, kind, size in fields:
            value = str(row.get(name, "")).encode("latin-1")
            result += value.rjust(size) if kind == "N" else value.ljust(size)
    path.write_bytes(result + b"\x1a")


def row(n, diagnosis, *, year=None, month=None):
    # None takes the competence of the file being served.
    return {
        "ANO_CMPT": year,
        "MES_CMPT": month,
        "N_AIH": f"{n:013d}",
        "IDENT": "1",
        "MUNIC_RES": "355030",
        "MUNIC_MOV": "355030",
        "DT_INTER": "20190105",
        "DT_SAIDA": "20190108",
        "DIAG_PRINC": diagnosis,
        "COD_IDADE": "4",
        "IDADE": "70",
        "VAL_TOT": "100.50",
    }


@pytest.fixture
def fake_source(tmp_path, monkeypatch):
    """Serve a plain DBF as the "DBC" and make decompression a copy."""
    rows = {
        "value": [row(1, "J180"), row(2, "A09"), row(3, "I21"), row(4, "s720"), row(5, "T07")],
        "missing": {("SP", 2026, 9)},
        "broken": set(),
    }

    def download(state, year, month, root):
        source = (
            root / "raw" / "sih" / str(year) / state / f"RD{state}{year % 100:02d}{month:02d}.dbc"
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        if (state, year, month) in rows["missing"]:
            raise URLError("ftp error: error_perm('550 Failed to open file.')")
        if (state, year, month) in rows["broken"]:
            raise URLError("ftp error: timed out")
        make_sih_dbf(
            source,
            [
                {
                    **r,
                    "ANO_CMPT": r["ANO_CMPT"] or str(year),
                    "MES_CMPT": r["MES_CMPT"] or f"{month:02d}",
                }
                for r in rows["value"]
            ],
        )
        from folego.ingest.common import sha256_file

        source.with_suffix(".dbc.source.json").write_text(
            json.dumps(
                {
                    "url": f"ftp://example/{source.name}",
                    "retrieved_at": "t",
                    "sha256": sha256_file(source),
                }
            )
        )
        return source

    fake = types.ModuleType("pyreaddbc")
    fake.dbc2dbf = shutil.copyfile
    monkeypatch.setitem(sys.modules, "pyreaddbc", fake)
    monkeypatch.setattr(sih_national, "download_sih", download)
    monkeypatch.setattr(sih_national, "ensure_dbc_reserve", lambda source, directory: 10**9)
    return rows


def test_partition_keeps_study_chapters_records_provenance_and_deletes_dbc(tmp_path, fake_source):
    root = tmp_path / "data"
    metadata = sih_national.extract_national_partition("SP", 2019, 1, root)
    output = sih_national.partition_path(root, "SP", 2019, 1)
    frame = pl.read_parquet(output)
    assert frame["DIAG_PRINC"].to_list() == ["J180", "I21", "s720", "T07"]
    assert metadata["scanned_records"] == 5 and metadata["rows"] == 4
    assert len(metadata["source_sha256"]) == 64
    assert metadata["source_url"].endswith("RDSP1901.dbc")
    assert not list((root / "raw").rglob("*.dbc*"))
    assert json.loads(output.with_suffix(".source.json").read_text()) == metadata
    # A second run reuses the Parquet and never downloads again.
    fake_source["value"] = []
    assert sih_national.extract_national_partition("SP", 2019, 1, root) == metadata


def test_tampered_partition_and_wrong_competence_fail(tmp_path, fake_source):
    root = tmp_path / "data"
    sih_national.extract_national_partition("SP", 2019, 1, root)
    output = sih_national.partition_path(root, "SP", 2019, 1)
    output.write_bytes(output.read_bytes() + b"x")
    with pytest.raises(ValueError, match="changed"):
        sih_national.extract_national_partition("SP", 2019, 1, root)
    fake_source["value"] = [row(1, "J180", month="02")]
    with pytest.raises(ValueError, match="competence"):
        sih_national.extract_national_partition("RJ", 2019, 1, root)
    assert not sih_national.partition_path(root, "RJ", 2019, 1).exists()


def test_run_lists_unpublished_months_and_writes_coverage(tmp_path, fake_source):
    root = tmp_path / "data"
    report = sih_national.extract_national(
        date(2026, 8, 1), date(2026, 9, 1), root, states=["SP", "RJ"], progress=lambda _: None
    )
    assert report["partitions_expected"] == 4
    assert report["partitions_extracted"] == 3
    assert report["unpublished"] == ["SP 2026-09"]
    assert report["failed"] == []
    saved = json.loads((root / "staging/sih/national/coverage.json").read_text())
    assert saved["partitions"] == 3 and len(saved["partition_manifest_sha256"]) == 64
    assert saved["last_run"]["unpublished"] == ["SP 2026-09"]


def test_competence_range_is_inclusive_and_ordered():
    assert sih_national.competences(date(2012, 11, 1), date(2013, 2, 1)) == [
        (2012, 11),
        (2012, 12),
        (2013, 1),
        (2013, 2),
    ]
    with pytest.raises(ValueError):
        sih_national.competences(date(2013, 2, 1), date(2012, 11, 1))


def test_gaps_before_the_last_published_month_and_other_errors_fail(tmp_path, fake_source):
    root = tmp_path / "data"
    fake_source["missing"] = {("SP", 2026, 7)}
    fake_source["broken"] = {("RJ", 2026, 8)}
    report = sih_national.extract_national(
        date(2026, 7, 1), date(2026, 8, 1), root, states=["SP", "RJ"], progress=lambda _: None
    )
    assert report["unpublished"] == []
    assert report["failed"] == [
        {"partition": "RJ 2026-08", "error": "URLError"},
        {"partition": "SP 2026-07", "error": "MissingBeforeLastPublished"},
    ]
    # A moved FTP directory answers 550 for everything: that is a failure, not "unreleased".
    fake_source["missing"] = {(s, 2026, m) for s in ("SP", "RJ") for m in (9, 10)}
    report = sih_national.extract_national(
        date(2026, 9, 1), date(2026, 10, 1), root, states=["SP", "RJ"], progress=lambda _: None
    )
    assert {"partition": "all", "error": "NothingExtracted"} in report["failed"]


def test_coverage_keeps_the_whole_dataset_and_a_history_of_runs(tmp_path, fake_source):
    root = tmp_path / "data"
    first = sih_national.extract_national(
        date(2019, 1, 1), date(2019, 2, 1), root, states=["AC"], progress=lambda _: None
    )
    sih_national.extract_national(
        date(2019, 1, 1), date(2019, 1, 1), root, states=["RR"], progress=lambda _: None
    )
    base = sih_national.national_root(root)
    coverage = json.loads((base / "coverage.json").read_text())
    assert coverage["partitions"] == 3
    assert coverage["partitions_per_state"]["AC"] == 2
    assert len(list((base / "runs").glob("*.json"))) == 2
    assert first["software"]["dbfread"]


def test_keep_source_and_leftover_dbc_after_a_crash(tmp_path, fake_source):
    root = tmp_path / "data"
    sih_national.extract_national_partition("AC", 2019, 1, root, keep_source=True)
    source = sih_national.raw_path(root, "AC", 2019, 1)
    assert source.exists()
    # The next run without --keep-source finds the cached Parquet and removes the DBC.
    sih_national.extract_national_partition("AC", 2019, 1, root)
    assert not source.exists()
    assert not source.with_suffix(".dbc.source.json").exists()


def test_parquet_without_manifest_or_old_reader_is_extracted_again(tmp_path, fake_source):
    root = tmp_path / "data"
    sih_national.extract_national_partition("AC", 2019, 1, root)
    output = sih_national.partition_path(root, "AC", 2019, 1)
    manifest = output.with_suffix(".source.json")
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "reader_version": 0}))
    assert sih_national.extract_national_partition("AC", 2019, 1, root)["reader_version"] == 1
    manifest.unlink()
    fake_source["value"] = [row(9, "J450")]
    assert sih_national.extract_national_partition("AC", 2019, 1, root)["rows"] == 1
    assert not list(output.parent.glob("*.partial"))


def test_decompression_bomb_is_rejected(tmp_path, fake_source, monkeypatch):
    monkeypatch.setattr(sih_national, "ensure_dbc_reserve", lambda source, directory: 10)
    with pytest.raises(ValueError, match="larger than its header"):
        sih_national.extract_national_partition("AC", 2019, 1, tmp_path / "data")


def test_second_concurrent_run_is_refused(tmp_path, fake_source):
    import fcntl

    root = tmp_path / "data"
    base = sih_national.national_root(root)
    base.mkdir(parents=True)
    with (base / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="Another"):
            sih_national.extract_national(date(2019, 1, 1), date(2019, 1, 1), root, states=["AC"])


def test_projection_filters_chapter_for_every_residence(tmp_path):
    from folego.ingest.dbf import projected_dbf_rows

    path = tmp_path / "x.dbf"
    make_sih_dbf(path, [row(1, " J18"), row(2, ""), {**row(3, "i10"), "MUNIC_RES": "110020"}])
    rows = list(projected_dbf_rows(path, ["N_AIH"], None, diagnosis_chapters=["J", "I"]))
    assert [r["N_AIH"] for r in rows] == ["0000000000001", "0000000000003"]

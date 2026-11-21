import json
import shutil
from datetime import date

import polars as pl
import pytest

from folego.ingest import srag
from folego.ingest.common import sha256_file

CSV = "DT_SIN_PRI;ID_MN_RESI;CLASSI_FIN;HOSPITAL;OUTRO\n" + "\n".join(
    [
        "02/01/2018;110020;1;1;x",
        "02/01/2018;110020;4;1;x",
        "02/01/2018;110020;1;2;x",  # not hospitalised
        "03/01/2018;530040;2;1;x",  # DF administrative region
        "31/12/2017;110020;1;1;x",  # onset in the previous year
        "2018-01-04;1100205;9;1;x",  # ISO date, 7-digit code, unknown class
    ]
)


@pytest.fixture
def lookup(tmp_path):
    path = tmp_path / "lookup.parquet"
    pl.DataFrame(
        {"municipality_code": ["110020", "530010"], "health_region_code": ["11004", "53001"]}
    ).write_parquet(path)
    return path


@pytest.fixture
def served(tmp_path, monkeypatch):
    files = tmp_path / "served"
    files.mkdir()
    (files / "INFLUD18.csv").write_text(CSV, encoding="latin-1")
    pl.DataFrame(
        {
            "DT_SIN_PRI": ["2019-01-05 00:00:00"],
            "CO_MUN_RES": ["110020"],
            "CLASSI_FIN": [5.0],
            "HOSPITAL": [1.0],  # stored as a float in some releases
        }
    ).write_parquet(files / "INFLUD19-23-03-2026.parquet")

    def download(url, destination, **_):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(files / url.rsplit("/", 1)[1], destination)
        destination.with_suffix(destination.suffix + ".source.json").write_text(
            json.dumps({"url": url, "retrieved_at": "t", "sha256": sha256_file(destination)})
        )
        return destination

    monkeypatch.setattr(srag, "cached_download", download)
    return {
        2018: "https://d26692udehoye.cloudfront.net/SRAG/2013-2018/INFLUD18.csv",
        2019: "https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/SRAG/2019/INFLUD19-23-03-2026.parquet",
    }


def test_daily_counts_by_region_and_category_with_source_deleted(tmp_path, lookup, served):
    root = tmp_path / "data"
    output = tmp_path / "srag.parquet"
    series = srag.build_srag(2018, 2019, lookup, output, root, files=served)
    assert series.height == 730 * 2
    day = series.filter(pl.col("date") == date(2018, 1, 2), pl.col("health_region_code") == "11004")
    assert day.select("influenza", "unspecified", "viral").row(0) == (1, 1, 1)
    brasilia = series.filter(pl.col("health_region_code") == "53001")
    assert brasilia["other_virus"].sum() == 1
    assert series.filter(pl.col("date") == date(2019, 1, 5))["covid"].sum() == 1
    assert not list((root / "raw").rglob("INFLUD*"))
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    years = {m["year"]: m for m in manifest["years"]}
    assert years[2018]["hospitalised"] == 5 and years[2018]["invalid_onset"] == 0
    assert series.filter(pl.col("date") == date(2018, 1, 4))["unclassified"].sum() == 1
    assert "OUTRO" not in pl.read_parquet(root / "staging" / "srag" / "2018.parquet").columns


def test_unmapped_residences_fail(tmp_path, lookup, served):
    bad = pl.DataFrame({"municipality_code": ["999999"], "health_region_code": ["99999"]})
    with pytest.raises(ValueError, match="unmapped"):
        srag.extract_year(2018, served[2018], bad, tmp_path / "data")


def test_discovery_keeps_only_official_hosts_and_newest_release(monkeypatch):
    page = (
        '<a href="https://d26692udehoye.cloudfront.net/SRAG/2013-2018/INFLUD18.csv">'
        '<a href="https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/SRAG/2019/INFLUD19-01-02-2025.parquet">'
        '<a href="https://s3.sa-east-1.amazonaws.com/ckan.saude.gov.br/SRAG/2019/INFLUD19-23-03-2026.parquet">'
        '<a href="https://evil.example/SRAG/2020/INFLUD20-01-01-2030.parquet">'
    )

    class Response:
        text = page

        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, url):
            return Response()

    monkeypatch.setattr(srag.httpx, "Client", Client)
    files = srag.discover_files(pages=["p"])
    assert set(files) == {2018, 2019}
    assert files[2019].endswith("INFLUD19-23-03-2026.parquet")


def test_bad_onset_dates_fail_and_raw_file_is_still_deleted(tmp_path, lookup, served):
    root = tmp_path / "data"
    (tmp_path / "served" / "INFLUD18.csv").write_text(
        "DT_SIN_PRI;ID_MN_RESI;CLASSI_FIN;HOSPITAL\n" + "Jan 2018;110020;1;1\n" * 3,
        encoding="latin-1",
    )
    table = pl.read_parquet(lookup)
    with pytest.raises(ValueError, match="invalid onset"):
        srag.extract_year(2018, served[2018], table, root)
    assert not list((root / "raw").rglob("INFLUD*"))


def test_neighbouring_file_contributes_edge_onsets(tmp_path, lookup, served):
    series = srag.build_srag(
        2019, 2019, lookup, tmp_path / "s.parquet", tmp_path / "data", files=served
    )
    # The 2018 file holds no 2019 onset; reading it must not add rows outside 2019.
    assert series["date"].min() == date(2019, 1, 1)
    assert series["covid"].sum() == 1

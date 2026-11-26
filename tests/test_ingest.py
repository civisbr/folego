import hashlib
import json
from pathlib import Path

import httpx
import pytest

from folego.ingest.cams import pm25_to_micrograms
from folego.ingest.common import cached_download


def test_download_is_cached_atomic_and_has_provenance(tmp_path, monkeypatch):
    real_client = httpx.Client
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(200, content=b"official snapshot")

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler))
    )
    path = tmp_path / "raw.csv"
    cached_download("https://example.org/data.csv", path)
    cached_download("https://example.org/data.csv", path)
    assert len(hits) == 1 and path.read_bytes() == b"official snapshot"
    assert not list(tmp_path.glob("*.tmp"))
    manifest = json.loads(path.with_suffix(".csv.source.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_sensitive_url_is_not_written_to_provenance(tmp_path, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"csv"))
        ),
    )
    path = tmp_path / "fires.csv"
    cached_download("https://example.org/private-key/data", path, sensitive=True)
    assert "private-key" not in path.with_suffix(".csv.source.json").read_text()


def test_explicit_cams_units():
    assert pm25_to_micrograms(2e-8, "kg m**-3") == 20
    with pytest.raises(ValueError):
        pm25_to_micrograms(20, "unknown")


def test_sih_converter_validates_fields_and_caches_parquet(tmp_path, monkeypatch):
    import polars as pl

    dbfread = pytest.importorskip("dbfread")
    pyreaddbc = pytest.importorskip("pyreaddbc")

    from folego.ingest.sih import convert_sih

    calls = []

    def decompress(source, output):
        calls.append(source)
        Path(output).write_bytes(b"dbf")

    row = {
        "MUNIC_RES": "355030",
        "DT_INTER": "20240101",
        "DIAG_PRINC": "J450",
        "IDADE": 70,
        "COD_IDADE": 4,
        "VAL_TOT": 1500.0,
        "N_AIH": "1",
        "IDENT": "1",
        "EXTRA": "private",
    }
    monkeypatch.setattr(pyreaddbc, "dbc2dbf", decompress)
    monkeypatch.setattr(dbfread, "DBF", lambda *args, **kwargs: iter([row]))
    output = tmp_path / "staging" / "sih.parquet"
    convert_sih(tmp_path / "raw.dbc", output)
    convert_sih(tmp_path / "raw.dbc", output)
    frame = pl.read_parquet(output)
    assert calls == [str(tmp_path / "raw.dbc")]
    assert frame.height == 1 and "EXTRA" not in frame.columns
    assert frame["DIAG_PRINC"][0] == "J450"


def test_cached_download_rejects_tampered_or_unexpected_files(tmp_path):
    import hashlib
    import json

    from folego.ingest.common import cached_download

    target = tmp_path / "file.bin"
    target.write_bytes(b"original")
    digest = hashlib.sha256(b"original").hexdigest()
    (tmp_path / "file.bin.source.json").write_text(json.dumps({"sha256": digest}))
    url = "https://example.invalid/file.bin"
    assert cached_download(url, target, expected_sha256=digest) == target
    with pytest.raises(ValueError, match="Unexpected SHA-256"):
        cached_download(url, target, expected_sha256="0" * 64)
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed since download"):
        cached_download(url, target)


def test_truncated_manifest_counts_as_missing(tmp_path):
    from folego.ingest.common import _recorded_sha256

    manifest = tmp_path / "file.bin.source.json"
    manifest.write_text('{"sha256": "ab')
    assert _recorded_sha256(manifest) is None
    assert _recorded_sha256(tmp_path / "absent.json") is None


def test_cams_zip_request_is_saved_as_zip(tmp_path, monkeypatch):
    import sys
    import types

    from folego.ingest.cams import retrieve_cams

    class Client:
        def __init__(self, url):
            pass

        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"PK")

    monkeypatch.setitem(sys.modules, "cdsapi", types.SimpleNamespace(Client=Client))
    request = {"variable": ["particulate_matter_2.5um"], "data_format": "netcdf_zip"}
    target = retrieve_cams("cams-global-atmospheric-composition-forecasts", request, tmp_path)
    assert target.suffix == ".zip" and target.read_bytes() == b"PK"
    assert target.with_suffix(".source.json").exists()


def test_cached_cams_file_with_wrong_hash_is_refused(tmp_path, monkeypatch):
    import sys
    import types

    from folego.ingest.cams import retrieve_cams

    class Client:
        def __init__(self, url):
            pass

        def retrieve(self, dataset, request, target):
            Path(target).write_bytes(b"PK")

    monkeypatch.setitem(sys.modules, "cdsapi", types.SimpleNamespace(Client=Client))
    request = {"variable": ["particulate_matter_2.5um"], "data_format": "netcdf_zip"}
    dataset = "cams-global-atmospheric-composition-forecasts"
    target = retrieve_cams(dataset, request, tmp_path)
    assert retrieve_cams(dataset, request, tmp_path) == target
    target.write_bytes(b"swapped")
    with pytest.raises(ValueError, match="recorded sha256"):
        retrieve_cams(dataset, request, tmp_path)

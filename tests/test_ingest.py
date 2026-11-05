import hashlib
import json

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

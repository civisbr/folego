import json
from urllib.parse import parse_qs, urlparse

import pytest

from folego.ingest import ibge

CODES = [f"{110000 + i}" for i in range(7)]


def fake_api(monkeypatch, mode, codes=CODES):
    cities = [
        {
            "id": int(code + "0"),
            "nome": f"City {code}",
            "regiao-imediata": {"regiao-intermediaria": {"UF": {"sigla": "RO"}}},
        }
        for code in CODES
    ]
    regions = [
        {"codigo_municipio": code, "codigo_regiao_saude": "11001", "regiao_saude": "R"}
        for code in codes
    ]
    calls = []

    def download(url, destination, *, refresh=False):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if url == ibge.IBGE_URL:
            destination.write_text(json.dumps(cities))
            return destination
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        limit, offset = int(query["limit"][0]), int(query["offset"][0])
        if mode == "page":
            page = regions[offset * limit : (offset + 1) * limit]
        elif mode == "record":
            page = regions[offset : offset + limit]
        elif mode == "repeat":
            page = regions[:limit]
        elif mode == "endless":
            page = [
                {**regions[0], "codigo_municipio": f"{200000 + offset * limit + i}"}
                for i in range(limit)
            ]
        elif mode == "capped":
            # Server caps the page at 2 records but still advances by the requested limit.
            page = regions[offset * limit : offset * limit + 2]
        destination.write_text(json.dumps({"macrorregiao_regiao_saude_municipios": page}))
        return destination

    monkeypatch.setattr(ibge, "cached_download", download)
    monkeypatch.setattr(ibge, "HEALTH_PAGE_SIZE", 3)
    return calls


def test_offset_is_a_page_index(tmp_path, monkeypatch):
    fake_api(monkeypatch, "page")
    frame, report = ibge.build_correspondence(tmp_path)
    assert report["matched"] == report["ibge_municipalities"] == len(CODES)
    assert (tmp_path / "staging" / "municipalities.parquet").exists()


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("record", "overlapping"),
        ("repeat", "overlapping"),
        ("endless", "Unexpected pagination"),
        ("capped", "Short Ministry of Health page"),
    ],
)
def test_inconsistent_pagination_fails_closed(tmp_path, monkeypatch, mode, message):
    fake_api(monkeypatch, mode)
    with pytest.raises(ValueError, match=message):
        ibge.build_correspondence(tmp_path)
    assert not (tmp_path / "staging" / "municipalities.parquet").exists()
    assert not (tmp_path / "raw" / "territory" / "health-pages").exists()


def test_invalid_municipality_code_is_rejected(tmp_path, monkeypatch):
    fake_api(monkeypatch, "page", codes=[*CODES[:-1], None])
    with pytest.raises(ValueError, match="Invalid Ministry of Health municipality code"):
        ibge.build_correspondence(tmp_path)


def test_cached_page_set_is_reused_whole(tmp_path, monkeypatch):
    calls = fake_api(monkeypatch, "page")
    ibge.build_correspondence(tmp_path)
    first = len(calls)
    ibge.build_correspondence(tmp_path)
    assert len(calls) == first
    ibge.build_correspondence(tmp_path, refresh=True)
    assert len(calls) == 2 * first


def test_incomplete_coverage_keeps_previous_lookup(tmp_path, monkeypatch):
    fake_api(monkeypatch, "page")
    ibge.build_correspondence(tmp_path)
    previous = (tmp_path / "staging" / "municipalities.parquet").read_bytes()
    fake_api(monkeypatch, "page", codes=CODES[:3])
    with pytest.raises(ValueError, match="mappings missing"):
        ibge.build_correspondence(tmp_path, refresh=True)
    assert (tmp_path / "staging" / "municipalities.parquet").read_bytes() == previous

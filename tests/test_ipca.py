import json
from datetime import date

import pytest

from folego.ingest.ipca import parse_sidra_ipca

HEADER = {"V": "Valor", "D2C": "Variável (Código)", "D3C": "Mês (Código)", "D1N": "Brasil"}


def rows(*items, variable="2266"):
    return [HEADER] + [{"V": value, "D2C": variable, "D3C": month} for month, value in items]


def test_parses_monthly_index(tmp_path):
    path = tmp_path / "ipca.json"
    path.write_text(json.dumps(rows(("201901", "5000.5"), ("201902", "5010.0"))))
    frame = parse_sidra_ipca(path)
    assert frame["month"].to_list() == [date(2019, 1, 1), date(2019, 2, 1)]
    assert frame["ipca_index"].to_list() == [5000.5, 5010.0]


@pytest.mark.parametrize(
    "payload",
    [
        rows(("201901", "5000"), ("201903", "5010")),  # gap
        rows(("201901", "5000"), ("201901", "5010")),  # repeated month
        rows(("201901", "-1")),  # non-positive
        rows(("201901", "...")),  # SIDRA placeholder for missing values
        rows(("201901", "5000"), variable="63"),  # monthly variation, not the index
        rows(("201913", "5000")),  # invalid month code
        [{"V": "Valor", "D2C": "Mês (Código)", "D3C": "Variável (Código)"}],  # reordered
        {"V": "Valor"},  # not a list
    ],
)
def test_rejects_inconsistent_series(tmp_path, payload):
    path = tmp_path / "ipca.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        parse_sidra_ipca(path)


def test_refresh_keeps_previous_snapshots(tmp_path, monkeypatch):
    from folego.ingest import ipca

    def fake_download(url, destination, **_):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(rows(("201901", "5000"))))
        return destination

    monkeypatch.setattr(ipca, "cached_download", fake_download)
    old = tmp_path / "raw" / "ipca" / "sidra-1737-20200101.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(rows(("201901", "4000"))))
    # Manifests sort after the snapshot they describe and must never be parsed as data.
    (old.parent / "sidra-1737-20200101.json.source.json").write_text('{"sha256": "x"}')
    frame, path = ipca.download_ipca(tmp_path)
    assert path == old and frame["ipca_index"].to_list() == [4000.0]
    frame, path = ipca.download_ipca(tmp_path, refresh=True)
    assert path != old and old.exists() and frame["ipca_index"].to_list() == [5000.0]

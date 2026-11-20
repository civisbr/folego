import stat

import pytest

from folego.ingest.ads_setup import save_ads_credentials


def test_token_is_saved_privately_and_not_printed(tmp_path, capsys):
    p = tmp_path / ".cdsapirc"
    save_ads_credentials("test-token-never-real", p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    lines = p.read_text().splitlines()
    assert lines[0] == "url: https://ads.atmosphere.copernicus.eu/api"
    assert lines[1] == "key: test-token-never-real"
    assert capsys.readouterr().out == ""


def test_saved_key_survives_line_based_parsing(tmp_path):
    # Mirrors cdsapi/ecmwf.datastores parsing so the check runs without the climate extra.
    p = tmp_path / ".cdsapirc"
    save_ads_credentials("00000000-0000-0000-0000-000000000000", p)
    parsed = dict(line.split(":", 1) for line in p.read_text().splitlines())
    assert parsed["key"].strip() == "00000000-0000-0000-0000-000000000000"


def test_partial_file_is_removed_when_write_fails(tmp_path, monkeypatch):
    p = tmp_path / ".cdsapirc"

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr("os.fchmod", fail)
    with pytest.raises(OSError):
        save_ads_credentials("test-token", p)
    assert not p.exists()


def test_cdsapi_reads_token_without_quotes(tmp_path):
    api = pytest.importorskip("cdsapi.api")
    p = tmp_path / ".cdsapirc"
    save_ads_credentials("00000000-0000-0000-0000-000000000000", p)
    assert api.read_config(str(p))["key"] == "00000000-0000-0000-0000-000000000000"


def test_existing_configuration_is_preserved(tmp_path):
    p = tmp_path / ".cdsapirc"
    p.write_text("existing configuration")
    with pytest.raises(FileExistsError):
        save_ads_credentials("test-new-token", p)
    assert p.read_text() == "existing configuration"


@pytest.mark.parametrize(
    "token", ["", "key: two words", "one\ntwo", '"quoted"', "esc\x1bape", "nul\x00"]
)
def test_invalid_token_does_not_create_file(tmp_path, token):
    p = tmp_path / ".cdsapirc"
    with pytest.raises(ValueError):
        save_ads_credentials(token, p)
    assert not p.exists()

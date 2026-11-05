import json
import stat

import pytest

from folego.ingest.ads_setup import save_ads_credentials


def test_token_is_saved_privately_and_not_printed(tmp_path, capsys):
    p = tmp_path / ".cdsapirc"
    save_ads_credentials("test-token-never-real", p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    lines = p.read_text().splitlines()
    assert lines[0] == "url: https://ads.atmosphere.copernicus.eu/api"
    assert json.loads(lines[1].removeprefix("key: ")) == "test-token-never-real"
    assert capsys.readouterr().out == ""


def test_existing_configuration_is_preserved(tmp_path):
    p = tmp_path / ".cdsapirc"
    p.write_text("existing configuration")
    with pytest.raises(FileExistsError):
        save_ads_credentials("test-new-token", p)
    assert p.read_text() == "existing configuration"


@pytest.mark.parametrize("token", ["", "key: two words", "one\ntwo"])
def test_invalid_token_does_not_create_file(tmp_path, token):
    p = tmp_path / ".cdsapirc"
    with pytest.raises(ValueError):
        save_ads_credentials(token, p)
    assert not p.exists()

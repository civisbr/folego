"""Local-only credential setup; never prints, uploads or versions the API token."""

import os
import re
from pathlib import Path

ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
# Covers current UUID tokens and legacy "uid:key"; anything else (quotes, control
# characters, line breaks) would be stored verbatim by the readers below.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_:-]+")


def save_ads_credentials(token: str, destination: Path | None = None) -> Path:
    token = token.strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Cole apenas a chave da API, em uma única linha e sem aspas.")
    destination = destination if destination is not None else Path.home() / ".cdsapirc"
    # Exclusive creation preserves an existing ADS/CDS configuration.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            # cdsapi.api.read_config and ecmwf.datastores read the value verbatim after
            # the first ":" (no YAML), so quotes would become part of the key.
            output.write(f"url: {ADS_URL}\nkey: {token}\n")
    except BaseException:
        # A partial file would block every later run because creation is exclusive.
        destination.unlink(missing_ok=True)
        raise
    return destination


def configure_ads_interactively():
    from getpass import getpass

    destination = Path.home() / ".cdsapirc"
    if destination.exists():
        raise FileExistsError(
            "Já existe ~/.cdsapirc; a configuração foi preservada. Se o ADS responder 401 e "
            "a linha key estiver entre aspas, remova as aspas ou apague o arquivo e repita."
        )
    token = getpass("Cole a API key do perfil ADS (entrada oculta): ")
    path = save_ads_credentials(token, destination)
    print(f"Configuração salva em {path}, com permissão 600.")
    print("A autenticação e a licença ainda serão verificadas no download do CAMS.")

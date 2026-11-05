"""Local-only credential setup; never prints, uploads or versions the API token."""

import json
import os
from pathlib import Path

ADS_URL = "https://ads.atmosphere.copernicus.eu/api"


def save_ads_credentials(token: str, destination: Path | None = None) -> Path:
    token = token.strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("Cole apenas a chave da API, em uma única linha.")
    destination = destination if destination is not None else Path.home() / ".cdsapirc"
    # Exclusive creation preserves an existing ADS/CDS configuration.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(f"url: {ADS_URL}\nkey: {json.dumps(token)}\n")
    return destination


def configure_ads_interactively():
    from getpass import getpass

    destination = Path.home() / ".cdsapirc"
    if destination.exists():
        raise FileExistsError("Já existe ~/.cdsapirc; a configuração foi preservada.")
    token = getpass("Cole a API key do perfil ADS (entrada oculta): ")
    path = save_ads_credentials(token, destination)
    print(f"Configuração salva em {path}, com permissão 600.")
    print("A autenticação e a licença ainda serão verificadas no download do CAMS.")

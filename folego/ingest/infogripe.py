"""Import an explicitly chosen InfoGripe snapshot; do not infer regional resolution."""

import hashlib
from pathlib import Path

from .common import cached_download


def download_infogripe(url: str, root=Path("data")):
    from urllib.parse import urlsplit

    if urlsplit(url).hostname not in ("raw.githubusercontent.com", "info.gripe.fiocruz.br"):
        raise ValueError("Use the official InfoGripe repository or Fiocruz host")
    return cached_download(
        url, root / "raw" / "infogripe" / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".csv")
    )

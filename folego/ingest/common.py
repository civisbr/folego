"""Atomic cached downloads and source provenance. Secrets never enter manifests."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx


def cached_download(url: str, destination: Path, *, refresh=False, sensitive=False) -> Path:
    destination = Path(destination)
    if destination.exists() and not refresh:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        if url.startswith("ftp://"):
            with urlopen(url, timeout=120) as source, temporary.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
        else:
            with httpx.Client(
                timeout=httpx.Timeout(120, connect=15),
                transport=httpx.HTTPTransport(retries=2),
                follow_redirects=True,
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, destination)
        source = (
            f"{urlsplit(url).scheme}://{urlsplit(url).hostname}/[redacted]" if sensitive else url
        )
        destination.with_suffix(destination.suffix + ".source.json").write_text(
            json.dumps(
                {
                    "url": source,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "sha256": digest,
                },
                indent=2,
            )
        )
    finally:
        temporary.unlink(missing_ok=True)
    return destination

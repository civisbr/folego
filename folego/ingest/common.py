"""Atomic cached downloads and source provenance. Secrets never enter manifests."""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx


def cached_download(
    url: str,
    destination: Path,
    *,
    refresh=False,
    sensitive=False,
    expected_sha256: str | None = None,
) -> Path:
    destination = Path(destination)
    manifest = destination.with_suffix(destination.suffix + ".source.json")
    # A file without its manifest may come from an interrupted run; fetch it again.
    recorded = _recorded_sha256(manifest) if destination.exists() and not refresh else None
    if recorded is not None:
        if sha256_file(destination) != recorded:
            raise ValueError(f"Cached file changed since download: {destination}")
        _check_expected(recorded, expected_sha256, destination)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    digest = hashlib.sha256()
    try:
        if url.startswith("ftp://"):
            with urlopen(url, timeout=120) as source, temporary.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
        else:
            with httpx.Client(
                timeout=httpx.Timeout(120, connect=15),
                transport=httpx.HTTPTransport(retries=2),
                follow_redirects=True,
                event_hooks={"response": [_https_only] if url.startswith("https://") else []},
            ) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        for chunk in response.iter_bytes():
                            digest.update(chunk)
                            output.write(chunk)
        _check_expected(digest.hexdigest(), expected_sha256, destination)
        os.replace(temporary, destination)
        source = (
            f"{urlsplit(url).scheme}://{urlsplit(url).hostname}/[redacted]" if sensitive else url
        )
        partial = manifest.with_suffix(".tmp")
        partial.write_text(
            json.dumps(
                {
                    "url": source,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "sha256": digest.hexdigest(),
                },
                indent=2,
            )
        )
        os.replace(partial, manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _recorded_sha256(manifest: Path) -> str | None:
    # A truncated or legacy manifest means the file must be fetched again.
    try:
        return json.loads(manifest.read_text())["sha256"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _https_only(response: httpx.Response) -> None:
    # Checked on every hop, so an intermediate plain-HTTP redirect is also refused.
    if response.url.scheme != "https":
        raise ValueError("Download redirected away from HTTPS")


def atomic_write_text(path: Path, text: str) -> None:
    """Readers of the fixed name never see a half-written file after a crash or OOM kill."""
    # A unique O_EXCL name: concurrent runs do not share it and a planted symlink is not followed.
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".partial")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _check_expected(actual: str, expected: str | None, destination: Path) -> None:
    if expected is not None and actual != expected:
        raise ValueError(f"Unexpected SHA-256 for {destination}")

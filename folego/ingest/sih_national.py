"""Nationwide SIH extraction: keep only study diagnoses, then drop the bulky DBC.

Each state×month file is downloaded, projected to the respiratory (J), circulatory (I)
and negative-control (S/T) admissions for every residence, written to Parquet with a
manifest holding the source URL and SHA-256, and the DBC is deleted. The manifest is
the only evidence left of the raw file, so it is written before the deletion.

The DATASUS FTP is plain FTP and republishes revised files under the same name: the
recorded hash proves what was read, not that it came from DATASUS, and a deleted DBC
cannot be recovered byte for byte. Monthly counts are checked against TabNet instead.
"""

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError

import polars as pl

from .common import sha256_file
from .dbf import projected_dbf_rows
from .sih import FIELDS, SCHEMA, STATES, download_sih, ensure_dbc_reserve

READER_VERSION = 1
DIAGNOSIS_CHAPTERS = ("I", "J", "S", "T")
# Threads check free space one at a time, so each decompression sees the space the
# previous one already took; without this the reserve is shared by every worker.
_DECOMPRESS = threading.Lock()


class NotPublished(Exception):
    """The DATASUS FTP answered 550 for this file."""


def national_root(root: Path) -> Path:
    return root / "staging" / "sih" / "national"


def partition_path(root: Path, state: str, year: int, month: int) -> Path:
    return national_root(root) / str(year) / state / f"{year}{month:02d}.parquet"


def raw_path(root: Path, state: str, year: int, month: int) -> Path:
    return root / "raw" / "sih" / str(year) / state / f"RD{state}{year % 100:02d}{month:02d}.dbc"


def extract_national_partition(
    state: str,
    year: int,
    month: int,
    root: Path = Path("data"),
    *,
    keep_source: bool = False,
    provenance: dict | None = None,
) -> dict:
    output = partition_path(root, state, year, month)
    manifest_path = output.with_suffix(".source.json")
    cached = _valid_cache(output, manifest_path)
    if cached is not None:
        if not keep_source:
            # A crash between the manifest and the deletion leaves the DBC behind.
            _delete_source(raw_path(root, state, year, month))
        return cached
    try:
        source = download_sih(state, year, month, root)
    except URLError as error:
        if "550" in str(error.reason):
            raise NotPublished(f"{state} {year}-{month:02d}") from error
        raise
    # cached_download already compared the file with this hash on a cache hit.
    downloaded = json.loads(source.with_suffix(source.suffix + ".source.json").read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = national_root(root) / ".tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    stats = {}
    from pyreaddbc import dbc2dbf

    with TemporaryDirectory(prefix="folego-sih-", dir=scratch) as directory:
        dbf = Path(directory) / "source.dbf"
        with _DECOMPRESS:
            expected_size = ensure_dbc_reserve(source, scratch)
            dbc2dbf(str(source), str(dbf))
        # The header sizes come from the same untrusted file; a larger output is a bomb.
        if dbf.stat().st_size > expected_size + 1:
            raise ValueError(f"Decompressed DBF larger than its header declares: {source.name}")
        columns = {name: [] for name in FIELDS}
        for record in projected_dbf_rows(
            dbf, FIELDS, None, diagnosis_chapters=DIAGNOSIS_CHAPTERS, stats=stats
        ):
            for name in FIELDS:
                columns[name].append(record[name])
    frame = pl.DataFrame(columns, schema=SCHEMA)
    del columns
    if frame.height and (
        (frame["ANO_CMPT"].cast(pl.Int64) != year).any()
        or (frame["MES_CMPT"].cast(pl.Int64) != month).any()
    ):
        raise ValueError(f"SIH competence does not match source filename: {source.name}")
    # Not ending in .parquet keeps an interrupted write out of any *.parquet glob.
    staged = output.with_name(output.name + ".partial")
    staged_manifest = manifest_path.with_name(manifest_path.name + ".partial")
    try:
        frame.write_parquet(staged, compression="zstd")
        metadata = {
            "reader_version": READER_VERSION,
            "state": state,
            "year": year,
            "month": month,
            "diagnosis_chapters": list(DIAGNOSIS_CHAPTERS),
            "residences": "all",
            **stats,
            "rows": frame.height,
            "parquet_sha256": sha256_file(staged),
            "source_url": downloaded["url"],
            "source_retrieved_at": downloaded["retrieved_at"],
            "source_sha256": downloaded["sha256"],
            "source_bytes": source.stat().st_size,
            "source_deleted": not keep_source,
            "extracted_at": datetime.now(UTC).isoformat(),
            "software": provenance or software_provenance(),
        }
        staged_manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        os.replace(staged, output)
        os.replace(staged_manifest, manifest_path)
    finally:
        staged.unlink(missing_ok=True)
        staged_manifest.unlink(missing_ok=True)
    if not keep_source:
        _delete_source(source)
    return metadata


def _delete_source(source: Path) -> None:
    source.unlink(missing_ok=True)
    source.with_suffix(source.suffix + ".source.json").unlink(missing_ok=True)


def _valid_cache(output: Path, manifest_path: Path) -> dict | None:
    if not (output.exists() and manifest_path.exists()):
        return None
    try:
        metadata = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("reader_version") != READER_VERSION or metadata.get(
        "diagnosis_chapters"
    ) != list(DIAGNOSIS_CHAPTERS):
        return None
    if metadata.get("parquet_sha256") != sha256_file(output):
        raise ValueError(f"Extracted SIH partition changed since extraction: {output}")
    return metadata


def software_provenance() -> dict:
    """Code revision and reader versions, so each partition can be tied to its producer."""
    result = {}
    for package in ("pyreaddbc", "dbfread", "polars"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    try:
        here = Path(__file__).parent
        result["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=here, capture_output=True, text=True, check=True
        ).stdout.strip()
        result["git_dirty"] = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=here,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        result["git_commit"] = None
    return result


def competences(start: date, end: date) -> list[tuple[int, int]]:
    if (start.year, start.month) > (end.year, end.month):
        raise ValueError("Start competence after end")
    result = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        result.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def extract_national(
    start: date,
    end: date,
    root: Path = Path("data"),
    *,
    states: Iterable[str] = STATES,
    workers: int = 3,
    keep_source: bool = False,
    progress: Callable[[str], None] = print,
) -> dict:
    """Extract every state×competence in range.

    A missing file counts as unpublished only after the last competence extracted for
    its state; a gap before it, or a run that extracts nothing, is a failure. That keeps
    a moved FTP directory (also answered with 550) from passing as "not released yet".
    """
    states = list(states)
    if not set(states) <= set(STATES) or len(set(states)) != len(states):
        raise ValueError("Invalid SIH states")
    base = national_root(root)
    base.mkdir(parents=True, exist_ok=True)
    with (base / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another sih-national run is using this data folder") from None
        # Only one run holds the lock, so leftovers are from a killed run.
        shutil.rmtree(base / ".tmp", ignore_errors=True)
        report = _run(start, end, root, states, workers, keep_source, progress)
    return report


def _run(start, end, root, states, workers, keep_source, progress) -> dict:
    tasks = [(s, y, m) for y, m in competences(start, end) for s in states]
    provenance = software_provenance()
    outcomes = {}

    def run(task):
        state, year, month = task
        try:
            metadata = extract_national_partition(
                state, year, month, root, keep_source=keep_source, provenance=provenance
            )
            return "done", metadata
        except NotPublished:
            return "unpublished", None
        except Exception as error:  # noqa: BLE001 - one bad file must not stop the others
            progress(f"falha {_label(task)}: {type(error).__name__}: {error}")
            return "failed", type(error).__name__

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {task: pool.submit(run, task) for task in tasks}
        for count, (task, future) in enumerate(futures.items(), start=1):
            outcomes[task] = future.result()
            if count % 27 == 0 or count == len(tasks):
                failed = sum(status == "failed" for status, _ in outcomes.values())
                progress(f"{count}/{len(tasks)} partições ({failed} falhas)")
    finally:
        # Ctrl-C must not leave the queued downloads running in the background.
        pool.shutdown(wait=True, cancel_futures=True)
    last_done = {}
    for (state, year, month), (status, _) in outcomes.items():
        if status == "done":
            last_done[state] = max(last_done.get(state, (0, 0)), (year, month))
    unpublished, failed = [], []
    for task, (status, payload) in sorted(outcomes.items()):
        state, year, month = task
        if status == "unpublished" and (year, month) > last_done.get(state, (9999, 12)):
            unpublished.append(_label(task))
        elif status == "unpublished":
            failed.append({"partition": _label(task), "error": "MissingBeforeLastPublished"})
        elif status == "failed":
            failed.append({"partition": _label(task), "error": payload})
    if not last_done:
        failed.append({"partition": "all", "error": "NothingExtracted"})
    run_report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "start": f"{start:%Y-%m}",
        "end": f"{end:%Y-%m}",
        "states": states,
        "partitions_expected": len(tasks),
        "partitions_extracted": sum(status == "done" for status, _ in outcomes.values()),
        "unpublished": unpublished,
        "failed": failed,
        "software": provenance,
    }
    _write_coverage(root, run_report)
    return run_report


def _write_coverage(root: Path, run_report: dict) -> None:
    """History of every run plus a dataset view rebuilt from all manifests on disk."""
    base = national_root(root)
    manifests = [json.loads(p.read_text()) for p in sorted(base.glob("*/*/*.source.json"))]
    dataset = {
        "generated_at": run_report["generated_at"],
        "partitions": len(manifests),
        "competences": sorted({f"{m['year']}-{m['month']:02d}" for m in manifests}),
        "partitions_per_state": {
            state: sum(m["state"] == state for m in manifests) for state in STATES
        },
        "scanned_records": sum(m["scanned_records"] for m in manifests),
        "selected_records": sum(m["rows"] for m in manifests),
        "source_bytes": sum(m["source_bytes"] for m in manifests),
        "partition_manifest_sha256": hashlib.sha256(
            json.dumps(
                sorted((m["state"], m["year"], m["month"], m["parquet_sha256"]) for m in manifests)
            ).encode()
        ).hexdigest(),
        "last_run": run_report,
    }
    history = base / "runs"
    history.mkdir(exist_ok=True)
    stamp = datetime.fromisoformat(run_report["generated_at"]).strftime("%Y%m%dT%H%M%S%fZ")
    _atomic_json(history / f"{stamp}.json", run_report)
    _atomic_json(base / "coverage.json", dataset)


def _atomic_json(path: Path, payload: dict) -> None:
    staged = path.with_name(path.name + ".partial")
    try:
        staged.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _label(task) -> str:
    state, year, month = task
    return f"{state} {year}-{month:02d}"

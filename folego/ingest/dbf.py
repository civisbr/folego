"""Project SIH fields before decoding and filter residence before retaining records.

DBF headers/types come from dbfread. Fixed-width projection avoids materializing
all clinical fields for millions of out-of-region records.
"""

from collections.abc import Collection, Iterator
from pathlib import Path


def projected_dbf_rows(
    path: Path, columns: list[str], residence_codes: Collection[str], *, stats: dict | None = None
) -> Iterator[dict]:
    from dbfread import DBF

    table = DBF(str(path), encoding="latin-1", load=False)
    positions = {}
    offset = 1
    for field in table.fields:
        positions[field.name] = (field, slice(offset, offset + field.length))
        offset += field.length
    required = set(columns) | {"MUNIC_RES"}
    if missing := required - set(positions):
        raise ValueError(f"Missing DBF fields: {sorted(missing)}")
    if offset != table.header.recordlen:
        raise ValueError("Unexpected DBF record layout")
    expected = table.header.headerlen + table.header.numrecords * table.header.recordlen
    if path.stat().st_size < expected:
        raise ValueError("Truncated DBF")
    codes = {c.encode("ascii") for c in residence_codes}
    residence_slice = positions["MUNIC_RES"][1]
    parser = table.parserclass(table, None)
    if stats is None:
        stats = {}
    stats.update(scanned_records=0, deleted_records=0, selected_records=0)
    with path.open("rb") as stream:
        stream.seek(table.header.headerlen)
        for _ in range(table.header.numrecords):
            record = stream.read(table.header.recordlen)
            if len(record) != table.header.recordlen:
                raise ValueError("Truncated DBF record")
            stats["scanned_records"] += 1
            if record[:1] == b"*":
                stats["deleted_records"] += 1
                continue
            if record[:1] != b" ":
                raise ValueError("Invalid DBF record marker")
            if record[residence_slice].strip() not in codes:
                continue
            stats["selected_records"] += 1
            yield {
                name: parser.parse(positions[name][0], record[positions[name][1]])
                for name in columns
            }

"""Local analytical warehouse; Parquet remains the canonical interchange."""

from pathlib import Path

import duckdb


def connect_warehouse(root=Path("data"), *, spatial=False):
    root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(root / "folego.duckdb"))
    if spatial:
        connection.execute("INSTALL spatial")
        connection.execute("LOAD spatial")
    return connection

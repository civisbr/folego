from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

from folego.warehouse import connect_warehouse


def test_arrow_parquet_remains_readable_from_persistent_duckdb(tmp_path):
    source = tmp_path / "counts.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"date": date(2024, 1, 1), "region": "11004", "admissions": 5},
                {"date": date(2024, 1, 2), "region": "11004", "admissions": 7},
            ]
        ),
        source,
    )
    connection = connect_warehouse(tmp_path / "warehouse")
    connection.execute("CREATE TABLE outcomes AS SELECT * FROM read_parquet(?)", [str(source)])
    connection.close()
    connection = connect_warehouse(tmp_path / "warehouse")
    assert connection.execute(
        "SELECT region, sum(admissions) FROM outcomes GROUP BY region"
    ).fetchall() == [("11004", 12)]
    connection.close()

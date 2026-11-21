import json
from datetime import date

import polars as pl
import pytest

pytest.importorskip("shapely")

from geo_helpers import write_regions  # noqa: E402

from folego.ingest import firms_archive  # noqa: E402

HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight,type\n"
)


def csv(rows):
    return (
        HEADER
        + "".join(
            f"{lat},{lon},300,0.4,0.4,{day},{time},N,VIIRS,{conf},2,290,{frp},D,{kind}\n"
            for lat, lon, day, time, conf, frp, kind in rows
        )
    ).encode()


def test_chunk_keeps_vegetation_fires_per_region_and_local_day(tmp_path):
    regions = write_regions(tmp_path)
    payload = csv(
        [
            (-0.5, 0.0, "2019-08-15", "1700", "n", 10.0, 0),  # A, local 15 Aug
            (-0.5, 0.0, "2019-08-16", "0200", "h", 5.0, 0),  # A, local 15 Aug 23:00
            (-0.5, 0.8, "2019-08-16", "0400", "n", 7.0, 0),  # B, local 16 Aug
            (-0.5, 0.8, "2019-08-16", "0400", "l", 99.0, 0),  # low confidence
            (-0.5, 0.8, "2019-08-16", "0400", "n", 99.0, 2),  # static industrial source
            (5.0, 5.0, "2019-08-16", "0400", "n", 99.0, 0),  # outside every region
        ]
    )
    counts, stats = firms_archive.reduce_chunk(payload, regions)
    got = {
        (r["date"], r["health_region_code"]): (r["fire_count"], r["frp"]) for r in counts.to_dicts()
    }
    assert got == {(date(2019, 8, 15), "A"): (2, 15.0), (date(2019, 8, 16), "B"): (1, 7.0)}
    assert stats == {
        "detections": 6,
        "non_vegetation": 1,
        "low_confidence": 1,
        "outside_brazil_regions": 1,
    }


def test_build_zero_fills_drops_partial_edge_days_and_resumes(tmp_path, monkeypatch):
    regions = write_regions(tmp_path)
    calls = []

    def fetch(client, key, first, **_):
        calls.append(first)
        return csv([(-0.5, 0.0, first.isoformat(), "1200", "n", 1.0, 0)])

    monkeypatch.setenv("FIRMS_MAP_KEY", "a" * 32)
    monkeypatch.setattr(firms_archive, "fetch_chunk", fetch)
    monkeypatch.setattr(firms_archive, "archive_end", lambda client, key: date(2019, 12, 31))
    output = tmp_path / "fires.parquet"
    root = tmp_path / "data"
    series = firms_archive.build_fires(date(2019, 8, 1), date(2019, 8, 10), regions, output, root)
    # Local 10 Aug ends at 03 UTC on 11 Aug, so a third chunk is fetched.
    assert calls == [date(2019, 8, 1), date(2019, 8, 6), date(2019, 8, 11)]
    assert series["date"].min() == date(2019, 8, 1) and series["date"].max() == date(2019, 8, 10)
    assert series.height == 10 * 2
    # Fires at 12 UTC on 1 and 6 Aug count; the one on 11 Aug is past the end.
    assert series.filter(pl.col("health_region_code") == "A")["fire_count"].sum() == 2
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    assert manifest["totals"]["detections"] == 3
    assert "a" * 32 not in output.with_suffix(".parquet.source.json").read_text()
    firms_archive.build_fires(date(2019, 8, 1), date(2019, 8, 10), regions, output, root)
    assert len(calls) == 3
    monkeypatch.setattr(firms_archive, "archive_end", lambda client, key: date(2019, 8, 12))
    with pytest.raises(RuntimeError, match="processed up to"):
        firms_archive.build_fires(date(2019, 8, 1), date(2019, 8, 10), regions, output, root)


def test_key_file_must_be_private_and_errors_never_echo_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    key_file = tmp_path / "firms-key"
    key_file.write_text("b" * 32)
    key_file.chmod(0o644)
    monkeypatch.setattr(firms_archive, "KEY_FILE", key_file)
    with pytest.raises(RuntimeError, match="chmod 600"):
        firms_archive.map_key()
    key_file.chmod(0o600)
    assert firms_archive.map_key() == "b" * 32

    import httpx

    def handler(request):
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError) as error:
        firms_archive.fetch_chunk(client, "b" * 32, date(2019, 8, 1), attempts=1)
    assert "b" * 32 not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_chunk_with_no_fire_inside_brazil(tmp_path):
    regions = write_regions(tmp_path)
    counts, stats = firms_archive.reduce_chunk(
        csv([(5.0, 5.0, "2019-08-16", "0400", "n", 9.0, 0)]), regions
    )
    assert counts.is_empty() and stats["outside_brazil_regions"] == 1


def test_non_csv_answers_are_errors_after_retries(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, text="Exceeding allowed transaction limit")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="transaction limit"):
        firms_archive.fetch_chunk(client, "c" * 32, date(2019, 8, 1), attempts=2, pause=0)


def test_key_file_link_and_non_ascii_key_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    target = tmp_path / "real"
    target.write_text("d" * 32)
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setattr(firms_archive, "KEY_FILE", link)
    with pytest.raises(RuntimeError, match="not a link"):
        firms_archive.map_key()
    monkeypatch.setenv("FIRMS_MAP_KEY", "é" * 32)
    with pytest.raises(RuntimeError, match="ASCII"):
        firms_archive.map_key()


def test_national_outage_days_become_null_not_zero():
    from datetime import timedelta

    days = [date(2022, 7, 1) + timedelta(days=i) for i in range(40)]
    counts = pl.DataFrame(
        {
            "date": days,
            "health_region_code": "A",
            "fire_count": [0 if 26 <= d.day <= 28 and d.month == 7 else 500 for d in days],
            "frp": 1.0,
        }
    )
    assert firms_archive.outage_days(counts, days[0], days[-1]) == [
        date(2022, 7, 26),
        date(2022, 7, 27),
        date(2022, 7, 28),
    ]
    # A day missing from the counts altogether is an outage too.
    assert date(2022, 7, 10) in firms_archive.outage_days(
        counts.filter(pl.col("date") != date(2022, 7, 10)), days[0], days[-1]
    )


def test_build_nulls_outage_days_in_every_region_and_lists_them(tmp_path, monkeypatch):
    from datetime import timedelta

    regions = write_regions(tmp_path)
    outage = {date(2019, 8, 1) + timedelta(days=i) for i in range(9, 27)}

    def fetch(client, key, first, **_):
        days = [first + timedelta(days=i) for i in range(5)]
        # Region A burns every day; region B never does and keeps real zeros.
        return csv(
            [(-0.5, 0.0, d.isoformat(), "1500", "n", 1.0, 0) for d in days if d not in outage]
        )

    monkeypatch.setenv("FIRMS_MAP_KEY", "a" * 32)
    monkeypatch.setattr(firms_archive, "fetch_chunk", fetch)
    monkeypatch.setattr(firms_archive, "archive_end", lambda client, key: date(2019, 12, 31))
    output = tmp_path / "fires.parquet"
    # The 18-day outage is longer than half the median window and reaches the end.
    series = firms_archive.build_fires(
        date(2019, 8, 1), date(2019, 8, 27), regions, output, tmp_path / "data"
    )
    nulls = series.filter(pl.col("fire_count").is_null())
    assert set(nulls["date"]) == outage and set(nulls["health_region_code"]) == {"A", "B"}
    assert nulls["frp"].null_count() == nulls.height
    normal = series.filter(pl.col("date") == date(2019, 8, 2))
    assert dict(normal.select("health_region_code", "fire_count").iter_rows()) == {"A": 1, "B": 0}
    manifest = json.loads(output.with_suffix(".parquet.source.json").read_text())
    assert manifest["outage_days"] == sorted(d.isoformat() for d in outage)

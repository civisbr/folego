from datetime import date

import polars as pl

from folego.evaluate.backtest import rolling_origin_splits
from folego.evaluate.metrics import peak_metrics, weekly_metrics
from folego.publish.export_web import suppress_small_cells


def test_suppression_removes_payments_and_related_breakdowns():
    frame = pl.DataFrame(
        {
            "admissions": [0, 4, 5],
            "payment": [0.0, 400.0, 500.0],
            "mean_payment": [0.0, 100.0, 100.0],
        }
    )
    result = suppress_small_cells(frame)
    assert result["admissions"].to_list() == [None, None, 5]
    assert result["payment"].to_list() == [None, None, 500.0]
    assert result["mean_payment"].to_list() == [None, None, 100.0]


def test_metrics_do_not_divide_by_zero():
    result = weekly_metrics([0, 10, 20], [1, 12, 18])
    assert result["mae"] == 5 / 3
    assert result["mape"] == 15
    assert result["mape_excluded_zero"] == 1
    assert peak_metrics([False, True, True], [False, True, False])["recall"] == 0.5


def test_backtest_never_trains_on_future():
    splits = list(
        rolling_origin_splits(
            [date(2018, 1, 1), date(2019, 8, 1), date(2020, 8, 1)], years=[2019, 2020]
        )
    )
    assert len(splits) == 2
    assert all(max(train) < min(test) for _, train, test in splits)

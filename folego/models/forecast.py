import numpy as np

from .attributable import attributable


def forecast_excess(model, basis, history, forecast, confounders, *, cost_per_admission):
    """Preserve observed lag history in both scenarios; replace only future exposure."""
    history, forecast = np.asarray(history, float), np.asarray(forecast, float)
    if len(history) < basis.max_lag or not 1 <= len(forecast) <= 5:
        raise ValueError("Need full observed lag history and a 1–5 day forecast")
    if not np.isfinite(history).all() or not np.isfinite(forecast).all():
        raise ValueError("Incomplete PM2.5 history/forecast")
    context = history[-basis.max_lag :] if basis.max_lag else np.array([])
    actual = basis.transform(np.r_[context, forecast])[-len(forecast) :]
    counter = basis.transform(np.r_[context, np.full(len(forecast), basis.reference)])[
        -len(forecast) :
    ]
    if len(confounders) != len(forecast):
        raise ValueError("Confounder horizon mismatch")
    return attributable(
        model,
        np.column_stack([actual, confounders]),
        np.column_stack([counter, confounders]),
        cost_per_admission=cost_per_admission,
    )


def alert_level(relative_excess: float) -> str:
    if not np.isfinite(relative_excess):
        raise ValueError("Invalid relative excess")
    return (
        "normal"
        if relative_excess < 0.05
        else "attention"
        if relative_excess < 0.10
        else "alert"
        if relative_excess < 0.20
        else "emergency"
    )

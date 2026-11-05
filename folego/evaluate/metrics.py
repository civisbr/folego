import numpy as np


def weekly_metrics(observed, predicted):
    observed, predicted = np.asarray(observed, float), np.asarray(predicted, float)
    if observed.shape != predicted.shape or not len(observed):
        raise ValueError("Empty/mismatched series")
    if not np.isfinite(observed).all() or not np.isfinite(predicted).all():
        raise ValueError("Missing values")
    nonzero = observed != 0
    return {
        "mae": float(np.mean(abs(observed - predicted))),
        "mape": float(
            np.round(
                np.mean(abs((observed[nonzero] - predicted[nonzero]) / observed[nonzero])) * 100, 10
            )
        )
        if nonzero.any()
        else None,
        "mape_excluded_zero": int((~nonzero).sum()),
    }


def peak_metrics(actual, predicted):
    actual, predicted = np.asarray(actual, bool), np.asarray(predicted, bool)
    if actual.shape != predicted.shape:
        raise ValueError("Mismatched peaks")
    tp = int((actual & predicted).sum())
    return {
        "precision": tp / int(predicted.sum()) if predicted.any() else None,
        "recall": tp / int(actual.sum()) if actual.any() else None,
    }

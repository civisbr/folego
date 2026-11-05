"""Quasi-Poisson GLM with explicit, reusable confounder design."""

import numpy as np
import statsmodels.api as sm
from patsy import build_design_matrices, dmatrix


def fit_count_model(counts, design, *, offset=None):
    counts, design = np.asarray(counts, float), np.asarray(design, float)
    if counts.ndim != 1 or design.ndim != 2 or len(counts) != len(design):
        raise ValueError("Incompatible outcome/design shapes")
    if (counts < 0).any() or not np.isfinite(counts).all() or not np.isfinite(design).all():
        raise ValueError("Missing or invalid model inputs")
    x = np.column_stack([np.ones(len(design)), design])
    if np.linalg.matrix_rank(x) != x.shape[1]:
        raise ValueError("Rank-deficient design")
    if len(counts) <= x.shape[1]:
        raise ValueError("Insufficient observations")
    model = sm.GLM(counts, x, family=sm.families.Poisson(), offset=offset).fit()
    # Pearson scale implements overdispersion without claiming sub-Poisson certainty.
    scale = max(1.0, float(model.pearson_chi2 / model.df_resid))
    return sm.GLM(counts, x, family=sm.families.Poisson(), offset=offset).fit(scale=scale)


def predict_counts(model, design, *, offset=None):
    return model.predict(np.column_stack([np.ones(len(design)), design]), offset=offset)


class ConfounderDesign:
    """Fit on training rows only; preserve spline knots and weekday levels.

    Natural time splines extrapolate beyond training; seasonal harmonics repeat.
    A holiday indicator is supplied from an externally verified local calendar.
    """

    def __init__(self, include_srag=False):
        self.include_srag = include_srag
        self.info = None
        self.origin = None

    def transform(self, frame, *, fit=False):
        import pandas as pd

        data = frame.to_pandas() if hasattr(frame, "to_pandas") else frame.copy()
        dates = pd.to_datetime(data["date"])
        if fit:
            self.origin = dates.min()
        if self.origin is None:
            raise ValueError("Fit confounders before predicting")
        data["trend"] = (dates - self.origin).dt.days / 365.25
        data["weekday"] = dates.dt.dayofweek
        for h in (1, 2, 3):
            data[f"sin{h}"] = np.sin(2 * np.pi * h * dates.dt.dayofyear / 365.25)
            data[f"cos{h}"] = np.cos(2 * np.pi * h * dates.dt.dayofyear / 365.25)
        # Fixed wide boundaries keep forecasts valid; knots are learned on past data.
        formula = (
            "0 + bs(temperature, df=4, lower_bound=-30, upper_bound=60)"
            " + bs(humidity, df=4, lower_bound=0, upper_bound=100)"
            " + cr(trend, df=6, constraints='center')"
            " + C(weekday, levels=[0,1,2,3,4,5,6]) + holiday"
            " + sin1 + cos1 + sin2 + cos2 + sin3 + cos3"
        )
        if self.include_srag:
            formula += " + srag"
        if fit:
            design = dmatrix(formula, data, NA_action="raise")
            self.info = design.design_info
        elif self.info is not None:
            design = build_design_matrices([self.info], data, NA_action="raise")[0]
        else:
            raise ValueError("Unfitted design")
        # Drop the first weekday column: fit_count_model adds an intercept.
        return np.asarray(design)[:, 1:]

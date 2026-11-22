"""Quasi-Poisson GLM with explicit, reusable confounder design."""

import numpy as np
import statsmodels.api as sm
from patsy import build_design_matrices, dmatrix


def fit_count_model(counts, design, *, offset=None, hac_lags=None):
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
    if hac_lags is None:
        fitted = sm.GLM(counts, x, family=sm.families.Poisson(), offset=offset).fit(scale=scale)
    else:
        # Newey-West sandwich: the point estimate is the Poisson one, the covariance
        # absorbs both overdispersion and residual autocorrelation up to hac_lags days.
        # Unlike the quasi-Poisson scale it has no floor at 1 and no small-sample correction.
        fitted = sm.GLM(counts, x, family=sm.families.Poisson(), offset=offset).fit(
            cov_type="HAC", cov_kwds={"maxlags": hac_lags}
        )
    # A sparse stratum with a dense trend can stop with huge, meaningless coefficients.
    if not fitted.converged:
        raise ValueError("GLM did not converge")
    return fitted


def predict_counts(model, design, *, offset=None):
    return model.predict(np.column_stack([np.ones(len(design)), design]), offset=offset)


class ConfounderDesign:
    """Fit on training rows only; preserve spline knots and weekday levels.

    Natural time splines extrapolate beyond training; seasonal harmonics repeat.
    A holiday indicator is supplied from an externally verified local calendar.
    """

    # Means of the previous days, built by the study from contiguous rows.
    LAGGED_METEOROLOGY = ("temperature_lag1_7", "temperature_lag8_21", "humidity_lag1_7")

    def __init__(
        self,
        include_srag=False,
        *,
        trend_df_per_year=None,
        harmonics=3,
        extra=(),
        lagged_meteorology=False,
    ):
        # None keeps the forecasting design (6 df trend + harmonics, which repeat beyond
        # training). Inference uses a dense time spline instead (~7 df/year, Bhaskaran et
        # al. 2013), which absorbs season and epidemics but extrapolates poorly.
        if trend_df_per_year is not None and not (
            isinstance(trend_df_per_year, int | float) and 0 < trend_df_per_year < 366
        ):
            raise ValueError("trend_df_per_year must be positive and finite")
        if not isinstance(harmonics, int) or harmonics < 0:
            raise ValueError("harmonics must be a non-negative integer")
        import keyword

        # Names go into a patsy formula, which evaluates Python; a bare string would also
        # be split into one term per letter.
        reserved = {"trend", "weekday", "holiday", "temperature", "humidity", "srag", "C", "I"}
        # A data column named like a formula function would shadow it.
        reserved |= set(self.LAGGED_METEOROLOGY) | {"bs", "cr", "np"}
        if isinstance(extra, str) or not all(
            isinstance(c, str)
            and c.isidentifier()
            and not keyword.iskeyword(c)
            and c not in reserved
            and not c.startswith(("sin", "cos"))
            for c in extra
        ):
            raise ValueError("extra confounders must be a sequence of new column names")
        self.include_srag = include_srag
        self.extra = tuple(extra)
        self.lagged_meteorology = bool(lagged_meteorology)
        self.trend_df_per_year = trend_df_per_year
        self.harmonics = harmonics
        self.trend_df = 6
        self.info = None
        self.origin = None

    @property
    def columns(self):
        """Optional columns a row needs before it can enter the design."""
        return self.extra + (self.LAGGED_METEOROLOGY if self.lagged_meteorology else ())

    def transform(self, frame, *, fit=False):
        import pandas as pd

        data = frame.to_pandas() if hasattr(frame, "to_pandas") else frame.copy()
        dates = pd.to_datetime(data["date"])
        if fit:
            self.origin = dates.min()
            if self.trend_df_per_year is not None:
                # Days present, not the calendar span: gaps carry no information.
                years = dates.nunique() / 365.25
                self.trend_df = max(3, round(self.trend_df_per_year * years))
        if self.origin is None:
            raise ValueError("Fit confounders before predicting")
        data["trend"] = (dates - self.origin).dt.days / 365.25
        data["weekday"] = dates.dt.day_of_week
        for h in range(1, self.harmonics + 1):
            data[f"sin{h}"] = np.sin(2 * np.pi * h * dates.dt.day_of_year / 365.25)
            data[f"cos{h}"] = np.cos(2 * np.pi * h * dates.dt.day_of_year / 365.25)
        # Fixed wide boundaries keep forecasts valid; knots are learned on past data.
        formula = (
            "0 + bs(temperature, df=4, lower_bound=-30, upper_bound=60)"
            " + bs(humidity, df=4, lower_bound=0, upper_bound=100)"
            f" + cr(trend, df={self.trend_df}, constraints='center')"
            " + C(weekday, levels=[0,1,2,3,4,5,6]) + holiday"
        )
        formula += "".join(f" + sin{h} + cos{h}" for h in range(1, self.harmonics + 1))
        if self.lagged_meteorology:
            formula += (
                " + bs(temperature_lag1_7, df=3, lower_bound=-30, upper_bound=60)"
                " + bs(temperature_lag8_21, df=3, lower_bound=-30, upper_bound=60)"
                " + bs(humidity_lag1_7, df=3, lower_bound=0, upper_bound=100)"
            )
        if self.include_srag:
            formula += " + srag"
        formula += "".join(f" + {c}" for c in self.extra)
        if fit:
            design = dmatrix(formula, data, NA_action="raise")
            self.info = design.design_info
        elif self.info is not None:
            design = build_design_matrices([self.info], data, NA_action="raise")[0]
        else:
            raise ValueError("Unfitted design")
        # Drop the first weekday column: fit_count_model adds an intercept.
        return np.asarray(design)[:, 1:]

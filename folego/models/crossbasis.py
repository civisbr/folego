"""Tensor cross-basis: centered linear hinges × Legendre lag basis.

This is a DLNM-style basis, not a numerical clone of R's default ns()/ns().
Knots and reference are fixed before fitting and reused for every prediction.
"""

from dataclasses import dataclass

import numpy as np
from numpy.polynomial.legendre import legvander


@dataclass(frozen=True)
class CrossBasis:
    max_lag: int = 7
    knots: tuple[float, ...] = (15.0, 35.0, 75.0)
    lag_degree: int = 2
    reference: float = 15.0

    def transform(self, exposure) -> np.ndarray:
        x = np.asarray(exposure, dtype=float)
        if x.ndim != 1 or self.max_lag < 0 or not 0 <= self.lag_degree <= self.max_lag:
            raise ValueError("Invalid exposure or lag specification")
        if np.any(np.isfinite(x) & (x < 0)):
            raise ValueError("Negative PM2.5")
        features = (
            np.column_stack(
                [
                    x - self.reference,
                    *[np.maximum(x - k, 0) - max(self.reference - k, 0) for k in self.knots],
                ]
            )
            / 10
        )
        lags = legvander(np.linspace(-1, 1, self.max_lag + 1), self.lag_degree)
        result = np.full((len(x), features.shape[1] * lags.shape[1]), np.nan)
        for t in range(self.max_lag, len(x)):
            window = features[t - np.arange(self.max_lag + 1)]
            result[t] = (window.T @ lags).ravel()
        return result

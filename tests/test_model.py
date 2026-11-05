"""Acceptance with a known synthetic effect, written before the model."""

import numpy as np
import pytest

from folego.models.attributable import attributable
from folego.models.crossbasis import CrossBasis
from folego.models.glm import fit_count_model, predict_counts


def test_crossbasis_recovers_known_distributed_effect():
    rng = np.random.default_rng(42)
    exposure = rng.uniform(5, 90, 6000)
    basis = CrossBasis(max_lag=2, knots=(), lag_degree=2, reference=15)
    design = basis.transform(exposure)
    valid = np.isfinite(design).all(axis=1)
    # Ground truth: a log-relative risk of 0.08 per 10 µg/m³, lag zero only.
    mu = np.exp(3 + 0.008 * (exposure - 15))
    counts = rng.poisson(mu)
    model = fit_count_model(counts[valid], design[valid])
    prediction = predict_counts(model, design[valid])
    assert np.corrcoef(prediction, mu[valid])[0, 1] > 0.99
    assert np.mean(abs(prediction - mu[valid]) / mu[valid]) < 0.03
    assert model.scale >= 1


def test_history_is_not_zero_padded_and_reference_is_zero():
    basis = CrossBasis(max_lag=7)
    result = basis.transform(np.full(12, 15.0))
    assert np.isnan(result[:7]).all()
    assert np.allclose(result[7:], 0)


def test_counterfactual_and_uncertainty_share_coefficient_draws():
    model = fit_count_model(np.array([10, 12, 15, 19, 24, 30]), np.arange(6)[:, None])
    actual = np.array([[2.0], [3.0]])
    counterfactual = np.zeros((2, 1))
    result = attributable(model, actual, counterfactual, cost_per_admission=1000)
    assert result["excess"] > 0
    assert result["lower"] <= result["excess"] <= result["upper"]
    assert result["payment"] == pytest.approx(result["excess"] * 1000)
    same = attributable(model, actual, actual, cost_per_admission=1000)
    assert same["excess"] == same["lower"] == same["upper"] == 0

import numpy as np

from .glm import predict_counts


def attributable(model, actual, counterfactual, *, cost_per_admission, draws=2000, seed=42):
    """Joint coefficient draws retain covariance across dates and scenarios.

    Interval covers conditional mean excess, not future count noise or input error.
    Signed excess is retained; no post-estimation positive clipping.
    """
    actual, counterfactual = np.asarray(actual, float), np.asarray(counterfactual, float)
    if actual.shape != counterfactual.shape:
        raise ValueError("Scenario shape mismatch")
    if not np.isfinite(cost_per_admission) or cost_per_admission < 0:
        raise ValueError("Invalid AIH payment")
    difference = predict_counts(model, actual) - predict_counts(model, counterfactual)
    coefficients = np.random.default_rng(seed).multivariate_normal(
        model.params, model.cov_params(), size=draws
    )
    xa = np.column_stack([np.ones(len(actual)), actual])
    xc = np.column_stack([np.ones(len(counterfactual)), counterfactual])
    samples = (np.exp(xa @ coefficients.T) - np.exp(xc @ coefficients.T)).sum(axis=0)
    lower, upper = np.quantile(samples, [0.025, 0.975])
    excess = float(difference.sum())
    return {
        "excess": excess,
        "lower": float(lower),
        "upper": float(upper),
        "payment": excess * cost_per_admission,
        "interval": "95% conditional mean",
    }

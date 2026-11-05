import numpy as np
from scipy.stats import chi2


def joint_exposure_test(model, exposure_columns):
    """Joint Wald test; significant results trigger bias investigation, not acceptance."""
    indices = np.asarray(exposure_columns, int)
    if not len(indices) or (indices < 1).any():
        raise ValueError("Select exposure coefficients only")
    beta = np.asarray(model.params)[indices]
    covariance = np.asarray(model.cov_params())[np.ix_(indices, indices)]
    statistic = float(beta @ np.linalg.solve(covariance, beta))
    pvalue = float(chi2.sf(statistic, len(indices)))
    return {"wald": statistic, "pvalue": pvalue, "requires_investigation": pvalue < 0.05}

"""M5 regime model: 2-state Gaussian HMM on weekly index return and volatility, plus VIX
when available (§10.4). The stressed state is the one with the higher volatility mean."""
from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def regime(index_adj: pd.Series, vix: pd.Series | None, cfg: dict) -> dict:
    m = cfg["settings"]["models"]
    r = cfg["risk"]["R4_regime"]
    px = index_adj.dropna()
    wk = px.resample("W-FRI").last().dropna()
    ret = np.log(wk).diff()
    dvol = np.log(px).diff().rolling(20).std().resample("W-FRI").last() * np.sqrt(252)
    feats = pd.concat([ret.rename("ret"), dvol.rename("vol")], axis=1)
    if vix is not None and len(vix.dropna()) > 50:
        feats = feats.join(vix.resample("W-FRI").last().rename("vix"))
    feats = feats.dropna()
    if len(feats) < 52:
        return {"label": "neutral", "stressed_prob": None, "calm_prob": None, "method": "insufficient_history"}
    Z = (feats - feats.mean()) / feats.std()
    try:
        from hmmlearn.hmm import GaussianHMM
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            hmm = GaussianHMM(n_components=m["hmm_states"], covariance_type="diag", n_iter=200,
                              random_state=m["seed"]).fit(Z.values)
        post = hmm.predict_proba(Z.values)   # last row of the smoothed posterior == filtered prob
        vol_col = list(Z.columns).index("vol")
        stressed = int(np.argmax(hmm.means_[:, vol_col]))
        p_st = float(post[-1, stressed])
        method = "gaussian_hmm"
    except Exception as e:
        log.warning("HMM failed (%s); falling back to vol percentile", e)
        p_st = float((feats["vol"] <= feats["vol"].iloc[-1]).mean())
        method = "vol_percentile"
    p_calm = 1 - p_st
    if p_st > r["stressed_prob_high"]:
        label = "stressed"
    elif p_calm > r["calm_prob_low"]:
        label = "calm"
    else:
        label = "neutral"
    return {"label": label, "stressed_prob": round(p_st, 4), "calm_prob": round(p_calm, 4), "method": method,
            "weeks": int(len(feats))}

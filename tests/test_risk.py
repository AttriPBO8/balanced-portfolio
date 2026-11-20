import pandas as pd
import pytest

from common.config import load_config
from models.risk import DIMS, apply_labels, composite, dimension_labels

CFG = None


@pytest.fixture(scope="module")
def cfg():
    from tests.conftest import REPO
    return load_config(REPO)


def row(**kw):
    base = {d: "Low" for d in DIMS}
    base.update(kw)
    return pd.Series(base)


def test_low_needs_five_low_and_no_high(cfg):
    assert composite(row(), cfg) == "Low"
    assert composite(row(R1="Moderate", R2="Moderate"), cfg) == "Low"
    assert composite(row(R1="Moderate", R2="Moderate", R4="Moderate"), cfg) == "Moderate"
    assert composite(row(R1="High"), cfg) == "Moderate"     # one High that isn't R3/R5/R7


@pytest.mark.parametrize("dim", ["R3", "R5", "R7"])
def test_any_of_r3_r5_r7_high_is_high(cfg, dim):
    assert composite(row(**{dim: "High"}), cfg) == "High"


def test_three_highs_is_high(cfg):
    assert composite(row(R1="High", R2="High", R4="High"), cfg) == "High"
    assert composite(row(R1="High", R2="High"), cfg) == "Moderate"


def _table():
    return pd.DataFrame({
        "garch_sigma": [0.1, 0.2, 0.3], "har_vs_garch": [0.0, 0.2, 0.5], "halfwidth": [0.03, 0.06, 0.09],
        "break_age_days": [None, 400, 30], "m1_pct": [0.9, 0.5, 0.1], "m2_pct": [0.95, 0.5, 0.9],
        "liquid": [True, True, False], "gap_age": [None, 10, 2],
    }, index=["A", "B", "C"]).astype({"break_age_days": float, "gap_age": float})


def test_dimension_thresholds(cfg):
    d = dimension_labels(_table(), {"stressed_prob": 0.1, "calm_prob": 0.9}, 0.6, cfg)
    assert list(d["R1"]) == ["Low", "Moderate", "High"]
    assert list(d["R2"]) == ["Low", "Moderate", "High"]
    assert list(d["R3"]) == ["Low", "Moderate", "High"]
    assert set(d["R4"]) == {"Low"}
    assert list(d["R5"]) == ["Low", "Moderate", "High"]
    assert list(d["R6"]) == ["Low", "Low", "High"]
    assert list(d["R7"]) == ["Low", "Moderate", "High"]


def test_overrides_logged_in_order(cfg):
    t = _table()
    reg = {"stressed_prob": 0.8, "calm_prob": 0.2}
    d = dimension_labels(t, reg, 0.6, cfg)
    comp, log = apply_labels(d, t, reg, cfg)
    assert [o["override"] for o in log] == ["regime_stressed", "liquidity_floor", "data_gap"]
    assert "Moderate" not in set(comp)
    assert comp["C"] == "High"

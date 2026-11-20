import pytest

from callouts.addon import HELPS, NEUTRAL, NOT, assess
from common.config import load_config


@pytest.fixture(scope="module")
def cfg():
    from tests.conftest import REPO
    return load_config(REPO)


def base(**kw):
    d = dict(price=40.0, acb_per_share=45.0, add_value=1000.0, position_value=1600.0, invested=12000.0,
             limit=3000.0, composite="Low", r5="Low", blend_pct=0.9, first_buy_pct=0.85,
             dist_126d_high=-0.12, garch_sigma=0.22, risk_contrib_change=0.02, negative_event=False,
             dividend_cut=False, qty=40.0)
    d.update(kw)
    return d


def test_average_down_helps_within_normal_pullback(cfg):
    a = assess(cfg, **base())
    assert a["mode"] == "AVERAGE_DOWN" and a["verdict"] == HELPS and a["thesis_intact"]
    assert a["new_acb_cad"] == pytest.approx((45 * 40 + 25 * 40) / 65)
    assert 2 <= len(a["reasons"]) <= 4


@pytest.mark.parametrize("kw,needle", [
    ({"composite": "Moderate"}, "risk label"),
    ({"r5": "High"}, "structural break"),
    ({"position_value": 2500.0}, "above"),
    ({"blend_pct": 0.5}, "thesis"),
    ({"first_buy_pct": 0.99, "blend_pct": 0.75}, "thesis"),
    ({"dividend_cut": True}, "dividend cut"),
    ({"negative_event": True}, "negative event"),
])
def test_does_not_help_rules(cfg, kw, needle):
    a = assess(cfg, **base(**kw))
    assert a["verdict"] == NOT
    assert any(needle in r for r in a["reasons"])


def test_never_average_down_on_broken_thesis(cfg):
    a = assess(cfg, **base(blend_pct=0.4, price=30.0))
    assert a["mode"] == "AVERAGE_DOWN" and a["verdict"] == NOT


def test_neutral_when_risk_contribution_too_high(cfg):
    assert assess(cfg, **base(risk_contrib_change=0.09))["verdict"] == NEUTRAL


def test_neutral_when_pullback_too_deep(cfg):
    assert assess(cfg, **base(dist_126d_high=-0.6))["verdict"] == NEUTRAL


def test_average_up_needs_score_rise(cfg):
    up = assess(cfg, **base(price=50.0, first_buy_pct=0.8, blend_pct=0.9))
    assert up["mode"] == "AVERAGE_UP" and up["verdict"] == HELPS
    flat = assess(cfg, **base(price=50.0, first_buy_pct=0.92, blend_pct=0.9))
    assert flat["verdict"] == NEUTRAL

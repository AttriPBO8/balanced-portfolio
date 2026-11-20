"""Backtest replay, simulator, baseline, DSR and PBO."""
import datetime as dt
import importlib.util
from collections import Counter

import numpy as np
import pandas as pd
import pytest

from backtest.replay import replay_live, run_backtest, write_outputs
from backtest.simulator import Book
from backtest.stats import deflated_sharpe, pbo_cscv, perf_summary
from common.config import load_config
from features.snapshot import features_at, label_for
from ingest.macro import sample_macro
from ingest.prices import SampleSource
from pipeline.core import purge_labels
from pipeline.run_weekly import run_weekly
from tests.conftest import REPO, TEST_UNIVERSE, make_root, utc

FEES = {"per_trade_cad": 9.95, "half_spread_bps": 10}


def test_purge_equals_truncated_labels():
    src = SampleSource()
    full = src.fetch(TEST_UNIVERSE + ["XIC.TO"], dt.date(2021, 1, 1), dt.date(2025, 12, 31))
    D = dt.date(2025, 6, 14)
    dates = [D - dt.timedelta(days=7 * k) for k in range(30)][::-1]
    f = features_at(full, dates, "XIC.TO", TEST_UNIVERSE)
    purged = purge_labels(label_for(f, full, 63), f, full.adj_close.index, dt.date(2025, 6, 13), 63)
    trunc = full.upto(dt.date(2025, 6, 13))
    expected = label_for(f, trunc, 63)
    pd.testing.assert_series_equal(purged.isna(), expected.isna(), check_names=False)
    both = purged.notna()
    np.testing.assert_allclose(purged[both].values, expected[both].values)


def test_book_accounting_and_delisting():
    b = Book.start({"TFSA": 1000.0})
    n = b.buy("TFSA", "X.TO", 500, 10.0, dt.date(2026, 1, 5), FEES)   # more than cash allows
    fill = 10.0 * 1.001
    assert n == int((1000 - 9.95) // fill)
    assert b.cash["TFSA"] == pytest.approx(1000 - n * fill - 9.95)
    b.sell("TFSA", "X.TO", 10, 12.0, dt.date(2026, 1, 12), FEES)
    assert b.qty("TFSA", "X.TO") == n - 10
    cash_before = b.cash["TFSA"]
    b.delist("X.TO", dt.date(2026, 2, 1), 5.0, -0.5)
    assert b.qty("TFSA", "X.TO") == 0
    assert b.cash["TFSA"] == pytest.approx(cash_before + (n - 10) * 2.5)
    assert len(b.trades) == 2   # delisting is not counted as an owner transaction


def test_perf_summary_and_dsr():
    nav = pd.Series(np.cumprod(np.r_[1.0, 1 + np.full(60, 0.002) + np.tile([0.01, -0.01], 30)]))
    s = perf_summary(nav)
    assert s["total_return"] > 0 and s["sharpe"] > 0 and s["max_drawdown"] <= 0
    assert perf_summary(nav.iloc[:5])["sharpe"] is None
    rng = np.random.default_rng(0)
    r = rng.normal(0.003, 0.02, 200)
    few = deflated_sharpe(r, [0.05, 0.1])
    many = deflated_sharpe(r, list(rng.normal(0.0, 0.1, 50)))
    assert 0 <= many["dsr"] <= few["dsr"] <= 1      # more trials -> harder to believe


def test_pbo_detects_noise_vs_skill():
    rng = np.random.default_rng(1)
    noise = pd.DataFrame(rng.normal(0, 0.02, (200, 12)))
    p_noise = pbo_cscv(noise, 10)["pbo"]
    skill = noise.copy()
    skill[0] += 0.01                                  # one variant is genuinely better
    p_skill = pbo_cscv(skill, 10)["pbo"]
    assert p_noise > 0.2 and p_skill < 0.1


@pytest.fixture(scope="module")
def bt(tmp_path_factory):
    root = make_root(tmp_path_factory.mktemp("bt"))
    cfg = load_config(root)
    s = cfg["settings"]
    tick = TEST_UNIVERSE + ["XIC.TO", "XIU.TO"] + [t for t in s["baseline"]["tickers"] if t not in TEST_UNIVERSE]
    panel = SampleSource().fetch(tick, dt.date(2021, 1, 1), dt.date(2024, 4, 30))
    res = run_backtest(cfg, panel, sample_macro(dt.date(2021, 1, 1), dt.date(2024, 4, 30)),
                       dt.date(2024, 1, 6), dt.date(2024, 4, 27), root, with_validation=False)
    return root, cfg, res


def test_backtest_runs_and_respects_caps(bt):
    root, cfg, res = bt
    nav = res["nav"]
    assert len(nav) >= 15 and {"callouts", "baseline", "XIC.TO", "XIU.TO"} <= set(nav.columns)
    caps = cfg["settings"]["transaction_caps"]
    for trades in (res["trades"], res["baseline_trades"]):
        if len(trades):
            cnt = Counter((t.account_type, t.date.year, t.date.month) for t in trades.itertuples())
            for (acct, y, m), n in cnt.items():
                assert n <= caps[acct]["max_per_month"], (acct, y, m, n)
    assert len(res["frozen"]) >= 10
    assert all(len(f["callouts"]) <= 25 for f in res["frozen"])
    r = res["report"]
    assert r["verdict"] and "survivorship" in r["caveat"]
    assert r["dsr"]["n_trials"] == 18
    assert res["variants"].shape[1] == 18


def test_backtest_outputs_and_dashboard(bt):
    root, cfg, res = bt
    out = write_outputs(root, res, cfg, "sample")
    for f in ("report.json", "nav.csv", "trades.csv", "callouts.jsonl", "variants.csv"):
        assert (out / f).exists()
    run_weekly(root, now=utc(2026, 11, 14, 15), source=SampleSource())
    spec = importlib.util.spec_from_file_location("site_build", REPO / "site/build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    html = mod.build(root, root / "_site", now=dt.datetime(2026, 11, 14, 16, tzinfo=dt.timezone.utc)).read_text()
    for needle in ("id='backtest'", "Deflated Sharpe", "overfitting", "Naive baseline", "Table view", "<polyline"):
        assert needle in html, needle


def test_live_replay_reads_frozen_callouts(tmp_path):
    root = make_root(tmp_path)
    for d in (dt.date(2026, 10, 31), dt.date(2026, 11, 7)):
        run_weekly(root, now=utc(d.year, d.month, d.day, 15), source=SampleSource())
    cfg = load_config(root)
    panel = SampleSource().fetch(TEST_UNIVERSE + ["XIC.TO", "XIU.TO"] + cfg["settings"]["baseline"]["tickers"],
                                 dt.date(2021, 1, 1), dt.date(2026, 11, 13))
    lr = replay_live(root, cfg, panel, dt.date(2026, 11, 14))
    assert lr["weeks"] == 2 and lr["start"] == "2026-10-31"
    assert {"callouts", "baseline", "XIC.TO"} <= set(lr["series"])

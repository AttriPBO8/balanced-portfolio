"""Two-step US screener: candidate parsing, step-1 filters, step-2 scoring with RRSP-only buys."""
import datetime as dt
import json

import pandas as pd
import yaml

from common.config import buy_accounts_for, buy_list, load_config, universe, with_us_screen
from ingest.prices import SampleSource
from ingest.us_universe import parse_constituents
from pipeline import us_screen

HTML = ("<html><table><tr><td>nav</td></tr></table><table class='wikitable'><tr><th>Symbol</th><th>Security</th>"
        "<th>GICS Sector</th><th>Sub</th></tr>"
        + "".join(f"<tr><td><a href='#'>T{i}</a></td><td>Co {i}</td><td>{'Energy' if i % 2 else 'Health Care'}</td><td>x</td></tr>"
                  for i in range(25))
        + "<tr><td>BRK.B</td><td>Berkshire</td><td>Financials</td><td>x</td></tr></table></html>")


def test_parse_constituents():
    df = parse_constituents(HTML, "sp500")
    assert len(df) == 26 and "BRK-B" in set(df["ticker"])          # Yahoo dash form
    assert set(df["sector"]) == {"Energy", "Health Care", "Financials"}


def _cands(n=60):
    secs = ["Energy", "Health Care", "Information Technology", "Financials"]
    return pd.DataFrame({"ticker": [f"US{i:02d}" for i in range(n)], "name": "", "sector": [secs[i % 4] for i in range(n)],
                         "index": "sp500", "as_of": "2026-11-01"})


def test_screen_filters_ranks_and_caps_sectors():
    c = _cands()
    end = dt.date(2026, 11, 26)
    panel = SampleSource().fetch(list(c["ticker"]), dt.date(2024, 9, 1), end)
    sc = dict(min_price_cad=1, min_median_traded_value_cad=0, min_history_days=300, max_vol_60d=5,
              weights={"mom_126d": 0.4, "mom_60d": 0.2, "low_vol": 0.2, "liquidity": 0.2}, max_per_sector=5, keep=12)
    kept, counts = us_screen.screen(c, panel, end, sc)
    assert len(kept) == 12 and counts["kept"] == 12
    assert kept.groupby("sector").size().max() <= 5
    assert list(kept["rank"]) == list(range(1, 13)) and kept["score"].is_monotonic_decreasing
    strict, counts2 = us_screen.screen(c, panel, end, dict(sc, min_median_traded_value_cad=1e15))
    assert strict.empty and counts2["liquidity"] == 0


def test_run_writes_list_and_respects_refresh(root):
    (root / "data/universe").mkdir(parents=True, exist_ok=True)
    _cands(40).to_csv(root / "data/universe/us_candidates.csv", index=False)
    cfg = load_config(root)
    cfg["settings"]["us_screen"].update(min_price_cad=1, min_median_traded_value_cad=0, min_history_days=300,
                                        max_vol_60d=5, keep=10)
    now = dt.datetime(2026, 11, 27, 15, tzinfo=dt.timezone.utc)
    res = us_screen.run(root, cfg, now=now, source=SampleSource())
    assert res["status"] == "screened" and res["kept"] == 10
    assert (root / "artifacts/us_screen/2026-11-27.json").exists()
    assert us_screen.run(root, cfg, now=now + dt.timedelta(days=7), source=SampleSource())["status"] == "fresh"
    assert us_screen.run(root, cfg, now=now + dt.timedelta(days=30), source=SampleSource())["status"] == "screened"


def test_screened_names_join_scoring_and_buy_only_in_rrsp(root):
    (root / "data/universe").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": ["AAPL", "MSFT"], "sector": ["Information Technology"] * 2, "name": "", "rank": [1, 2],
                  "score": [0.9, 0.8], "price_cad": 1, "adv_60d_cad": 1, "mom_126d": 0, "vol_60d": 0,
                  "as_of": "2026-11-27"}).to_csv(root / "data/universe/us_screened.csv", index=False)
    cfg = load_config(root)
    assert with_us_screen(cfg, root) == ["AAPL", "MSFT"]
    assert {"AAPL", "MSFT"} <= set(universe(cfg)) and {"AAPL", "MSFT"} <= set(buy_list(cfg))
    assert cfg["sectors"]["AAPL"] == "Information Technology"
    assert buy_accounts_for(cfg, "AAPL") == ["RRSP"]
    assert buy_accounts_for(cfg, "RY.TO") == ["TFSA", "NONREG", "RRSP"]      # TFSA first, RRSP kept for US
    cfg["settings"]["us_screen"]["enabled"] = False
    assert buy_accounts_for(cfg, "RY.TO") is None
    cfg["settings"]["us_screen"]["enabled"] = True
    cfg["settings"]["us_screen"]["buy_enabled"] = False
    assert "AAPL" not in buy_list(cfg)


def test_weekly_with_us_screen_routes_us_buys_to_rrsp(root):
    from pipeline.run_weekly import run_weekly
    tick = [f"ZU{i}" for i in range(30)]
    (root / "data/universe").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": tick, "sector": "Industrials", "name": "", "rank": range(1, 31), "score": 0.5,
                  "price_cad": 1, "adv_60d_cad": 1, "mom_126d": 0, "vol_60d": 0, "as_of": "2026-11-21"}
                 ).to_csv(root / "data/universe/us_screened.csv", index=False)
    run_weekly(root, now=dt.datetime(2026, 11, 21, 15, tzinfo=dt.timezone.utc))
    doc = json.loads((root / "artifacts/weekly/2026-11-21/callouts.json").read_text())
    assert len(doc["us_screened"]) == 30
    scores = pd.read_csv(root / "artifacts/weekly/2026-11-21/scores.csv")
    assert set(tick) <= set(scores["ticker"])                        # step 2: scored by the model
    for c in doc["callouts"]:
        if c["ticker"] in tick and c["class"] == "ACTION_BUY_NEW":
            assert c["account"] == "RRSP"


def test_screen_config_present():
    from tests.conftest import REPO
    s = yaml.safe_load((REPO / "config/settings.yaml").read_text())["us_screen"]
    assert s["accounts"] == ["RRSP"] and s["keep"] == 100 and len(s["candidate_sources"]) == 2

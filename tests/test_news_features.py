"""T3-6..T3-9 features, FinBERT storage and the 26-week shadow gate (§8.5)."""
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from common.config import load_config
from features.news import news_features, news_frame, shadow_gate
from ingest.finbert import load_scores, score_new
from pipeline.core import apply_news
from tests.conftest import REPO, make_root


def items_for(ticker, start, days, sentiment_ids=True):
    out = []
    for k in range(days):
        ts = (start + dt.timedelta(days=k)).strftime("%Y-%m-%dT12:00:00Z")
        out.append({"id": f"{ticker}{k}", "title": f"headline {k}", "tickers": [ticker], "event_type": "other",
                    "published_utc": ts, "first_seen_utc": ts, "backfilled": False})
    return out


def test_sentiment_z_needs_20_days_and_flags_negative_events():
    start = dt.datetime(2026, 10, 1)
    items = items_for("RY.TO", start, 30)
    items.append({"id": "neg", "title": "RY lowers guidance", "tickers": ["RY.TO"], "event_type": "guidance_cut",
                  "published_utc": "2026-10-29T12:00:00Z", "first_seen_utc": "2026-10-29T12:00:00Z"})
    items += items_for("TD.TO", start + dt.timedelta(days=25), 5)
    scores = {it["id"]: {"score": 0.1} for it in items}
    for k in range(25, 30):
        scores[f"RY.TO{k}"] = {"score": 0.9}         # sentiment jumps in the last 5 days
    for k in range(10):
        scores[f"RY.TO{k}"] = {"score": -0.1}
    nf = news_frame(items, scores)
    f = news_features(nf, ["RY.TO", "TD.TO", "BNS.TO"], pd.Timestamp("2026-10-31T00:00:00"))
    assert f.at["RY.TO", "sent_z20"] > 1
    assert bool(f.at["RY.TO", "neg_event_20d"]) and f.at["RY.TO", "last_negative_type"] == "guidance_cut"
    assert np.isnan(f.at["TD.TO", "sent_z20"])      # only 5 days of history
    assert f.at["BNS.TO", "n_events_20d"] == 0


def test_future_items_invisible():
    items = items_for("RY.TO", dt.datetime(2026, 11, 1), 10)
    nf = news_frame(items, {})
    f = news_features(nf, ["RY.TO"], pd.Timestamp("2026-11-03T00:00:00"))
    assert f.at["RY.TO", "n_events_20d"] == 2


def test_finbert_scores_stored_separately(tmp_path):
    p = tmp_path / "artifacts/news/2026/2026-11.jsonl"
    p.parent.mkdir(parents=True)
    its = items_for("RY.TO", dt.datetime(2026, 11, 1), 3)
    p.write_text("".join(json.dumps(i) + "\n" for i in its))
    fake = lambda texts: [{"pos": 0.7, "neg": 0.1, "neu": 0.2} for _ in texts]
    assert score_new(tmp_path, scorer=fake, model_name="fake") == 3
    assert score_new(tmp_path, scorer=fake, model_name="fake") == 0     # idempotent
    s = load_scores(tmp_path)
    assert s["RY.TO0"]["score"] == pytest.approx(0.6)
    assert p.read_text().count("\n") == 3                              # news file untouched


def test_apply_news_only_when_admitted():
    sc = pd.DataFrame({"blend_pct": [0.9, 0.5, 0.1]}, index=["A", "B", "C"])
    nf = pd.DataFrame({"news_score": [-2.0, 0.0, 3.0], "neg_event_20d": [True, False, False],
                       "last_negative_type": ["financing", None, None]}, index=["A", "B", "C"])
    same, neg, adm = apply_news(sc, {"features": nf, "admitted": False})
    assert not adm and neg == {} and same["blend_pct"].tolist() == [0.9, 0.5, 0.1]
    out, neg, adm = apply_news(sc, {"features": nf, "admitted": True, "weight": 0.9})
    assert adm and "A" in neg and out["blend_pct"].idxmax() == "C"   # news moved C to the top
    assert "blend_pre_news" in out


def test_gate_needs_26_weeks(tmp_path):
    from ingest.prices import SampleSource
    from pipeline.run_weekly import run_weekly
    from tests.conftest import utc
    root = make_root(tmp_path)
    for d in (dt.date(2026, 10, 31), dt.date(2026, 11, 7)):
        run_weekly(root, now=utc(d.year, d.month, d.day, 15), source=SampleSource())
    assert (root / "artifacts/weekly/2026-11-07/shadow_features.csv").exists()
    cfg = load_config(root)
    panel = SampleSource().fetch(["RY.TO", "TD.TO"], dt.date(2026, 1, 1), dt.date(2026, 11, 20))
    g = shadow_gate(root, panel.adj_close, dt.date(2026, 11, 14), cfg)
    assert g["shadow_weeks"] == 2 and g["status"] == "shadow" and not g["admitted"]
    cfg["settings"]["news_features"]["min_shadow_weeks"] = 1
    cfg["settings"]["news_features"]["force_admit"] = True
    assert shadow_gate(root, panel.adj_close, dt.date(2026, 11, 14), cfg)["admitted"]

"""One weekly decision, shared by production (pipeline.run_weekly) and the backtest
(backtest.replay) so both use the identical code path (§11.3, §13.1).

`decide` takes features and labels that were computed elsewhere, purges labels at the
decision date, fits the models, labels risk and builds the callouts. History (last week's
labels, first-buy scores, recently reviewed holdings) comes from a History object: files in
artifacts/ for production, an in-memory record for the backtest.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from callouts.builder import Builder, WeeklyInputs
from callouts.rules import budgets, superficial_blocks
from common.config import enabled_accounts, universe
from common.dates import last_close_on_or_before
from ingest.macro import macro_as_of
from ingest.prices import data_gaps
from models.breaks import break_table
from models.covariance import lw_cov
from models.rankers import fit_predict
from models.regime import regime as regime_model
from models.risk import apply_labels, dimension_labels
from models.volatility import universe_vols
from pipeline import state as st


class StepFailure(RuntimeError):
    pass


# ------------------------------------------------------------------ history providers

class FileHistory:
    """Production history read from committed artifacts."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def prev_labels(self, D: dt.date) -> dict:
        prev = st.latest_published(self.root, D - dt.timedelta(days=1))
        sc = st.load_scores(self.root, prev) if prev else None
        return sc["composite"].to_dict() if sc is not None and "composite" in sc else {}

    def first_buy_pct(self, portfolio) -> dict:
        t = portfolio.trades
        if t.empty:
            return {}
        weeks = st.published_weeks(self.root)
        out = {}
        for tick, g in t[t["side"] == "BUY"].groupby("ticker"):
            prior = [w for w in weeks if w <= min(g["date"])]
            if prior:
                sc = st.load_scores(self.root, prior[-1])
                if sc is not None and tick in sc.index:
                    out[tick] = float(sc.at[tick, "blend_pct"])
        return out

    def reviewed_recently(self, D: dt.date, n_weeks: int) -> set:
        weeks = [w for w in st.published_weeks(self.root) if w < D][-n_weeks:] if n_weeks > 0 else []
        seen = set()
        for w in weeks:
            c = st.load_callouts(self.root, w)
            if c:
                seen |= {x["ticker"] for x in c.get("callouts", [])}
        return seen


@dataclass
class MemoryHistory:
    """Backtest history: what the simulated weekly runs published so far."""
    weeks: list = field(default_factory=list)          # decision dates, ascending
    scores: dict = field(default_factory=dict)         # D -> DataFrame (blend_pct, composite, ...)
    callout_tickers: dict = field(default_factory=dict)  # D -> set of tickers

    def record(self, D: dt.date, scores: pd.DataFrame, callouts: list[dict]) -> None:
        self.weeks.append(D)
        self.scores[D] = scores[["blend_pct", "composite"]].copy()
        self.callout_tickers[D] = {c["ticker"] for c in callouts}

    def prev_labels(self, D: dt.date) -> dict:
        prior = [w for w in self.weeks if w < D]
        return self.scores[prior[-1]]["composite"].to_dict() if prior else {}

    def first_buy_pct(self, portfolio) -> dict:
        t = portfolio.trades
        if t.empty:
            return {}
        out = {}
        for tick, g in t[t["side"] == "BUY"].groupby("ticker"):
            prior = [w for w in self.weeks if w <= min(g["date"])]
            if prior and tick in self.scores[prior[-1]].index:
                out[tick] = float(self.scores[prior[-1]].at[tick, "blend_pct"])
        return out

    def reviewed_recently(self, D: dt.date, n_weeks: int) -> set:
        seen = set()
        for w in [w for w in self.weeks if w < D][-n_weeks:] if n_weeks > 0 else []:
            seen |= self.callout_tickers.get(w, set())
        return seen


# ------------------------------------------------------------------ purging

def purge_labels(y: pd.Series, feats: pd.DataFrame, index: pd.DatetimeIndex, close_D: dt.date, horizon: int) -> pd.Series:
    """Keep a label only if its whole horizon was realised by close_D (§13.2). Equivalent to
    computing labels on a panel truncated at close_D (tested in test_backtest)."""
    pos_D = index.searchsorted(pd.Timestamp(close_D), side="right") - 1
    row_pos = index.searchsorted(pd.DatetimeIndex(feats["row_date"]))
    ok = (row_pos + horizon) <= pos_D
    return y.where(ok)


# ------------------------------------------------------------------ news (shadow / admitted)

def apply_news(scores: pd.DataFrame, news_ctx: dict | None) -> tuple[pd.DataFrame, dict, bool]:
    """When the news feature group is admitted (§8.5), blend a fixed-weight news score into
    the ranking and switch on negative-event checks. Otherwise scores are unchanged."""
    if not news_ctx or not news_ctx.get("admitted"):
        return scores, {}, False
    nf = news_ctx["features"].reindex(scores.index)
    w = float(news_ctx.get("weight", 0.2))
    ns = nf["news_score"].fillna(0.0).rank(pct=True)
    s = scores.copy()
    s["blend_pre_news"] = s["blend_pct"]
    s["blend_pct"] = ((1 - w) * s["blend_pct"] + w * ns).rank(pct=True)
    neg = {t: f"negative event in 20 days ({nf.at[t, 'last_negative_type']})"
           for t in nf.index if bool(nf.at[t, "neg_event_20d"] or False)}
    return s, neg, True


# ------------------------------------------------------------------ the decision

def decide(cfg: dict, D: dt.date, panel, feats: pd.DataFrame, y_long: pd.Series, y_short: pd.Series,
           macro: pd.DataFrame | None, portfolio, history, low_conf: bool, news_ctx: dict | None = None,
           fail_at: str | None = None) -> dict:
    """Steps 5-6 of §5.4 for decision date D.

    panel must already be truncated at the last close <= D. feats/y_* are indexed (date,
    ticker) and must contain D; labels are purged here, so callers may pass unpurged labels.
    """
    t0 = time.time()
    bench = cfg["settings"]["data"]["benchmark"]
    m = cfg["settings"]["models"]
    tickers = [t for t in universe(cfg) if t in panel.adj_close.columns]
    close_D = last_close_on_or_before(D)
    p = panel
    dates = feats.index.get_level_values("date")
    feats = feats[dates <= pd.Timestamp(D)]
    dates = feats.index.get_level_values("date")
    is_D = dates == pd.Timestamp(D)
    idx = p.adj_close.index
    y_long = purge_labels(y_long.reindex(feats.index), feats, idx, close_D, m["label_horizon_days"])
    y_short = purge_labels(y_short.reindex(feats.index), feats, idx, close_D, m["conformal_horizon_days"])
    current = feats[is_D]
    rr = fit_predict(feats[~is_D], y_long[~is_D], y_short[~is_D], current, cfg)
    snap = current.droplevel("date")
    scores, negative_events, news_admitted = apply_news(rr.scores, news_ctx)

    vols = universe_vols(p, tickers, cfg)
    breaks = break_table(p, tickers, pd.Timestamp(close_D), cfg)
    vix = None
    if macro is not None and len(macro):
        mv = macro[(macro["series"] == "vix") & (macro["available_from"] <= pd.Timestamp(close_D))]
        if len(mv):
            vix = mv.set_index("obs_date")["value"]
    reg = regime_model(p.adj_close[bench] if bench in p.adj_close else p.adj_close.mean(axis=1), vix, cfg)
    gaps = data_gaps(p, close_D)
    liq_floor = cfg["checks"]["liquidity"]["min_median_traded_value_cad"]
    rt = pd.DataFrame(index=snap.index).join(vols).join(breaks)
    rt["halfwidth"] = rr.conformal["halfwidth"]
    rt["m1_pct"] = rr.scores["m1_pct"]
    rt["m2_pct"] = rr.scores["m2_pct"]
    rt["liquid"] = (snap["adv_60d_cad"].fillna(0) >= liq_floor) & (snap["price"].fillna(0) >= cfg["checks"]["min_price"])
    rt["gap_age"] = pd.Series(gaps).reindex(rt.index).astype(float)
    dims = dimension_labels(rt, reg, rr.model_agreement, cfg)
    comp, overrides = apply_labels(dims, rt, reg, cfg)

    if fail_at == "callouts":
        raise StepFailure("injected failure at step 6 (test)")

    accounts = enabled_accounts(cfg)
    bud = budgets(cfg, portfolio.trades, D, accounts, low_confidence=low_conf)
    cov = lw_cov(p.adj_close[tickers].pct_change(fill_method=None))
    review_n = int(cfg["settings"]["callouts"]["holding_review_weeks"]) - 1
    inp = WeeklyInputs(
        decision_date=D, cfg=cfg, features=snap, scores=scores, dims=dims, composite=comp, risk_table=rt,
        conformal=rr.conformal, shap_top=rr.shap_top, regime=reg, portfolio=portfolio, budgets=bud,
        accounts=accounts, cov=cov, sectors=cfg["sectors"], first_buy_pct=history.first_buy_pct(portfolio),
        reviewed_recently=history.reviewed_recently(D, review_n), prev_labels=history.prev_labels(D),
        superficial=superficial_blocks(portfolio, D, cfg["settings"]["superficial_loss"]["window_days"]),
        negative_events=negative_events, negative_admitted=news_admitted)
    callouts, meta = Builder(inp).build()

    out_scores = scores.join(dims).assign(composite=comp)
    out_scores = out_scores.join(rt[["garch_sigma", "har_sigma", "halfwidth", "break_age_days", "gap_age", "liquid"]])
    return {
        "D": D, "close_D": close_D, "snapshot": snap, "scores": out_scores, "rr": rr, "regime": reg,
        "overrides": overrides, "budgets": bud, "callouts": callouts, "meta": meta, "feats": feats,
        "y_long": y_long, "panel": p, "runtime_s": time.time() - t0, "tickers": tickers,
        "macro": macro_as_of(macro, close_D) if macro is not None else {}, "gaps": gaps, "vols": vols,
        "news_admitted": news_admitted,
    }

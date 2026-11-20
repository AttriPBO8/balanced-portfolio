"""Backtest replay engine (§13.1). NOT BUILT YET (TODO P4).

Plan: for the historical period, generate weekly artifacts with the exact production code path
(pipeline.run_weekly.compute_week on panel.upto(D)) into a scratch root, then replay the frozen
callouts.json files week by week: apply ACTION callouts under the same monthly caps, cash and
whole-share sizing, charge fees.per_trade_cad and half-spread, apply the delisting-log terminal
haircut (§6.5), and report net returns vs XIC.TO, XIU.TO and the naive baseline (§13.5).
For live periods it must read the committed artifacts and never recompute a past decision.
"""


def replay(*_args, **_kwargs):
    raise NotImplementedError("backtest replay is phase P4; see module docstring")

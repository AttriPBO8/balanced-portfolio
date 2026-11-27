"""Static dashboard generator (§14). Reads committed artifacts only; writes one HTML page.

    python site/build.py --root . --out _site

Every number on the page comes from an artifact and shows its run_id / model_card_hash.
Charts are inline SVG with a hover tooltip and a table view; no external libraries.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import load_config  # noqa: E402
from common.dates import utc_now  # noqa: E402
from pipeline import state as st  # noqa: E402
from callouts import explain as ex  # noqa: E402
from common.tickers import is_us_listed  # noqa: E402

NOTICES = [
    "Recommendations only. Not investment advice. The owner places all orders.",
    "Not a trading portal: up to 10 trade suggestions a week, within monthly trade limits.",
    "Backtested figures carry survivorship bias: the universe is today's list plus a manual delisting log.",
    "Positions shown here are public by owner choice.",
]

CSS = """
:root{--bg:#f4f5f7;--fg:#16181d;--muted:#667085;--card:#fff;--line:#e4e7ec;--soft:#f9fafb;
--low:#067647;--low-bg:#dcfae6;--mod:#93370d;--mod-bg:#fef0c7;--high:#b42318;--high-bg:#fee4e2;--accent:#2e5bd8;--accent-bg:#eaf0ff;
--banner:#fffaeb;--banner-line:#fedf89;--buy:#067647;--sell:#b42318;--hero1:#1d2b53;--hero2:#2e5bd8;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--grid:#eaecf0;--shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#eceef2;--muted:#98a2b3;--card:#181b21;--line:#2a2f37;--soft:#1e2229;
--low:#75e0a7;--low-bg:#0b3a24;--mod:#fec84b;--mod-bg:#4a2a07;--high:#fda29b;--high-bg:#55160c;--accent:#84a9ff;--accent-bg:#1c2a4d;
--banner:#2b2410;--banner-line:#5c4813;--buy:#75e0a7;--sell:#fda29b;--hero1:#121a33;--hero2:#1f3d99;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--grid:#252a32;--shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 Inter,system-ui,-apple-system,Segoe UI,sans-serif}
.hero{background:linear-gradient(135deg,var(--hero1),var(--hero2));color:#fff;padding:22px 0 64px}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px}
.hero h1{font-size:24px;margin:0 0 2px;letter-spacing:-.01em}.hero .sub{opacity:.8;font-size:13px}

.topbar{position:sticky;top:0;z-index:20;background:var(--hero1);box-shadow:0 1px 0 rgba(255,255,255,.08),0 2px 8px rgba(0,0,0,.15)}
nav{display:flex;gap:6px;padding:8px 0;overflow-x:auto;scrollbar-width:none}nav::-webkit-scrollbar{display:none}
nav a{color:#fff;text-decoration:none;font-size:13px;padding:5px 11px;border-radius:999px;background:rgba(255,255,255,.12);white-space:nowrap}
nav a:hover{background:rgba(255,255,255,.24)}
h2[id]{scroll-margin-top:56px}#hood{scroll-margin-top:56px}
h3.grp{display:flex;align-items:center;gap:8px;margin:18px 0 6px}.pill{font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;background:var(--line,#ddd)}
details.fold{margin:14px 0}details.fold>summary{cursor:pointer;font-weight:600;padding:6px 0}
details.hood{margin:28px 0 8px;border-top:1px solid var(--line,#ddd);padding-top:6px}
details.hood>summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;padding:12px 0}
details.hood>summary::-webkit-details-marker{display:none}
details.hood>summary>span:first-child{font-size:20px;font-weight:700}
details.hood>summary>span:first-child::before{content:'\\25B8  ';}details.hood[open]>summary>span:first-child::before{content:'\\25BE  '}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:-44px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow)}
.kpi .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.kpi .v{font-size:24px;font-weight:700;margin-top:2px;letter-spacing:-.01em}
.kpi .d{font-size:12px;color:var(--muted)}.up{color:var(--buy)}.down{color:var(--sell)}
main{padding-bottom:40px}h2{font-size:19px;margin:34px 0 12px;letter-spacing:-.01em}h3{font-size:15px;margin:22px 0 8px}
.notices{font-size:12px;color:var(--muted);margin:14px 0 0}
.banner{background:var(--banner);border:1px solid var(--banner-line);padding:10px 14px;border-radius:10px;margin:10px 0;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:10px 0;box-shadow:var(--shadow);overflow-x:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:14px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
tr.click{cursor:pointer}tr.click:hover td{background:var(--soft)}
.tag{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;font-weight:600;margin:1px 2px 1px 0;white-space:nowrap}
.Low{color:var(--low);background:var(--low-bg)}.Moderate{color:var(--mod);background:var(--mod-bg)}.High{color:var(--high);background:var(--high-bg)}
.pass{color:var(--low)}.warn{color:var(--mod)}.fail{color:var(--high)}.na{color:var(--muted)}
details summary{cursor:pointer}.muted{color:var(--muted);font-size:13px}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;margin:4px 0}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;display:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.bars{display:flex;gap:3px;align-items:flex-end;height:48px}.bars span{flex:1;background:var(--s1);border-radius:3px 3px 0 0;min-height:1px}
.btn{display:inline-block;background:var(--accent);color:#fff;padding:9px 15px;border-radius:10px;text-decoration:none;font-weight:600;font-size:14px;margin:4px 10px 4px 0;border:0;cursor:pointer}
.btn.alt{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr));gap:12px}
.co{all:unset;box-sizing:border-box;display:block;cursor:pointer;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow);transition:transform .12s,box-shadow .12s;position:relative;overflow:hidden}
.co:hover,.co:focus-visible{transform:translateY(-2px);box-shadow:0 6px 18px rgba(16,24,40,.12);outline:none}
.co:focus-visible{box-shadow:0 0 0 3px var(--accent-bg),0 0 0 4px var(--accent)}
.co::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--line)}
.co.BUY::before{background:var(--buy)}.co.SELL::before{background:var(--sell)}.co.IDEA::before{background:var(--accent)}
.co .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.co .tk{font-size:18px;font-weight:700}
.pill{font-size:12px;font-weight:700;padding:2px 9px;border-radius:999px;background:var(--soft);border:1px solid var(--line)}
.pill.BUY{color:var(--buy);background:var(--low-bg);border-color:transparent}.pill.SELL{color:var(--sell);background:var(--high-bg);border-color:transparent}
.co .what{margin:8px 0 6px;font-weight:600}.co .meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--muted)}
.co .more{margin-top:8px;font-size:13px;color:var(--accent);font-weight:600}
.meter{height:6px;border-radius:99px;background:var(--grid);overflow:hidden;margin-top:8px}.meter span{display:block;height:100%;background:var(--accent);border-radius:99px}
.split{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,250px),1fr));gap:10px;align-items:start}
.donut{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.donut svg{width:150px;height:150px;flex:none}
.lg{font-size:13px;flex:1;min-width:150px}.lg div{display:flex;justify-content:space-between;gap:10px;padding:2px 0;border-bottom:1px dashed var(--line)}
.lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.stack{display:flex;height:16px;border-radius:99px;overflow:hidden;margin:6px 0}.stack span{display:block;height:100%}
.modal{position:fixed;inset:0;background:rgba(10,14,25,.55);display:none;align-items:flex-start;justify-content:center;z-index:50;padding:4vh 12px;overflow:auto}
.modal.open{display:flex}.dlg{background:var(--card);color:var(--fg);border-radius:18px;max-width:860px;width:100%;box-shadow:0 20px 50px rgba(0,0,0,.35);position:relative}
.dlg header{padding:18px 20px 0}.dlg .x{position:absolute;right:12px;top:10px;font-size:26px;line-height:1;background:transparent;border:0;color:var(--muted);cursor:pointer;padding:6px 10px;border-radius:8px}
.dlg .x:hover{background:var(--soft)}.dlg .body{padding:6px 20px 20px}
.tabs{display:flex;gap:6px;margin:12px 0 4px;border-bottom:1px solid var(--line)}.tabs button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;font:inherit;font-weight:600;color:var(--muted);cursor:pointer}
.tabs button[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent)}
.pane{display:none}.pane.on{display:block}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,370px),1fr));gap:12px;margin-top:12px}
.cbox{border:1px solid var(--line);border-radius:12px;padding:10px 12px}.cbox h4{margin:0 0 6px;font-size:13px;color:var(--muted);font-weight:600}
.cbox svg{width:100%;height:auto;display:block}table.fin th{width:38%;text-transform:none;font-size:13px;letter-spacing:0}
@media (max-width:640px){td,th{padding:6px 4px}.hide-sm{display:none}.kpi .v{font-size:20px}}
"""


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def tag(label) -> str:
    return f'<span class="tag {e(label)}">{e(label)}</span>'


def fmt_pct(x, d=0):
    return "n/a" if x is None else f"{x:.{d}%}"


def signed(x, spec: str = ".3f") -> str:
    return "n/a" if x is None else format(x, "+" + spec)


SERIES_LABELS = {"callouts": "Callouts", "baseline": "Naive baseline", "XIC.TO": "XIC (TSX Composite)", "XIU.TO": "XIU (TSX 60)"}


def line_chart(rows: list[dict], keys: list[str], cid: str, title: str) -> str:
    """Inline SVG line chart: 2px lines, hairline grid, legend + end labels, hover tooltip,
    and a table view. Series colours follow the entity (fixed slot per key)."""
    keys = [k for k in keys if any(r.get(k) is not None for r in rows)]
    if len(rows) < 2 or not keys:
        return ""
    W, H, L, R, T, B = 760, 260, 56, 120, 12, 28
    vals = [r[k] for r in rows for k in keys if r.get(k) is not None and r[k] == r[k]]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    x = lambda i: L + (W - L - R) * i / (len(rows) - 1)
    y = lambda v: T + (H - T - B) * (1 - (v - lo) / (hi - lo))
    out = [f"<div class='chart' id='{cid}'><div class='legend'>"]
    out += [f"<span><i style='background:var(--s{i + 1})'></i>{e(SERIES_LABELS.get(k, k))}</span>" for i, k in enumerate(keys)]
    out.append(f"</div><svg viewBox='0 0 {W} {H}' role='img' aria-label='{e(title)}'>")
    for j in range(5):
        v = lo + (hi - lo) * j / 4
        out.append(f"<line x1='{L}' x2='{W - R}' y1='{y(v):.1f}' y2='{y(v):.1f}' stroke='var(--grid)' stroke-width='1'/>"
                   f"<text x='{L - 6}' y='{y(v) + 4:.1f}' text-anchor='end' font-size='11' fill='var(--muted)'>${v:,.0f}</text>")
    for idx in sorted({0, len(rows) // 2, len(rows) - 1}):
        out.append(f"<text x='{x(idx):.1f}' y='{H - 8}' text-anchor='middle' font-size='11' fill='var(--muted)'>{e(rows[idx]['date'])}</text>")
    ends = []
    for i, k in enumerate(keys):
        pts = " ".join(f"{x(j):.1f},{y(r[k]):.1f}" for j, r in enumerate(rows) if r.get(k) is not None and r[k] == r[k])
        out.append(f"<polyline points='{pts}' fill='none' stroke='var(--s{i + 1})' stroke-width='2' stroke-linejoin='round' stroke-linecap='round'/>")
        last = rows[-1].get(k)
        if last is not None and last == last:
            ends.append([y(last), i, k, last])
    ends.sort()
    for n in range(1, len(ends)):   # nudge end labels apart
        if ends[n][0] - ends[n - 1][0] < 13:
            ends[n][0] = ends[n - 1][0] + 13
    for yy, i, k, last in ends:
        out.append(f"<circle cx='{x(len(rows) - 1):.1f}' cy='{y(last):.1f}' r='4' fill='var(--s{i + 1})' stroke='var(--card)' stroke-width='2'/>"
                   f"<text x='{W - R + 8}' y='{yy + 4:.1f}' font-size='11' fill='var(--fg)'>{e(SERIES_LABELS.get(k, k).split(' ')[0])} ${last:,.0f}</text>")
    out.append(f"<line class='xh' x1='0' x2='0' y1='{T}' y2='{H - B}' stroke='var(--muted)' stroke-width='1' visibility='hidden'/>"
               f"<rect x='{L}' y='{T}' width='{W - L - R}' height='{H - T - B}' fill='transparent' class='hit'/></svg><div class='tip'></div>")
    data = [{"d": r["date"], **{k: (round(r[k], 2) if r.get(k) is not None and r[k] == r[k] else None) for k in keys}} for r in rows]
    out.append("<details><summary class='muted'>Table view</summary><table><tr><th>Date</th>"
               + "".join(f"<th>{e(SERIES_LABELS.get(k, k))}</th>" for k in keys) + "</tr>"
               + "".join("<tr><td>" + e(d["d"]) + "</td>" + "".join(f"<td>{'' if d[k] is None else f'${d[k]:,.0f}'}</td>" for k in keys) + "</tr>"
                         for d in data[::max(1, len(data) // 60)]) + "</table></details>")
    labels = {k: SERIES_LABELS.get(k, k) for k in keys}
    out.append(f"<script>(function(){{var c=document.getElementById('{cid}'),d={json.dumps(data)},k={json.dumps(keys)},lb={json.dumps(labels)};"
               f"var svg=c.querySelector('svg'),h=svg.querySelector('.hit'),xh=svg.querySelector('.xh'),tip=c.querySelector('.tip');"
               f"function mv(ev){{var b=svg.getBoundingClientRect(),sx={W}/b.width,px=(ev.clientX-b.left)*sx;"
               f"var i=Math.round((px-{L})/({W - L - R})*(d.length-1));i=Math.max(0,Math.min(d.length-1,i));"
               f"var xx={L}+({W - L - R})*i/(d.length-1);xh.setAttribute('x1',xx);xh.setAttribute('x2',xx);xh.setAttribute('visibility','visible');"
               f"var s='<b>'+d[i].d+'</b>';k.forEach(function(q,j){{if(d[i][q]!==null)s+='<br><i style=\"display:inline-block;width:10px;height:3px;background:var(--s'+(j+1)+')\"></i> '+lb[q]+': $'+Math.round(d[i][q]).toLocaleString();}});"
               f"tip.innerHTML=s;tip.style.display='block';var cb=c.getBoundingClientRect();var tx=ev.clientX-cb.left+12;if(tx>cb.width-180)tx-=200;tip.style.left=tx+'px';tip.style.top=(ev.clientY-cb.top+10)+'px';}}"
               f"h.addEventListener('mousemove',mv);h.addEventListener('mouseleave',function(){{tip.style.display='none';xh.setAttribute('visibility','hidden');}});}})();</script></div>")
    return "".join(out)


def news_gate_text(g: dict | None) -> str:
    if not g:
        return ""
    imp = g.get("mean_ic_improvement_5d")
    return (f"<br><b>Trial gate</b>: {e(g['status'])} · {g['shadow_weeks']} of {g['required_weeks']} weeks logged · "
            f"IC change {'n/a' if imp is None else format(imp, '+.3f')}")


def budget_bars(hist: list[dict]) -> str:
    if not hist:
        return ""
    cap = max([h["cap"] for h in hist] + [1])
    bars = "".join(f"<span title='{e(h['month'])}: {h['used']} of {h['cap']}' style='height:{max(h['used'] / cap * 100, 2):.0f}%'></span>" for h in hist)
    return (f"<div class='muted' style='margin-top:6px'>Transactions per month, last 12 (cap {cap})</div><div class='bars' role='img' "
            f"aria-label='transactions per month'>{bars}</div><div class='muted'>" + ", ".join(f"{h['month'][2:]}:{h['used']}" for h in hist) + "</div>")


def perf_table(series: dict) -> str:
    keys = [k for k in ("callouts", "baseline", "XIC.TO", "XIU.TO") if k in series]
    rows = "".join(f"<tr><td>{e(SERIES_LABELS.get(k, k))}</td><td>{fmt_pct(series[k].get('total_return'), 1)}</td>"
                   f"<td>{fmt_pct(series[k].get('cagr'), 1)}</td><td>{fmt_pct(series[k].get('vol'), 1)}</td>"
                   f"<td>{'n/a' if series[k].get('sharpe') is None else format(series[k]['sharpe'], '.2f')}</td>"
                   f"<td>{fmt_pct(series[k].get('max_drawdown'), 1)}</td></tr>" for k in keys)
    return ("<table><tr><th>Net of costs</th><th>Total</th><th>CAGR</th><th>Vol</th><th>Sharpe</th><th>Max DD</th></tr>"
            + rows + "</table>")


SIDE = {"ACTION_BUY_NEW": "BUY", "ACTION_ADD": "BUY", "ACTION_TRIM": "SELL", "ACTION_EXIT": "SELL"}


def confirm_url(repo: str, cal: dict, fee: float) -> str:
    """A link that opens a new GitHub issue pre-filled with this week's trades (see pipeline/confirm.py)."""
    lines = [f"{SIDE[c['class']]} {c['account']} {c['ticker']} {c['shares']} @ {c['est_price_cad']:.2f} fee {fee:.2f}"
             for c in cal["callouts"] if c["class"] in SIDE and c.get("account") and c.get("shares")]
    body = ("Delete the lines for trades you did NOT make. Change shares, price and fee to match your broker's confirmation.\n"
            "Add `on YYYY-MM-DD` to a line if you traded on an earlier day (otherwise today is used).\n"
            "You can add other trades in the same format. Then press Submit: the files are updated within a few minutes.\n\n"
            + ("\n".join(lines) if lines else "BUY TFSA TICKER.TO 10 @ 50.00 fee 9.95") + "\n")
    q = urllib.parse.urlencode({"title": f"Confirm trades {cal['decision_date']}", "body": body})
    return f"https://github.com/{repo}/issues/new?{q}"


PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#8e5cd9", "#e0407b", "#14a3b8", "#7a8b25", "#b0653a", "#5b6b8c",
           "#c94f4f", "#3f9b5a"]
KIND = {"ACTION_BUY_NEW": "BUY", "ACTION_ADD": "BUY", "ACTION_TRIM": "SELL", "ACTION_EXIT": "SELL"}


class Items:
    """Everything that opens a pop-up: id -> the fields the charts need plus both explanations."""

    def __init__(self, ctx: dict):
        self.ctx, self.data = ctx, {}

    def add(self, c: dict) -> str:
        k = f"i{len(self.data)}"
        keep = ("ticker", "class", "risk_label", "risk_breakdown", "score_pct", "m1_pct", "m2_pct", "shap_top_features",
                "conformal_interval_80", "est_price_cad", "shares", "account", "projected_value_cad", "projected_return_3m",
                "price_cad", "target_price_1w", "range_80_1w", "expected_return_1w", "sector")
        d = {f: c.get(f) for f in keep if c.get(f) is not None}
        d["label"] = ex.CLASS_LABEL.get(c["class"], c["class"])
        d["layman"], d["financial"] = ex.layman(c, self.ctx), ex.financial(c, self.ctx)
        self.data[k] = d
        return k


def _arc(cx, cy, r0, r1, a0, a1) -> str:
    import math
    if a1 - a0 >= 2 * math.pi - 1e-9:
        a1 = a0 + 2 * math.pi - 1e-4
    p = lambda r, a: (cx + r * math.sin(a), cy - r * math.cos(a))
    big = 1 if a1 - a0 > math.pi else 0
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p(r1, a0), p(r1, a1), p(r0, a1), p(r0, a0)
    return (f"M{x0:.2f},{y0:.2f}A{r1},{r1} 0 {big} 1 {x1:.2f},{y1:.2f}L{x2:.2f},{y2:.2f}"
            f"A{r0},{r0} 0 {big} 0 {x3:.2f},{y3:.2f}Z")


def donut(parts: list[tuple[str, float]], title: str, center: str, colors: dict | None = None, top: int = 8) -> str:
    import math
    parts = sorted([(k, v) for k, v in parts if v and v > 0], key=lambda x: -x[1])
    if len(parts) > top:
        parts = parts[:top - 1] + [("Other", sum(v for _, v in parts[top - 1:]))]
    tot = sum(v for _, v in parts) or 1
    a, segs, leg = 0.0, [], []
    for i, (k, v) in enumerate(parts):
        col = (colors or {}).get(k, PALETTE[i % len(PALETTE)])
        a1 = a + 2 * math.pi * v / tot
        segs.append(f"<path d='{_arc(75, 75, 46, 70, a, a1)}' fill='{col}' stroke='var(--card)' stroke-width='1.5'>"
                    f"<title>{e(k)}: ${v:,.0f} ({v / tot:.0%})</title></path>")
        leg.append(f"<div><span><i style='background:{col}'></i>{e(k)}</span><span>${v:,.0f} · {v / tot:.0%}</span></div>")
        a = a1
    return (f"<div><div class='muted' style='font-weight:600'>{e(title)}</div><div class='donut'>"
            f"<svg viewBox='0 0 150 150' role='img' aria-label='{e(title)}'>{''.join(segs)}"
            f"<text x='75' y='72' text-anchor='middle' font-size='15' font-weight='700' fill='var(--fg)'>{e(center)}</text>"
            f"<text x='75' y='90' text-anchor='middle' font-size='10' fill='var(--muted)'>market value</text></svg>"
            f"<div class='lg'>{''.join(leg)}</div></div></div>")


def portfolio_section(cal: dict) -> str:
    rows = [r for r in cal.get("portfolio") or [] if r.get("value_cad")]
    if not rows:
        return "<p class='muted'>No holdings with prices yet.</p>"
    sectors = sorted({r["sector"] for r in rows})
    scol = {k: PALETTE[i % len(PALETTE)] for i, k in enumerate(sectors)}
    cash = cal.get("cash_cad") or {}
    out = ["<div class='grid2'>"]
    for acct in sorted({r["account"] for r in rows}):
        rs = [r for r in rows if r["account"] == acct]
        val = sum(r["value_cad"] for r in rs)
        cost = sum(r["acb_cad"] for r in rs)
        gain = val - cost
        by_sector: dict[str, float] = {}
        for r in rs:
            by_sector[r["sector"]] = by_sector.get(r["sector"], 0) + r["value_cad"]
        out.append(f"<div class='card'><div style='display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px'>"
                   f"<b style='font-size:16px'>{e(acct)}</b><span class='muted'>{len(rs)} holdings · value ${val:,.0f} · "
                   f"cost ${cost:,.0f} · <span class='{'up' if gain >= 0 else 'down'}'>{'+' if gain >= 0 else '−'}${abs(gain):,.0f} "
                   f"({gain / cost:+.1%})</span> · cash ${cash.get(acct, 0):,.0f}</span></div>"
                   f"<div class='split' style='margin-top:10px'>"
                   + donut([(r["ticker"], r["value_cad"]) for r in rs], "By stock", f"${val / 1000:,.1f}k")
                   + donut(list(by_sector.items()), "By sector", f"${val / 1000:,.1f}k", scol) + "</div></div>")
    out.append("</div>")
    # both accounts: sector bar + holdings table
    tot = sum(r["value_cad"] for r in rows)
    by_sector = {}
    for r in rows:
        by_sector[r["sector"]] = by_sector.get(r["sector"], 0) + r["value_cad"]
    bar = "".join(f"<span title='{e(k)}: ${v:,.0f} ({v / tot:.0%})' style='width:{v / tot * 100:.2f}%;background:{scol[k]}'></span>"
                  for k, v in sorted(by_sector.items(), key=lambda x: -x[1]))
    leg = " ".join(f"<span class='muted' style='margin-right:10px'><i style='display:inline-block;width:10px;height:10px;border-radius:3px;"
                   f"background:{scol[k]};margin-right:4px'></i>{e(k)} {v / tot:.0%}</span>"
                   for k, v in sorted(by_sector.items(), key=lambda x: -x[1]))
    trs = "".join(f"<tr><td><b>{e(r['ticker'])}</b></td><td>{e(r['account'])}</td><td class='hide-sm'>{e(r['sector'])}</td>"
                  f"<td>{r['quantity']:g}</td><td>${r['price_cad']:,.2f}</td><td>${r['value_cad']:,.0f}</td>"
                  f"<td class='hide-sm'>${r['acb_cad']:,.0f}</td><td class='hide-sm {'up' if r['value_cad'] >= r['acb_cad'] else 'down'}'>"
                  f"{(r['value_cad'] - r['acb_cad']) / r['acb_cad']:+.1%}</td><td>{r['value_cad'] / tot:.1%}</td></tr>"
                  for r in sorted(rows, key=lambda r: -r["value_cad"]) if r["acb_cad"])
    out.append(f"<div class='card'><b>Both accounts by sector</b> <span class='muted'>· ${tot:,.0f} invested at market</span>"
               f"<div class='stack' role='img' aria-label='sector split'>{bar}</div><div>{leg}</div>"
               "<details style='margin-top:8px'><summary class='muted'>All holdings</summary><table><tr><th>Ticker</th><th>Account</th>"
               "<th class='hide-sm'>Sector</th><th>Shares</th><th>Price</th><th>Value</th><th class='hide-sm'>Cost</th><th class='hide-sm'>Gain</th>"
               f"<th>Weight</th></tr>{trs}</table></details></div>")
    return "".join(out)


def sugg_card(c: dict, items: Items) -> str:
    k = items.add(c)
    kind = KIND.get(c["class"], "")
    sh, px = c.get("shares") or 0, c.get("est_price_cad") or 0
    if kind == "BUY":
        what = f"Buy {sh} shares in {e(c['account'])} · ≈ ${sh * px:,.0f}"
    elif kind == "SELL":
        what = f"Sell {sh} shares in {e(c['account'])} · ≈ ${sh * px:,.0f}"
    else:
        what = e(ex.REASON_PLAIN.get(c.get("demoted_reason") or "", "Good trade idea, not for this week"))
        what = "Not this week: " + what
    pv = c.get("projected_value_cad")
    s = c.get("score_pct") or 0
    return (f"<button class='co {kind}' data-item='{k}' aria-haspopup='dialog'><div class='top'><span class='tk'>#{c['rank']} {e(c['ticker'])}</span>"
            f"<span class='pill {kind}'>{e(ex.CLASS_LABEL.get(c['class'], c['class']))}</span></div>"
            f"<div class='what'>{what}</div><div class='meta'><span>Risk {tag(c['risk_label'])}</span>"
            f"<span>Rating {s:.0%}</span>"
            + (f"<span>Value {'+' if pv >= 0 else '−'}${abs(pv):,.0f}</span>" if pv is not None else "")
            + (f"<span>{e(c.get('sector'))}</span>" if c.get("sector") else "")
            + ("<span>US · RRSP only</span>" if is_us_listed(c["ticker"]) else "")
            + f"</div><div class='meter' title='rating percentile'><span style='width:{s * 100:.0f}%'></span></div>"
            "<div class='more'>Why? Tap for the explanation →</div></button>")


def mini_table(items_: list[dict], items: Items) -> str:
    rows = "".join(f"<tr class='click' data-item='{items.add(c)}' tabindex='0'><td><b>{e(c['ticker'])}</b></td>"
                   f"<td>{e(ex.CLASS_LABEL.get(c['class'], c['class']))}</td><td>{tag(c['risk_label'])}</td>"
                   f"<td>{fmt_pct(c['score_pct'])}</td><td class='hide-sm muted'>{e(c['rationale'])}</td>"
                   f"<td style='color:var(--accent);font-weight:600'>Why →</td></tr>" for c in items_)
    return ("<div class='card'><table><tr><th>Ticker</th><th>Status</th><th>Risk</th><th>Rating</th><th class='hide-sm'>Note</th><th></th></tr>"
            + rows + "</table></div>")


def high_risk_table(ideas: list[dict], items: Items) -> str:
    rows = "".join(
        f"<tr class='click' data-item='{items.add(dict(x, **{'class': 'IDEA'}))}' tabindex='0'><td>{x['rank']}</td>"
        f"<td><b>{e(x['ticker'])}</b>{' <span class=muted>(held)</span>' if x.get('held') else ''}</td>"
        f"<td>${x['price_cad']:,.2f}</td><td><b>${x['target_price_1w']:,.2f}</b></td>"
        f"<td class='hide-sm'>${x['range_80_1w'][0]:,.2f} to ${x['range_80_1w'][1]:,.2f}</td>"
        f"<td>{signed(x['expected_return_1w'], '.1%')}</td><td class='hide-sm'>{fmt_pct(x['score_pct'])}</td>"
        f"<td>{tag(x['risk_label'])}</td><td style='color:var(--accent);font-weight:600'>Why →</td></tr>"
        for x in ideas)
    return ("<div class='card'><table><tr><th>#</th><th>Ticker</th><th>Price</th><th>Target, 1 week</th><th class='hide-sm'>80% range</th>"
            "<th>Expected</th><th class='hide-sm'>Rating</th><th>Risk</th><th></th></tr>" + rows + "</table></div>")


MODAL_JS = r"""
(function(){function openHood(){var h=location.hash;if(!h)return;var t=document.getElementById(h.slice(1));
var d=t&&t.closest?t.closest('details'):null;if(t&&t.tagName==='DETAILS')d=t;if(d&&!d.open){d.open=true;t.scrollIntoView();}}
window.addEventListener('hashchange',openHood);document.addEventListener('DOMContentLoaded',openHood);})();

(function(){
var D=JSON.parse(document.getElementById('popup-data').textContent),PH=D.ph||{dates:[],series:{}};
var m=document.getElementById('pop'),dlg=m.querySelector('.dlg'),last=null;
var DIM={R1:'Volatility',R2:'Vol trend',R3:'Forecast uncertainty',R4:'Market regime',R5:'Structural break',R6:'Model agreement',R7:'Liquidity/data'};
var FN=D.fn||{};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function money(v){return '$'+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});}
function priceChart(it){
  var s=PH.series[it.ticker];if(!s)return '<p class="muted">No price history saved for this week.</p>';
  var pts=[];for(var i=0;i<s.length;i++)if(s[i]!=null)pts.push([i,s[i]]);if(pts.length<2)return '';
  var px=it.est_price_cad||it.price_cad||pts[pts.length-1][1],band=null,tgt=null;
  if(it.range_80_1w){band=[it.range_80_1w[0],it.range_80_1w[1]];tgt=it.target_price_1w;}
  else if(it.conformal_interval_80){band=[px*(1+it.conformal_interval_80[0]),px*(1+it.conformal_interval_80[1])];}
  var vals=pts.map(function(p){return p[1];});if(band)vals=vals.concat(band);
  var lo=Math.min.apply(null,vals),hi=Math.max.apply(null,vals),pad=(hi-lo)*.08||1;lo-=pad;hi+=pad;
  var W=380,H=190,L=46,R=band?54:12,T=8,B=22,n=s.length;
  var x=function(i){return L+(W-L-R)*i/(n-1);},y=function(v){return T+(H-T-B)*(1-(v-lo)/(hi-lo));};
  var g='';for(var j=0;j<4;j++){var v=lo+(hi-lo)*j/3;g+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v)+'" y2="'+y(v)+'" stroke="var(--grid)"/><text x="'+(L-5)+'" y="'+(y(v)+4)+'" font-size="10" text-anchor="end" fill="var(--muted)">$'+v.toFixed(v<20?2:0)+'</text>';}
  var d='M'+pts.map(function(p){return x(p[0]).toFixed(1)+','+y(p[1]).toFixed(1);}).join('L');
  var area=d+'L'+x(pts[pts.length-1][0]).toFixed(1)+','+(H-B)+'L'+x(pts[0][0]).toFixed(1)+','+(H-B)+'Z';
  var up=pts[pts.length-1][1]>=pts[0][1],col=up?'var(--buy)':'var(--sell)';
  var out='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="price chart">'+g+'<path d="'+area+'" fill="'+col+'" opacity=".10"/><path d="'+d+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linejoin="round"/>';
  var lx=x(n-1),ly=y(pts[pts.length-1][1]);out+='<circle cx="'+lx+'" cy="'+ly+'" r="4" fill="'+col+'" stroke="var(--card)" stroke-width="2"/>';
  if(band){var bx=W-R+8;out+='<rect x="'+bx+'" y="'+y(band[1])+'" width="18" height="'+Math.max(2,y(band[0])-y(band[1]))+'" rx="4" fill="var(--accent)" opacity=".25"/>'+
    '<line x1="'+lx+'" x2="'+bx+'" y1="'+ly+'" y2="'+y(tgt||px)+'" stroke="var(--accent)" stroke-dasharray="3 3"/>'+
    '<circle cx="'+(bx+9)+'" cy="'+y(tgt||px)+'" r="3.5" fill="var(--accent)"/><text x="'+(bx+9)+'" y="'+(H-6)+'" font-size="10" text-anchor="middle" fill="var(--muted)">next wk</text>';}
  var dd=PH.dates;out+='<text x="'+L+'" y="'+(H-6)+'" font-size="10" fill="var(--muted)">'+esc(dd[0]||'')+'</text><text x="'+(W-R)+'" y="'+(H-6)+'" font-size="10" text-anchor="end" fill="var(--muted)">'+esc(dd[dd.length-1]||'')+'</text></svg>';
  var chg=(pts[pts.length-1][1]/pts[0][1]-1)*100;
  return out+'<div class="muted">6 months: '+(chg>=0?'+':'')+chg.toFixed(1)+'% · last '+money(pts[pts.length-1][1])+(band?' · shaded: likely range next week, '+money(band[0])+' to '+money(band[1]):'')+'</div>';
}
function riskChart(it){
  var rb=it.risk_breakdown||{},lv={Low:1,Moderate:2,High:3},o='<svg viewBox="0 0 380 '+(7*24+4)+'" role="img" aria-label="risk by dimension">',i=0;
  Object.keys(DIM).forEach(function(k){var v=rb[k]||'Moderate',w=lv[v]*80,yy=i*24+2;
    o+='<text x="0" y="'+(yy+15)+'" font-size="11" fill="var(--fg)">'+esc(DIM[k])+'</text><rect x="130" y="'+(yy+4)+'" width="240" height="14" rx="7" fill="var(--grid)"/>'+
       '<rect x="130" y="'+(yy+4)+'" width="'+w+'" height="14" rx="7" fill="var(--'+(v==='Low'?'low':v==='High'?'high':'mod')+')"/><text x="'+(136+w)+'" y="'+(yy+15)+'" font-size="10" fill="var(--muted)">'+esc(v)+'</text>';i++;});
  return o+'</svg><div class="muted">Overall: '+esc(it.risk_label)+'. Longer bar = riskier.</div>';
}
function driverChart(it){
  var f=it.shap_top_features||[];if(!f.length)return '<p class="muted">No driver data.</p>';
  var mx=Math.max.apply(null,f.map(function(a){return Math.abs(a.value);}))||1,o='<svg viewBox="0 0 380 '+(f.length*30+6)+'" role="img" aria-label="what moved the rating">';
  f.forEach(function(a,i){var w=Math.abs(a.value)/mx*110,yy=i*30+4,pos=a.value>0;
    o+='<text x="0" y="'+(yy+14)+'" font-size="11" fill="var(--fg)">'+esc(FN[a.feature]||a.feature)+'</text><line x1="260" x2="260" y1="'+yy+'" y2="'+(yy+22)+'" stroke="var(--line)"/>'+
       '<rect x="'+(pos?260:260-w)+'" y="'+(yy+4)+'" width="'+w+'" height="14" rx="4" fill="var(--'+(pos?'buy':'sell')+')" opacity=".85"/>';});
  return o+'</svg><div class="muted">Green pushes the rating up, red pulls it down.</div>';
}
function scoreChart(it){
  var r=[['Blended rating',it.score_pct],['Model 1 (LightGBM)',it.m1_pct],['Model 2 (Ridge)',it.m2_pct]].filter(function(a){return a[1]!=null;});
  var o='<svg viewBox="0 0 380 '+(r.length*30+16)+'" role="img" aria-label="rating percentile">';
  r.forEach(function(a,i){var yy=i*30+4,w=a[1]*230;o+='<text x="0" y="'+(yy+14)+'" font-size="11" fill="var(--fg)">'+esc(a[0])+'</text><rect x="130" y="'+(yy+4)+'" width="230" height="14" rx="7" fill="var(--grid)"/>'+
     '<rect x="130" y="'+(yy+4)+'" width="'+w+'" height="14" rx="7" fill="var(--accent)"/><text x="'+(136+w)+'" y="'+(yy+15)+'" font-size="10" fill="var(--muted)">'+Math.round(a[1]*100)+'%</text>';});
  return o+'<line x1="245" x2="245" y1="0" y2="'+(r.length*30+4)+'" stroke="var(--muted)" stroke-dasharray="2 3"/><text x="245" y="'+(r.length*30+14)+'" font-size="9" text-anchor="middle" fill="var(--muted)">middle</text></svg><div class="muted">Percentile among all stocks scored this week (100% = best).</div>';
}
function open(id,src){var it=D.items[id];if(!it)return;last=src;
  dlg.querySelector('.ttl').innerHTML=esc(it.ticker)+' <span class="pill">'+esc(it.label)+'</span> <span class="tag '+esc(it.risk_label)+'">'+esc(it.risk_label)+' risk</span>';
  dlg.querySelector('.sub2').textContent=(it.sector||'')+(it.account?' · '+it.account:'');
  dlg.querySelector('#pane-l').innerHTML=it.layman;dlg.querySelector('#pane-f').innerHTML=it.financial;
  dlg.querySelector('.charts').innerHTML='<div class="cbox"><h4>Price, last 6 months (CAD)</h4>'+priceChart(it)+'</div><div class="cbox"><h4>Rating</h4>'+scoreChart(it)+'</div>'+
    '<div class="cbox"><h4>What moved the rating</h4>'+driverChart(it)+'</div><div class="cbox"><h4>Risk, 7 checks</h4>'+riskChart(it)+'</div>';
  tab('l');m.classList.add('open');document.body.style.overflow='hidden';dlg.querySelector('.x').focus();}
function close(){m.classList.remove('open');document.body.style.overflow='';if(last)last.focus();}
function tab(w){['l','f'].forEach(function(k){var b=dlg.querySelector('[data-tab='+k+']');b.setAttribute('aria-selected',k===w);dlg.querySelector('#pane-'+k).classList.toggle('on',k===w);});}
document.addEventListener('click',function(ev){var t=ev.target.closest('[data-item]');if(t){open(t.getAttribute('data-item'),t);return;}
  var b=ev.target.closest('[data-tab]');if(b){tab(b.getAttribute('data-tab'));return;}
  if(ev.target===m||ev.target.closest('.x'))close();});
document.addEventListener('keydown',function(ev){if(ev.key==='Escape'&&m.classList.contains('open'))close();
  if((ev.key==='Enter'||ev.key===' ')&&ev.target.matches&&ev.target.matches('tr[data-item]')){ev.preventDefault();open(ev.target.getAttribute('data-item'),ev.target);}});
})();
"""

MODAL_HTML = ("<div class='modal' id='pop' role='dialog' aria-modal='true' aria-labelledby='pop-ttl'><div class='dlg'>"
              "<button class='x' aria-label='Close'>×</button><header><div class='ttl' id='pop-ttl' style='font-size:20px;font-weight:700'></div>"
              "<div class='sub2 muted'></div><div class='tabs' role='tablist'><button data-tab='l' role='tab'>In plain words</button>"
              "<button data-tab='f' role='tab'>Financial detail</button></div></header><div class='body'>"
              "<div class='pane' id='pane-l'></div><div class='pane' id='pane-f'></div><div class='charts'></div>"
              "<p class='muted' style='margin-top:12px'>Press Esc, click outside, or × to close. Not investment advice.</p></div></div></div>")


def _money_signed(v: float) -> str:
    return f"<span class='{'up' if v >= 0 else 'down'}'>{'+' if v >= 0 else '−'}${abs(v):,.0f}</span>"


def _bar(v: float, mx: float) -> str:
    w = abs(v) / mx * 46
    side = "left:50%" if v >= 0 else "right:50%"
    return (f"<div style='position:relative;height:12px;min-width:90px'><span style='position:absolute;left:50%;top:-2px;bottom:-2px;"
            f"border-left:1px solid var(--line)'></span><span style='position:absolute;top:0;height:12px;border-radius:4px;{side};"
            f"width:{w:.1f}%;background:var(--{'buy' if v >= 0 else 'sell'});opacity:.85'></span></div>")


def house_section(hm: dict) -> str:
    rows = [r for r in hm["rows"] if r.get("house_money_cad") is not None]
    t = hm["total_cad"]
    f = hm.get("high_risk_fund_total_cad")
    mx = max([abs(r["house_money_cad"]) for r in rows] + [abs(r.get("high_risk_fund_cad") or 0) for r in rows] + [1])
    trs = []
    for r in rows:
        hrf = r.get("high_risk_fund_cad")
        trs.append(f"<tr><td><b>{e(r['ticker'])}</b> <span class='muted'>{e(r['account'])}</span></td>"
                   f"<td class='hide-sm'>${r['acb_cad']:,.0f}</td><td>${r['value_cad']:,.0f}</td><td>{_money_signed(r['gain_cad'])}</td>"
                   f"<td class='hide-sm' style='white-space:nowrap' title='{e(r.get('drop_from'))} to {e(r.get('drop_to'))}'>{r['worst_drop']:.0%}"
                   f"<span class='muted'> ({e((r.get('drop_from') or '')[:7])} → {e((r.get('drop_to') or '')[:7])})</span></td>"
                   f"<td style='white-space:nowrap'><b>{_money_signed(r['house_money_cad'])}</b>{_bar(r['house_money_cad'], mx)}</td>"
                   + (f"<td style='white-space:nowrap'><b>{_money_signed(hrf)}</b>{_bar(hrf, mx)}</td>" if hrf is not None else "<td></td>")
                   + "</tr>")
    tile = lambda name, v, sub: (f"<div class='kpi' style='box-shadow:none'><div class='k'>{name}</div>"
                                 f"<div class='v' style='font-size:28px'>{_money_signed(v)}</div><div class='d'>{sub}</div></div>")
    return ("<h2 id='house'>House money and High Risk Fund</h2><div class='card'><div class='grid'>"
            + tile("House money", t, "fall measured on today's value")
            + (tile("High Risk Fund", f, "fall measured on what you invested") if f is not None else "") + "</div>"
            "<p>Two ways to size money for the high-risk ideas list. For each holding, start from your gain over what you paid, then "
            "subtract a repeat of the stock's <b>worst fall</b>: the biggest drop inside any 3- or 6-month stretch since "
            f"{e(hm['since'])}. <b>House money</b> applies that fall to today's value; <b>High Risk Fund</b> applies it to the amount "
            "you invested. Example: paid $1,000, now $1,200, worst fall 15%: House money $200 − $180 = $20; High Risk Fund "
            "$200 − $150 = $50. Losing holdings count against both totals. Market-wide crashes are not added on top (your choice), "
            "and the 2020 crash is outside the data range. A sizing guide, not a guarantee.</p>"
            "<table><tr><th>Holding</th><th class='hide-sm'>Invested</th><th>Value</th><th>Gain</th><th class='hide-sm'>Worst fall</th>"
            "<th>House money</th><th>High Risk Fund</th></tr>" + "".join(trs) + "</table></div>")


def kpis(cal: dict) -> str:
    rows = [r for r in cal.get("portfolio") or [] if r.get("value_cad")]
    val = sum(r["value_cad"] for r in rows)
    cost = sum(r["acb_cad"] for r in rows)
    cash = sum((cal.get("cash_cad") or {}).values())
    gain = val - cost
    acts = [c for c in cal["callouts"] if c["class"] in KIND]
    nb = sum(1 for c in acts if KIND[c["class"]] == "BUY")
    reg = cal.get("regime") or {}
    tiles = [("Portfolio value", f"${val + cash:,.0f}", f"${val:,.0f} invested + ${cash:,.0f} cash"),
             ("Gain vs cost", f"<span class='{'up' if gain >= 0 else 'down'}'>{'+' if gain >= 0 else '−'}${abs(gain):,.0f}</span>",
              f"{gain / cost:+.1%} on ${cost:,.0f}" if cost else ""),
             ("This week", f"{len(acts)} trades", f"{nb} buy · {len(acts) - nb} sell"),
             ("Market", e((reg.get("label") or "n/a").capitalize()),
              f"stress probability {fmt_pct(reg.get('stressed_prob'))}" + (" · caution" if reg.get("label") == "stressed" else ""))]
    hm = cal.get("house_money")
    if hm:
        t = hm["total_cad"]
        f = hm.get("high_risk_fund_total_cad")
        tiles.insert(2, ("House money", _money_signed(t),
                         (f"High Risk Fund {_money_signed(f)} · " if f is not None else "")
                         + "<a href='#house' style='color:inherit'>details →</a>"))
    return "<div class='kpis'>" + "".join(f"<div class='kpi'><div class='k'>{k}</div><div class='v'>{v}</div><div class='d'>{d}</div></div>"
                                          for k, v, d in tiles) + "</div>"


def build(root: Path, out: Path, now: dt.datetime | None = None) -> Path:
    cfg = load_config(root)
    now = now or utc_now()
    state = st.load_state(root)
    weeks = sorted((root / "artifacts/weekly").glob("*/callouts.json"))
    latest_d = st.latest_published(root)
    cal = st.load_callouts(root, latest_d) if latest_d else None
    perf = json.loads((st.weekly_dir(root, latest_d) / "performance.json").read_text()) if latest_d else {}
    card = json.loads((st.weekly_dir(root, latest_d) / "model_card.json").read_text()) if latest_d else {}
    dailies = sorted((root / "artifacts/daily").glob("*/monitor.json"))
    daily = json.loads(dailies[-1].read_text()) if dailies else None

    banners = []
    if cal and cal.get("data_mode") == "sample":
        banners.append("SAMPLE DATA: these callouts use synthetic prices and are for testing only.")
    if not cal:
        banners.append("No published weekly callouts yet.")
    elif (now.date() - latest_d).days > 8:
        banners.append(f"Weekly run missing: last published callouts are for {latest_d}.")
    if cal and cal.get("confidence") == "low":
        banners.append("Low confidence: live rank IC diverges from validation; ACTION callouts are halved.")
    first = state.get("first_run_date")
    rb = cfg["settings"]["schedule"]["daily"]["review_banner_after_days"]
    if first and (now.date() - dt.date.fromisoformat(first)).days >= rb:
        banners.append(f"It has been {rb}+ days since the first run. Choose schedule option A, B or C in config/settings.yaml (§5.2). Nothing switches automatically.")
    if daily:
        banners += daily.get("banners", [])
    if cal:
        for a, r in (cal.get("contribution_room") or {}).items():
            banners += [f"{a}: {w}" for w in r.get("warnings", [])]
        if cal.get("uncovered_holdings"):
            banners.append("Holdings without price data (not valued or reviewed): " + "; ".join(cal["uncovered_holdings"])
                           + ". Check the ticker in positions.csv (.TO for TSX).")
        banners += [w for w in (cal.get("position_warnings") or []) if "dated after" in w]

    fee = float(cfg["settings"]["fees"]["per_trade_cad"])
    ctx = {"fee": fee, "regime": (cal or {}).get("regime") or {}, "stressed": ((cal or {}).get("regime") or {}).get("label") == "stressed",
           "ic": (cal or {}).get("projection_ic", 0.05),
           "held": {r["ticker"]: r for r in (cal or {}).get("portfolio") or []}}
    if cal:
        try:
            import csv
            with (st.weekly_dir(root, latest_d) / "scores.csv").open() as fh:
                ctx["n_scored"] = sum(1 for _ in csv.DictReader(fh))
        except OSError:
            pass
    items = Items(ctx)
    sub = (f"Week of {e(cal['decision_date'])} · prices from the close of {e(cal.get('close_used') or cal['decision_date'])}"
           + (" · on-demand run" if cal.get("run_kind") == "on_demand_today" else "") if cal else "No published week yet")
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Weekly Stock Callouts</title><style>{CSS}</style></head><body>"
             "<div class='topbar'><div class='wrap'><nav><a href='#week'>This week</a><a href='#portfolio'>Portfolio</a>"
             "<a href='#house'>House money</a><a href='#highrisk'>High-risk ideas</a><a href='#hood'>Under the hood</a></nav></div></div>"
             "<div class='hero'><div class='wrap'>"
             f"<h1>Weekly stock callouts</h1><div class='sub'>{sub}</div>"
             "</div></div>"
             "<main class='wrap'>" + (kpis(cal) if cal else "")
             + "".join(f"<div class='banner'>{e(b)}</div>" for b in banners)
             + "<div class='notices'>" + " · ".join(e(n) for n in NOTICES) + "</div>"]

    # 1. this week
    parts.append("<h2 id='week'>This week's trade suggestions</h2>")
    if cal:
        if ctx["stressed"]:
            parts.append("<div class='banner'><b>Market caution.</b> The market model reads a stressed phase "
                         f"({fmt_pct(ctx['regime'].get('stressed_prob'))} probability). New buys are limited to Low-risk stocks; "
                         "stress alone no longer marks stocks High risk or triggers sells.</div>")
        n_act = sum(1 for c in cal["callouts"] if c["class"] in SIDE)
        parts.append(f"<p>{n_act} to do this week (green edge = buy, red = sell), best projected value first, then good ideas that "
                     "don't fit this week's trade limit or cash. <b>Tap any card for the reasons</b>, in plain words or in financial "
                     "detail, with charts.</p>")
        repo = os.environ.get("GITHUB_REPOSITORY") or (cfg["settings"].get("github") or {}).get("repository")
        if repo and cal.get("status") == "published":
            blank = dict(cal, callouts=[])
            parts.append(f"<p><a class='btn' href='{e(confirm_url(repo, cal, fee))}' target='_blank' rel='noopener'>Confirm trades I made</a>"
                         f"<a class='btn alt' href='{e(confirm_url(repo, blank, fee))}' target='_blank' rel='noopener'>Record another trade</a>"
                         "<br><span class='muted'>Opens a GitHub issue with the trades filled in. Delete what you didn't do, fix prices, "
                         "submit. Your trades, holdings and cash update within a few minutes and the issue gets a summary.</span></p>")
        todo = [c for c in cal["callouts"] if c["class"] in SIDE]
        ideas = [c for c in cal["callouts"] if c["class"] not in SIDE]
        if todo:
            parts.append(f"<h3 class='grp'>To do this week <span class='pill'>{len(todo)}</span></h3>"
                         "<div class='cards'>" + "".join(sugg_card(c, items) for c in todo) + "</div>")
        if ideas:
            parts.append(f"<h3 class='grp'>Good ideas, not this week <span class='pill'>{len(ideas)}</span></h3>"
                         "<p class='muted'>Worth a look, but they don't fit this week's trade limit, cash or risk rules.</p>"
                         "<div class='cards'>" + "".join(sugg_card(c, items) for c in ideas) + "</div>")
        if not cal["callouts"]:
            parts.append("<p class='muted'>No suggestions this week.</p>")
        parts.append(f"<p class='muted'>Run <span class='mono'>{e(cal['run_id'])[:8]}</span> · model card "
                     f"<span class='mono'>{e(cal['model_card_hash'])[:12]}</span> · computed {e(cal['computed_at_utc'])}"
                     f"{' · ' + e(cal['list_note']) if cal.get('list_note') else ''}</p>")
        info = []
        fx = cal.get("fx") or {}
        if fx.get("usdcad"):
            info.append(f"US holdings ({e(', '.join(fx.get('converted', [])))}) converted at USD/CAD {fx['usdcad']:.4f} "
                        f"({e(fx.get('date'))})")
        if cal.get("us_screened"):
            info.append(f"{len(cal['us_screened'])} US stocks from the monthly screen were scored too (buys go to the RRSP only)")
        if cal.get("held_outside_lists"):
            info.append("reviewed but never suggested as new buys (not on the stock list): "
                        + e(", ".join(cal["held_outside_lists"])))
        if info:
            parts.append("<p class='muted'>" + " · ".join(info) + "</p>")
        if cal.get("holdings_review"):
            parts.append("<h3>Your other holdings</h3>" + mini_table(cal["holdings_review"], items))
        if cal.get("us_screened"):
            us = cal["us_screened"]
            meta = {}
            mp = root / "data/universe/us_screened.csv"
            if mp.exists():
                import csv
                with mp.open() as fh:
                    meta = {r["ticker"]: r for r in csv.DictReader(fh)}
            rows = "".join(f"<tr><td>{i}</td><td><b>{e(t)}</b></td><td>{e(meta.get(t, {}).get('name', ''))}</td>"
                           f"<td>{e(meta.get(t, {}).get('sector', ''))}</td></tr>" for i, t in enumerate(us, 1))
            parts.append(f"<details class='fold'><summary>US stocks being watched ({len(us)}) · from the monthly screen, "
                         "scored every week, buys go to the RRSP</summary><div class='card'><table><tr><th>#</th><th>Ticker</th>"
                         f"<th>Name</th><th>Sector</th></tr>{rows}</table></div></details>")
        if cal.get("watchlist"):
            parts.append("<h3>Watch list</h3>" + mini_table(cal["watchlist"], items))
        parts.append("<h2 id='portfolio'>Portfolio split</h2>" + portfolio_section(cal))
        if cal.get("house_money"):
            parts.append(house_section(cal["house_money"]))
    if cal and cal.get("high_risk_ideas") is not None:
        parts.append("<h2 id='highrisk'>High-risk ideas for next week</h2>"
                     "<div class='banner'>Separate from the 10 suggestions and <b>not</b> checked the same way: these may have a High "
                     "risk label, a recent structural break, earnings next week, or break the size or sector limits. They are the "
                     "stocks the models expect to rise most next week. The target is the expected price in one week; 4 times in 5 "
                     "the price should land inside the range. Not counted in your trade limits. If you buy one, record it with "
                     "Record another trade. Tap a row for the reasons.</div>")
        parts.append(high_risk_table(cal["high_risk_ideas"], items) if cal["high_risk_ideas"] else
                     "<p class='muted'>No stock met the bar this week (expected rise next week and a score in the top half).</p>")
    # 2. budget
    parts.append("<details class='hood' id='hood'><summary><span>Under the hood</span><span class='muted'>account budget, risk "
                 "posture, performance, backtest, history and run health</span></summary>")
    parts.append("<h2 id='budget'>Account budget</h2><div class='grid'>")
    if cal:
        for a, b in cal["budgets"].items():
            room = (cal.get("contribution_room") or {}).get(a, {})
            parts.append(f"<div class='card'><b>{e(a)}</b> · {e(b.get('month'))}<br>Cap {b['cap']} · used {b['used']} · "
                         f"remaining {b['remaining']} · this week {b['weekly_allowance']}<br>Cash ${cal.get('cash_cad', {}).get(a, 0):,.2f}"
                         + (f"<br>Est. contribution room ${room.get('estimated_remaining_cad', 0):,.0f} (entered ${room.get('room_entered_cad', 0):,.0f} as of {e(room.get('as_of'))})" if room else "")
                         + (f"<br><span class='muted'>{e(room.get('note'))}</span>" if room.get("note") else "")
                         + budget_bars((cal.get('budget_history') or {}).get(a, [])) + "</div>")
    parts.append("</div>")
    # 3. risk posture
    parts.append("<h2 id='risk'>Risk posture</h2><div class='grid'>")
    if cal:
        r = cal["regime"]
        sp = cal["starter_phase"]
        parts.append(f"<div class='card'><b>Regime</b><br>{e(r['label'])} · stressed prob {fmt_pct(r.get('stressed_prob'))}<br><span class='muted'>Stress is a caution: it limits new buys to Low-risk stocks but does not change risk labels.</span></div>")
        parts.append(f"<div class='card'><b>Starter phase</b><br>{'active' if sp['active'] else 'complete (sector caps on)'} · "
                     f"${sp['invested_cad']:,.0f} of ${sp['threshold_cad']:,.0f} ({fmt_pct(sp['invested_cad'] / sp['threshold_cad'] if sp['threshold_cad'] else 0)})</div>")
        ov = cal.get("overrides_active") or []
        parts.append("<div class='card'><b>Overrides</b><br>" + ("<br>".join(f"{e(o['override'])}: {len(o['tickers'])} names" for o in ov) or "none") + "</div>")
        gaps = (daily or {}).get("data_gaps") or {}
        brks = (daily or {}).get("new_structural_breaks") or []
        parts.append(f"<div class='card'><b>Data gaps</b> (latest daily)<br>{e(', '.join(gaps) or 'none')}<br><b>New breaks</b><br>{e(', '.join(b['ticker'] for b in brks) or 'none')}</div>")
    if daily and daily.get("opportunity_flags"):
        parts.append("<div class='card'><b>Daily opportunity flags</b> (not actions; next weekly run decides)<br>"
                     + "<br>".join(f"{e(f['ticker'])}: {e('; '.join(f['reasons']))}" for f in daily["opportunity_flags"]) + "</div>")
    parts.append("</div>")
    # 4. performance
    parts.append("<h2 id='perf'>Performance</h2><div class='card'>")
    if perf:
        li = perf.get("rank_ic_live", {})
        parts.append("<p class='muted'>Short-window IC is a diagnostic only. No absolute return target is shown.</p><table><tr><th>Live rank IC</th><th>1w</th><th>3m</th><th>6m</th><th>12m</th></tr>")
        for lab in ("ic_5d", "ic_63d"):
            parts.append(f"<tr><td>{lab}</td>" + "".join("<td>" + signed(li.get(lab + "_" + w)) + "</td>" for w in ("1w", "3m", "6m", "12m")) + "</tr>")
        parts.append("</table>")
        val = (card.get("validation") or {})
        if val:
            parts.append("<p>Validation (purged walk-forward, backtest, survivorship-biased): mean IC "
                         + ", ".join(f"{k} {v:+.3f}" for k, v in (val.get("mean_ic") or {}).items() if v is not None) + "</p><table><tr><th>Test year</th><th>Scheme</th><th>IC blend</th><th>IC M1</th><th>IC M2</th></tr>")
            for f in val.get("folds", []):
                parts.append(f"<tr><td>{f['test_year']}{' (regime holdout)' if f.get('holdout_regime') else ''}</td><td>{e(f.get('scheme'))}</td>"
                             + "".join("<td>" + signed(f.get(k)) + "</td>" for k in ("ic_blend", "ic_m1", "ic_m2")) + "</tr>")
            parts.append("</table>")
        hr = perf.get("hit_rate_as_if")
        parts.append(f"<p>Hit rate of past ACTION callouts (as-if, vs benchmark): {fmt_pct(hr)} over {perf.get('actions_scored', 0)} actions. "
                     f"Benchmarks since first run: {e(', '.join(f'{k} {v:+.1%}' for k, v in (perf.get('benchmarks_since_start') or {}).items()) or 'n/a')}. "
                     "</p>")
        lr = perf.get("live_replay") or {}
        if lr.get("nav") and len(lr["nav"]) >= 2:
            parts.append(f"<h3>Live record (as-if, since {e(lr.get('start'))})</h3><p class='muted'>{e(lr.get('note'))}</p>"
                         + line_chart(lr["nav"], ["callouts", "baseline", "XIC.TO", "XIU.TO"], "live-nav", "Live as-if value vs baseline and benchmarks")
                         + perf_table(lr.get("series", {})))
    parts.append("</div>")
    bt_dir = root / "artifacts/backtest/latest"
    if (bt_dir / "report.json").exists():
        import csv
        rep = json.loads((bt_dir / "report.json").read_text())
        nav = [dict(r, **{k: float(v) for k, v in r.items() if k != "date" and v not in ("", None)})
               for r in csv.DictReader((bt_dir / "nav.csv").open())]
        dsr, pbo = rep.get("dsr") or {}, rep.get("pbo") or {}
        parts.append("<h2 id='backtest'>Backtest</h2><div class='card'>"
                     f"<p class='banner'>{e(rep.get('caveat'))}</p>"
                     f"<p><b>{e(rep.get('verdict') or 'No verdict yet')}</b> · {e(rep.get('start'))} to {e(rep.get('end'))} · "
                     f"{rep.get('weeks')} weeks · {rep.get('trades')} trades (baseline {rep.get('baseline_trades')}) · data {e(rep.get('data_mode'))}</p>"
                     + line_chart(nav, ["callouts", "baseline", "XIC.TO", "XIU.TO"], "bt-nav", "Backtest value vs baseline and benchmarks")
                     + perf_table(rep.get("series", {}))
                     + f"<p>Deflated Sharpe Ratio: <b>{'n/a' if dsr.get('dsr') is None else format(dsr['dsr'], '.2f')}</b> "
                     f"(probability the Sharpe is real after {dsr.get('n_trials', 0)} trials; above 0.95 is strong) · "
                     f"Probability of backtest overfitting: <b>{'n/a' if pbo.get('pbo') is None else format(pbo['pbo'], '.0%')}</b> "
                     f"({pbo.get('splits', 0)} CSCV splits; lower is better{', ' + e(pbo.get('reason')) if pbo.get('reason') else ''})</p>")
        ab = rep.get("ablation") or {}
        if ab.get("groups"):
            parts.append("<p>Feature-group ablation (walk-forward blend IC; positive drop = the group helps): all features "
                         + signed(ab.get("ic_all")) + "</p><table><tr><th>Group</th><th>IC without</th><th>IC drop</th></tr>"
                         + "".join(f"<tr><td>{e(g['group'])}</td><td>{signed(g['ic_without'])}</td><td>{signed(g['ic_drop'])}</td></tr>" for g in ab["groups"])
                         + "</table>")
        parts.append("</div>")
    # 5. history
    parts.append("<h2 id='history'>History</h2><div class='card'><table><tr><th>Decision date</th><th>Status</th><th>Callouts</th><th class='hide-sm'>Actions</th></tr>")
    for p in reversed(weeks[-52:]):
        c = json.loads(p.read_text())
        n_act = sum(1 for x in c.get("callouts", []) if x["class"].startswith("ACTION"))
        parts.append(f"<tr><td>{e(c['decision_date'])}</td><td>{e(c['status'])}{' (catch-up)' if c.get('catchup') else ''}</td>"
                     f"<td>{len(c.get('callouts', []))}</td><td class='hide-sm'>{n_act}</td></tr>")
    parts.append("</table>")
    j = root / "artifacts/override_journal.jsonl"
    if j.exists():
        parts.append("<p><b>Override journal</b></p><ul>" + "".join(f"<li>{e(l)}</li>" for l in j.read_text().splitlines()[-20:]) + "</ul>")
    parts.append("</div>")
    # 6. run health
    ls = state.get("last_success", {})
    news = perf.get("news") or {}
    sched = cfg["settings"]["schedule"]
    opt = "A (daily + weekly)" if sched["daily"]["enabled"] and len(sched["daily"]["days"]) == 5 else \
        ("C (weekly + mid-week)" if sched["daily"]["enabled"] else "B (weekly only)")
    parts.append(f"<h2 id='health'>Run health</h2><div class='grid'><div class='card'><b>Last successful runs</b><br>weekly {e(ls.get('weekly'))}<br>daily {e(ls.get('daily'))}<br>schedule option {e(opt)}</div>"
                 f"<div class='card'><b>Sources</b><br>price source {e(card.get('price_source'))}<br>throttled/failed: {e(', '.join(card.get('sources_throttled') or []) or 'none')}<br>data gaps: {card.get('data_gap_count', 'n/a')}</div>"
                 f"<div class='card'><b>News collection</b> (forward-only, shadow)<br>"
                 + ("<br>".join(f"{e(k)}: {v['items']} items, match rate {fmt_pct(v.get('match_rate'))}" for k, v in (news.get('by_source') or {}).items()) or "no stats yet")
                 + news_gate_text(perf.get("news_gate")) + "</div></div>")
    popup = {"items": items.data, "ph": (cal or {}).get("price_history") or {"dates": [], "series": {}}, "fn": ex.FEATURE_NAME}
    parts.append(f"</details><p class='muted'>Generated {e(now.isoformat(timespec='seconds'))} from committed artifacts.</p></main>{MODAL_HTML}"
                 "<script type='application/json' id='popup-data'>" + json.dumps(popup).replace("</", "<\\/") + "</script>"
                 f"<script>{MODAL_JS}</script></body></html>")
    out.mkdir(parents=True, exist_ok=True)
    path = out / "index.html"
    path.write_text("\n".join(parts))
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="_site")
    a = ap.parse_args()
    print(build(Path(a.root), Path(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

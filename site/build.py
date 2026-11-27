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

NOTICES = [
    "Recommendations only. Not investment advice. The owner places all orders.",
    "Not a trading portal: up to 10 trade suggestions a week, within monthly trade limits.",
    "Backtested figures carry survivorship bias: the universe is today's list plus a manual delisting log.",
    "Positions shown here are public by owner choice.",
]

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e1dc;--low:#1f7a4d;--low-bg:#e3f3ea;
--mod:#8a5a00;--mod-bg:#fbf0d9;--high:#a1261c;--high-bg:#f9e1de;--accent:#2f5fa7;--banner:#fff6d6;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--grid:#e6e5e0}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#9c9b94;--card:#201f1d;--line:#34332f;
--low:#6fd3a0;--low-bg:#17352a;--mod:#f0c46a;--mod-bg:#3a2e12;--high:#ff8f84;--high-bg:#42201c;--accent:#8fb4ff;--banner:#3a3416;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--grid:#2e2d2a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header,main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0}h2{font-size:18px;margin:28px 0 10px}
nav a{margin-right:14px;color:var(--accent);text-decoration:none;font-size:14px}.notices{font-size:13px;color:var(--muted)}
.banner{background:var(--banner);border:1px solid var(--line);padding:10px 12px;border-radius:8px;margin:8px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}
.Low{color:var(--low);background:var(--low-bg)}.Moderate{color:var(--mod);background:var(--mod-bg)}.High{color:var(--high);background:var(--high-bg)}
.pass{color:var(--low)}.warn{color:var(--mod)}.fail{color:var(--high)}.na{color:var(--muted)}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}
.co{border-left:4px solid var(--line)}.co.ACTION{border-left-color:var(--accent)}.muted{color:var(--muted);font-size:13px}
.cls{font-weight:700;font-size:12px;letter-spacing:.02em}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.chart{position:relative}.chart svg{width:100%;height:auto;display:block}.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;margin:4px 0}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;display:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.bars{display:flex;gap:3px;align-items:flex-end;height:48px}.bars span{flex:1;background:var(--s1);border-radius:3px 3px 0 0;min-height:1px}
.btn{display:inline-block;background:var(--accent);color:var(--bg);padding:8px 14px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;margin:4px 10px 4px 0}
.btn.alt{background:transparent;color:var(--accent);border:1px solid var(--accent)}h3{font-size:15px;margin:20px 0 6px}
.pv{font-weight:600}
@media (max-width:640px){td,th{padding:5px 4px}.hide-sm{display:none}}
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


def callout_card(c: dict) -> str:
    is_action = c["class"].startswith("ACTION")
    open_attr = ""  # all collapsed to a one-line summary; High risk never opens by default (§14.3)
    dims = " ".join(f'{k}:{tag(v)}' for k, v in sorted(c["risk_breakdown"].items()))
    checks = "".join(f'<tr><td>{e(k["name"])}</td><td class="{e(k["result"]).replace("/", "")}">{e(k["result"])}</td>'
                     f'<td>{e(k["detail"])}</td></tr>' for k in c["checks"])
    shap = ", ".join(f'{e(s["feature"])} ({s["value"]:+.3f})' for s in c.get("shap_top_features", []))
    ci = c.get("conformal_interval_80") or [None, None]
    ci_txt = f"{ci[0]:+.1%} to {ci[1]:+.1%}" if None not in ci else "n/a"
    addon = ""
    if c.get("addon"):
        a = c["addon"]
        addon = (f'<p><b>Add-on assessment:</b> {e(a["verdict"])} ({e(a["mode"])}), new ACB ${a["new_acb_cad"]:,.2f}, '
                 f'new weight {fmt_pct(a["new_weight"])}, risk share change '
                 f'{signed(a["risk_contrib_change"], ".1%")}. '
                 + " ".join(e(r) + "." for r in a["reasons"]) + "</p>")
    trade = (f'{e(c["account"])} · {c["shares"]} sh @ ~${c["est_price_cad"]:,.2f} · est. cost ${c["est_cost_cad"]:,.2f}'
             if is_action else (f'reason: {e(c["demoted_reason"])}' if c.get("demoted_reason") else ""))
    pv = c.get("projected_value_cad")
    if pv is not None:
        trade = (f'<span class="pv">projected value {"+" if pv >= 0 else "−"}${abs(pv):,.0f}</span> '
                 f'({signed(c.get("projected_return_3m"), ".1%")} over 3 months) · ' + trade)
    return f"""<details class="card co {'ACTION' if is_action else ''}"{open_attr}>
<summary><b>#{c['rank']} {e(c['ticker'])}</b> <span class="cls">{e(c['class'])}</span> {tag(c['risk_label'])}
<span class="muted">score {fmt_pct(c['score_pct'])} · {trade}</span><br><span>{e(c['rationale'])}</span></summary>
<p class="muted">Risk breakdown: {dims}</p>
<p class="muted">80% interval (next week): {ci_txt} · SHAP drivers: {shap or 'n/a'} · warnings: {e(', '.join(c['warnings']) or 'none')}</p>
{addon}<table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>{checks}</table></details>"""


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


def mini_table(items: list[dict]) -> str:
    rows = "".join(f"<tr><td><b>{e(c['ticker'])}</b></td><td class='cls'>{e(c['class'])}</td><td>{tag(c['risk_label'])}</td>"
                   f"<td>{fmt_pct(c['score_pct'])}</td><td>{e(c['rationale'])}</td></tr>" for c in items)
    return ("<table><tr><th>Ticker</th><th>Status</th><th>Risk</th><th>Score</th><th>Note</th></tr>" + rows + "</table>")


def high_risk_table(items: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{x['rank']}</td><td><b>{e(x['ticker'])}</b>{' <span class=muted>(held)</span>' if x.get('held') else ''}</td>"
        f"<td>${x['price_cad']:,.2f}</td><td><b>${x['target_price_1w']:,.2f}</b></td>"
        f"<td class='hide-sm'>${x['range_80_1w'][0]:,.2f} to ${x['range_80_1w'][1]:,.2f}</td>"
        f"<td>{signed(x['expected_return_1w'], '.1%')}</td><td class='hide-sm'>{fmt_pct(x['score_pct'])}</td>"
        f"<td>{tag(x['risk_label'])}</td><td class='hide-sm muted'>{e('; '.join(x['outside_criteria']))}</td></tr>"
        for x in items)
    return ("<table><tr><th>#</th><th>Ticker</th><th>Price</th><th>Target, 1 week</th><th class='hide-sm'>80% range</th>"
            "<th>Expected</th><th class='hide-sm'>Score</th><th>Risk</th><th class='hide-sm'>Outside the main list because</th></tr>"
            + rows + "</table>")


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

    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Weekly Stock Callouts</title><style>{CSS}</style></head><body><header><h1>Weekly stock callouts</h1>"
             "<nav><a href='#week'>This week</a><a href='#highrisk'>High-risk ideas</a><a href='#budget'>Account budget</a><a href='#risk'>Risk posture</a>"
             "<a href='#perf'>Performance</a><a href='#backtest'>Backtest</a><a href='#history'>History</a><a href='#health'>Run health</a></nav>"
             "<div class='notices'>" + "<br>".join(e(n) for n in NOTICES) + "</div>"
             + "".join(f"<div class='banner'>{e(b)}</div>" for b in banners) + "</header><main>"]

    # 1. this week
    parts.append("<h2 id='week'>This week's trade suggestions</h2>")
    if cal:
        parts.append(f"<p class='muted'>Decision date {e(cal['decision_date'])} · computed {e(cal['computed_at_utc'])} · "
                     f"run <span class='mono'>{e(cal['run_id'])[:8]}</span> · model card <span class='mono'>{e(cal['model_card_hash'])[:12]}</span>"
                     f"{' · ' + e(cal['list_note']) if cal.get('list_note') else ''}</p>")
        info = []
        if cal.get("close_used"):
            info.append(f"prices: close of {e(cal['close_used'])}"
                        + (" (on-demand run dated today)" if cal.get("run_kind") == "on_demand_today" else ""))
        fx = cal.get("fx") or {}
        if fx.get("usdcad"):
            info.append(f"US holdings ({e(', '.join(fx.get('converted', [])))}) converted at USD/CAD {fx['usdcad']:.4f} "
                        f"({e(fx.get('date'))})")
        if cal.get("held_outside_lists"):
            info.append("reviewed but never suggested as new buys (not on the stock list): "
                        + e(", ".join(cal["held_outside_lists"])))
        if info:
            parts.append("<p class='muted'>" + " · ".join(info) + "</p>")
        n_act = sum(1 for c in cal["callouts"] if c["class"] in SIDE)
        parts.append(f"<p>{n_act} to do this week (blue edge), best projected value first. The rest are good trades that don't fit "
                     "this week's trade limit or cash, also by projected value. Projected value is the score turned into an expected "
                     "3-month return on the amount traded, minus the commission: a ranking estimate, not a promise.</p>")
        repo = os.environ.get("GITHUB_REPOSITORY") or (cfg["settings"].get("github") or {}).get("repository")
        if repo and cal.get("status") == "published":
            fee = float(cfg["settings"]["fees"]["per_trade_cad"])
            blank = dict(cal, callouts=[])
            parts.append(f"<p><a class='btn' href='{e(confirm_url(repo, cal, fee))}' target='_blank' rel='noopener'>Confirm trades I made</a>"
                         f"<a class='btn alt' href='{e(confirm_url(repo, blank, fee))}' target='_blank' rel='noopener'>Record another trade</a>"
                         "<br><span class='muted'>Opens a GitHub issue with the trades filled in. Delete what you didn't do, fix prices, "
                         "submit. Your trades, holdings and cash update within a few minutes and the issue gets a summary.</span></p>")
        parts += [callout_card(c) for c in cal["callouts"]]
        if cal.get("holdings_review"):
            parts.append("<h3>Your other holdings</h3>" + mini_table(cal["holdings_review"]))
        if cal.get("watchlist"):
            parts.append("<h3>Watch list</h3>" + mini_table(cal["watchlist"]))
    if cal and cal.get("high_risk_ideas") is not None:
        parts.append("<h2 id='highrisk'>High-risk ideas for next week</h2>"
                     "<div class='banner'>Separate from the 10 suggestions and <b>not</b> checked the same way: these may have a High "
                     "risk label, a recent structural break, earnings next week, or break the size or sector limits. They are the "
                     "stocks the models expect to rise most next week. The target is the expected price in one week; 4 times in 5 "
                     "the price should land inside the range. Not counted in your trade limits. If you buy one, record it with "
                     "Record another trade.</div>")
        parts.append(high_risk_table(cal["high_risk_ideas"]) if cal["high_risk_ideas"] else
                     "<p class='muted'>No stock met the bar this week (expected rise next week and a score in the top half).</p>")
    # 2. budget
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
        parts.append(f"<div class='card'><b>Regime</b><br>{e(r['label'])} · stressed prob {fmt_pct(r.get('stressed_prob'))}</div>")
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
    parts.append(f"<p class='muted'>Generated {e(now.isoformat(timespec='seconds'))} from committed artifacts.</p></main></body></html>")
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

"""Static dashboard generator (§14). Reads committed artifacts only; writes one HTML page.

    python site/build.py --root . --out _site

Every number on the page comes from an artifact and shows its run_id / model_card_hash.
TODO(P7): per-account 12-month charts, override journal outcomes, naive-baseline panel.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import load_config  # noqa: E402
from common.dates import utc_now  # noqa: E402
from pipeline import state as st  # noqa: E402

NOTICES = [
    "Recommendations only. Not investment advice. The owner places all orders.",
    "Not a trading portal: callouts are weekly and transaction-capped.",
    "Backtested figures carry survivorship bias: the universe is today's list plus a manual delisting log.",
    "Positions shown here are public by owner choice.",
]

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e1dc;--low:#1f7a4d;--low-bg:#e3f3ea;
--mod:#8a5a00;--mod-bg:#fbf0d9;--high:#a1261c;--high-bg:#f9e1de;--accent:#2f5fa7;--banner:#fff6d6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#9c9b94;--card:#201f1d;--line:#34332f;
--low:#6fd3a0;--low-bg:#17352a;--mod:#f0c46a;--mod-bg:#3a2e12;--high:#ff8f84;--high-bg:#42201c;--accent:#8fb4ff;--banner:#3a3416}}
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
    return f"""<details class="card co {'ACTION' if is_action else ''}"{open_attr}>
<summary><b>#{c['rank']} {e(c['ticker'])}</b> <span class="cls">{e(c['class'])}</span> {tag(c['risk_label'])}
<span class="muted">score {fmt_pct(c['score_pct'])} · {trade}</span><br><span>{e(c['rationale'])}</span></summary>
<p class="muted">Risk breakdown: {dims}</p>
<p class="muted">80% interval (next week): {ci_txt} · SHAP drivers: {shap or 'n/a'} · warnings: {e(', '.join(c['warnings']) or 'none')}</p>
{addon}<table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>{checks}</table></details>"""


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

    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>Weekly Stock Callouts</title><style>{CSS}</style></head><body><header><h1>Weekly stock callouts</h1>"
             "<nav><a href='#week'>This week</a><a href='#budget'>Account budget</a><a href='#risk'>Risk posture</a>"
             "<a href='#perf'>Performance</a><a href='#history'>History</a><a href='#health'>Run health</a></nav>"
             "<div class='notices'>" + "<br>".join(e(n) for n in NOTICES) + "</div>"
             + "".join(f"<div class='banner'>{e(b)}</div>" for b in banners) + "</header><main>"]

    # 1. this week
    parts.append("<h2 id='week'>This week's callouts</h2>")
    if cal:
        parts.append(f"<p class='muted'>Decision date {e(cal['decision_date'])} · computed {e(cal['computed_at_utc'])} · "
                     f"run <span class='mono'>{e(cal['run_id'])[:8]}</span> · model card <span class='mono'>{e(cal['model_card_hash'])[:12]}</span>"
                     f"{' · ' + e(cal['list_note']) if cal.get('list_note') else ''}</p>")
        parts += [callout_card(c) for c in cal["callouts"]]
    # 2. budget
    parts.append("<h2 id='budget'>Account budget</h2><div class='grid'>")
    if cal:
        for a, b in cal["budgets"].items():
            room = (cal.get("contribution_room") or {}).get(a, {})
            parts.append(f"<div class='card'><b>{e(a)}</b> · {e(b.get('month'))}<br>Cap {b['cap']} · used {b['used']} · "
                         f"remaining {b['remaining']} · this week {b['weekly_allowance']}<br>Cash ${cal.get('cash_cad', {}).get(a, 0):,.2f}"
                         + (f"<br>Est. contribution room ${room.get('estimated_remaining_cad', 0):,.0f} (entered ${room.get('room_entered_cad', 0):,.0f} as of {e(room.get('as_of'))})" if room else "")
                         + (f"<br><span class='muted'>{e(room.get('note'))}</span>" if room.get("note") else "")
                         + "<br><span class='muted'>12 months: " + ", ".join(f"{h['month'][2:]}:{h['used']}" for h in (cal.get('budget_history') or {}).get(a, [])) + "</span></div>")
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
                     "Naive baseline: not built yet.</p>")
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
                 + "</div></div>")
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

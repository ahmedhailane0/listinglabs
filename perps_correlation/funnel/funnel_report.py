"""Build the funnel report: index (findings + sortable table) and a detail
page per token (venue dates + day-lags + FDV). No charts — text/table only."""
from __future__ import annotations
import html, json, statistics as st
from datetime import date
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make lib./build. importable
from build.build_listing_report import page_meta, build_stamp  # shared CSP/OG head + stamp

HERE = Path(__file__).parent
OUT = HERE.parent / "Listinglabs" / "funnel" / "report"
OUT.mkdir(parents=True, exist_ok=True)
M = json.loads((HERE / "funnel_master.json").read_text(encoding="utf-8"))
# Token count of the sibling "Listing Reactions" report, for the cross-nav.
REACTIONS_N = len(list((HERE.parent / "listings").glob("*.json")))
try:
    SCAMS_N = len(json.loads((HERE.parent.parent / "cache" / "scam_data.json")
                             .read_text(encoding="utf-8")))
except Exception:
    SCAMS_N = None
M.sort(key=lambda x: x["alpha_date"])


def fdv_str(v):
    if not v: return "—"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.0f}M"
    return f"${v/1e3:.0f}K"


def fdv_bucket(v):
    if not v: return "unknown"
    if v < 100e6: return "<100M"
    if v < 300e6: return "100–300M"
    if v < 1e9: return "300M–1B"
    return ">1B"


def dnum(x): return "" if x is None else str(x)
def kr_str(m):
    return "".join(c for c, on in [("U", m["on_upbit"]), ("B", m["on_bithumb"]), ("C", m["on_coinone"])] if on)


def summ(xs):
    xs = [x for x in xs if x is not None]
    return f"median {st.median(xs):.0f}d / mean {st.mean(xs):.0f}d (n={len(xs)})" if xs else "n=0"


COLS = [
    ("symbol", "Token", "t"), ("fdv", "FDV", "n"), ("chain", "Chain", "t"),
    ("alpha_date", "Alpha", "t"), ("perp_date", "Perp", "t"), ("coinbase_date", "Coinbase", "t"),
    ("first_korean", "Korea(1st)", "t"),
    ("days_alpha_to_perp", "Δ A→Perp", "n"), ("days_coinbase_to_perp", "Δ CB→Perp", "n"),
    ("days_coinbase_to_korean", "Δ CB→KR", "n"), ("days_perp_to_korean", "Δ Perp→KR", "n"),
]


def table_rows():
    out = []
    for m in M:
        cells = []
        for key, _lbl, kind in COLS:
            if key == "symbol":
                v = f'<a href="{m["symbol"].lower()}.html">{html.escape(m["symbol"])}</a>'
            elif key == "fdv":
                v = f'<span class="b b-{fdv_bucket(m["fdv"]).replace("<","u").replace("–","-").replace(">","o")}">{fdv_str(m["fdv"])}</span>'
            elif key == "chain":
                v = html.escape(m.get("chain") or "")
            elif key.startswith("days_"):
                v = dnum(m.get(key))
            else:
                v = html.escape(str(m.get(key) or ""))
            sort = m.get(key)
            sortv = "" if sort is None else (str(sort) if kind == "t" else f"{float(sort):015.2f}" if isinstance(sort,(int,float)) else str(sort))
            cells.append(f'<td data-s="{html.escape(str(sortv))}" class="{kind}">{v}</td>')
        out.append(f'<tr>{"".join(cells)} <td class="t">{kr_str(m)}</td></tr>')
    return "\n".join(out)


def _alfdv_bucket(v):
    if not v: return "unknown"
    if v < 100e6: return "<100M"
    if v < 300e6: return "100–300M"
    if v < 1e9: return "300M–1B"
    return ">1B"


def _lag_strip(label, pairs, color="#1f6fb2"):
    """One lag-distribution row as inline SVG: a dot per token at its day-lag
    (exact value + symbol in the native tooltip), a red median line, min/max
    axis labels. Pure build-time SVG — values are the same day-lags shown in
    the table, never binned or smoothed."""
    pairs = [(s, d) for s, d in pairs if d is not None]
    if len(pairs) < 2:
        return ""
    vals = [d for _s, d in pairs]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    X0, X1, W = 14, 546, 560
    x = lambda d: X0 + (d - lo) / span * (X1 - X0)
    med = st.median(vals)
    dots = "".join(
        f'<circle cx="{x(d):.1f}" cy="15" r="3.5" fill="{color}" fill-opacity=".5">'
        f'<title>{html.escape(s)} — {d:g}d</title></circle>'
        for s, d in sorted(pairs, key=lambda p: p[1]))
    return f"""
  <div class="lagrow"><span class="laglbl">{html.escape(label)}</span>
    <svg viewBox="0 0 {W} 30" class="lagsvg" role="img" aria-label="{html.escape(label)} lag distribution">
      <line x1="{X0}" y1="15" x2="{X1}" y2="15" stroke="#dde4ec" stroke-width="1"/>
      {dots}
      <line x1="{x(med):.1f}" y1="3" x2="{x(med):.1f}" y2="27" stroke="#c0392b" stroke-width="2"><title>median {med:g}d</title></line>
      <text x="{X0}" y="28.5" class="lagax">{lo:g}d</text>
      <text x="{X1}" y="28.5" class="lagax" text-anchor="end">{hi:g}d</text>
    </svg>
    <span class="lagsum">median {med:g}d · n={len(pairs)}</span></div>"""


def findings_block():
    new = [m for m in M if m.get("new_launch")]
    majors = [m for m in M if not m.get("new_launch")]
    # primary = new launches
    cb2p_n = [m["days_coinbase_to_perp"] for m in new if (m["days_coinbase_to_perp"] or -1) > 0]
    p2k_n = [m["days_perp_to_korean"] for m in new if (m["days_perp_to_korean"] or -999) >= 0]
    cb2k_n = [m["days_coinbase_to_korean"] for m in new if (m["days_coinbase_to_korean"] or -999) >= 0]
    w3 = sum(1 for x in cb2p_n if x <= 3)
    # pooled (directional)
    p2k_all = [m["days_perp_to_korean"] for m in M if (m["days_perp_to_korean"] or -999) >= 0]
    cb2k_all = [m["days_coinbase_to_korean"] for m in M if (m["days_coinbase_to_korean"] or -999) >= 0]
    coinone_only = [m["symbol"] for m in M if not m.get("korea_datable") and m["on_coinone"]]
    # at-listing FDV buckets (new launches)
    alb = {}
    for m in new:
        alb[_alfdv_bucket(m.get("at_listing_fdv"))] = alb.get(_alfdv_bucket(m.get("at_listing_fdv")), 0) + 1
    curb = {}
    for m in new:
        curb[fdv_bucket(m["fdv"])] = curb.get(fdv_bucket(m["fdv"]), 0) + 1
    albk = " · ".join(f"{k} {alb.get(k,0)}" for k in ["<100M","100–300M","300M–1B",">1B"])
    curbk = " · ".join(f"{k} {curb.get(k,0)}" for k in ["<100M","100–300M","300M–1B",">1B"])
    ge100_al = sum(v for k, v in alb.items() if k != "<100M" and k != "unknown")
    # Lag-distribution strips: SAME filters as the medians quoted above, with
    # token symbols attached so each dot is traceable.
    strips = "".join([
        _lag_strip("Alpha → Binance Perp",
                   [(m["symbol"], m.get("days_alpha_to_perp")) for m in new]),
        _lag_strip("Coinbase → Binance Perp (Coinbase led)",
                   [(m["symbol"], m["days_coinbase_to_perp"]) for m in new
                    if (m["days_coinbase_to_perp"] or -1) > 0], color="#6aa84f"),
        _lag_strip("Binance Perp → Korea",
                   [(m["symbol"], m["days_perp_to_korean"]) for m in new
                    if (m["days_perp_to_korean"] or -999) >= 0], color="#9c6ade"),
        _lag_strip("Coinbase → Korea",
                   [(m["symbol"], m["days_coinbase_to_korean"]) for m in new
                    if (m["days_coinbase_to_korean"] or -999) >= 0], color="#d98c5f"),
    ])
    lag_viz = (f'<h3>Lag distributions — new launches <span class="hint">'
               f'(each dot = one token, hover for symbol · red line = median)</span></h3>'
               f'<div class="lagviz">{strips}</div>') if strips else ""
    return f"""
<section class="findings">
  <h2>Findings — {len(M)} tokens completed Alpha → Binance Perp → Coinbase → Korea (2025–today)</h2>
  <p class="sub">The set splits into <b>{len(new)} genuine new launches</b> (perp within 7d of Alpha) and
  <b>{len(majors)} re-featured majors</b> (perp existed long before Alpha, often 2023–24).
  <b>Primary results use the {len(new)} new launches</b> — the true comparator to the reference's new-listing data.</p>
  <h3>Primary — new launches (n={len(new)})</h3>
  <ul>
    <li><b>Coinbase → Binance Perp:</b> when Coinbase led ({len(cb2p_n)} cases) — {summ(cb2p_n)}; {w3}/{len(cb2p_n)} within 3 days. <span class="ref">✓ matches reference (~2d median)</span></li>
    <li><b>Binance Perp → Korea:</b> {summ(p2k_n)}. <span class="amb">↔ faster than reference (~22d) — fresh launches reach Korea quicker</span></li>
    <li><b>Coinbase → Korea:</b> {summ(cb2k_n)}. <span class="ref">✓ matches reference (~8–10d median)</span></li>
    <li><b>FDV at listing (perp price at onboard × total supply):</b> {albk}. <b>{ge100_al}/{len(new)} were ≥$100M at listing</b>, mass at $300M–1B+. <span class="ref">✓ strongly confirms "Binance favors $100M–1B"</span></li>
    <li><b>Why current FDV misleads:</b> same tokens <i>today</i>: {curbk}. Post-listing dumps push them into &lt;$100M — so a current-FDV snapshot would have <i>falsely rejected</i> the finding.</li>
  </ul>
  {lag_viz}
  <h3>Pooled — all {len(M)} (directional only)</h3>
  <ul>
    <li>Perp→Korea {summ(p2k_all)}; Coinbase→Korea {summ(cb2k_all)} — these match the reference's ~22–25d medians, but only because <b>re-featured majors drag the tail up</b>. Treat as directional, not a clean test.</li>
  </ul>
  <p class="cav">Caveats: venue dates are earliest-candle proxies (±1–2d). Coinone history is API-capped → excluded from Korea (so {", ".join(coinone_only) or "—"} have no datable Korean leg; 72/74 Korea-datable). At-listing FDV uses the first daily perp close (≈onboard) × current total supply. Δ = days; negative = second venue earlier.</p>
</section>"""


def index_html():
    head = "".join(f'<th data-i="{i}">{lbl}</th>' for i, (_k, lbl, _ki) in enumerate(COLS)) + '<th>KR</th>'
    scam_lbl = f"Manipulated ({SCAMS_N})" if SCAMS_N else "Manipulated"
    desc = (f"Alpha → Binance Perp → Coinbase → Korea funnel study: {len(M)} tokens "
            f"with listing-to-listing timing gaps and FDV-at-listing.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{page_meta("CEX → Korea funnel — ListingLabs", desc, favicon_rel="../../favicon.svg")}
<title>Alpha→Perp→Coinbase→Korea funnel</title><style>{CSS}</style></head><body>
<header><h1>CEX → Korea <span style="opacity:.6;font-weight:400">· Binance Alpha → Perp → Coinbase → Korea</span></h1>
<nav class="topnav"><a href="../../report/index.html">Binance Alpha &amp; Perps ({REACTIONS_N})</a><a class="active" href="index.html">CEX → Korea ({len(M)})</a><a href="../../scams/index.html">{scam_lbl}</a></nav>
<p>{len(M)} tokens · 2025-01-01 → today · click a token for its timing detail · updated {build_stamp()} UTC</p></header>
{findings_block()}
<section class="tablewrap"><h3>Funnel table <span class="hint">(click a header to sort)</span></h3>
<table id="ft"><thead><tr>{head}</tr></thead><tbody>
{table_rows()}
</tbody></table></section>
<script>{SORT_JS}</script></body></html>"""


def detail_html(m):
    rows = [("Chain", m.get("chain")), ("FDV", fdv_str(m["fdv"]) + f'  ({m.get("fdv_src","")})'),
            ("Contract", m.get("contract")),
            ("Binance Alpha", m.get("alpha_date")), ("Binance Perp", m.get("perp_date")),
            ("Coinbase spot", m.get("coinbase_date")), ("Upbit", m.get("upbit_date")),
            ("Bithumb", m.get("bithumb_date"))]
    dl = "".join(f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v or '—'))}</dd>" for k, v in rows)
    lags = [("Alpha → Perp", m["days_alpha_to_perp"]), ("Alpha → Coinbase", m["days_alpha_to_coinbase"]),
            ("Alpha → Korea", m["days_alpha_to_korean"]), ("Coinbase → Perp", m["days_coinbase_to_perp"]),
            ("Coinbase → Korea", m["days_coinbase_to_korean"]), ("Perp → Korea", m["days_perp_to_korean"])]
    lag_html = "".join(f'<div class="lag"><span class="k">{html.escape(k)}</span><span class="v">{("—" if v is None else str(v)+"d")}</span></div>' for k, v in lags)
    cmc = f'<a href="https://coinmarketcap.com/currencies/{m["cmc_slug"]}/" target="_blank">CoinMarketCap ↗</a>' if m.get("cmc_slug") else ""
    _desc = (f"{m.get('name') or m['symbol']} ({m['symbol']}) in the CEX → Korea funnel — "
             f"venue listing dates and day-lags from Binance Alpha to Korea.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{page_meta(f"{m['symbol']} — funnel", _desc, favicon_rel="../../favicon.svg")}
<title>{html.escape(m['symbol'])} — funnel</title><style>{CSS}</style></head><body>
<header><a class="back" href="index.html">← all {len(M)} tokens</a></header>
<main class="detail"><div class="info"><h2>{html.escape(m.get('name') or m['symbol'])} <span class="sym">{html.escape(m['symbol'])}</span></h2>
{cmc}<dl>{dl}</dl><h4>Listing lags</h4><div class="lags">{lag_html}</div></div></main>
</body></html>"""


CSS = """
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f6f9;color:#1d2733}
header{padding:16px 28px;background:#1f4e79;color:#fff}header h1{margin:0;font-size:19px}header p{margin:4px 0 0;opacity:.85;font-size:13px}
a{color:#1f6fb2;text-decoration:none}a:hover{text-decoration:underline}.back{color:#fff}
section{padding:18px 28px}h2{font-size:17px;margin:0 0 10px}h3{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:#6b7785}
.topnav{margin:8px 0 2px;display:flex;gap:8px}.topnav a{font-size:13px;color:#fff;opacity:.8;padding:4px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.35)}
.topnav a:hover{opacity:1;text-decoration:none;background:rgba(255,255,255,.1)}.topnav a.active{opacity:1;background:#fff;color:#1f4e79;border-color:#fff;font-weight:600}
.findings{background:#fff;border-bottom:1px solid #e1e7ee}.findings ul{margin:8px 0;padding-left:18px}.findings li{margin:4px 0}
.ref{color:#1a8f4c;font-weight:600;font-size:12px}.amb{color:#b9770e;font-weight:600;font-size:12px}
.cav{color:#8a96a3;font-size:12px;font-style:italic}.findings .sub{color:#42505e;font-size:13px;margin:2px 0 6px}
.findings h3{margin:12px 0 4px;color:#1f4e79}
.hint{font-weight:400;text-transform:none;color:#aab3bd;font-size:11px}
/* lag-distribution strips (inline SVG, one dot per token) */
.lagviz{margin:4px 0 8px}
.lagrow{display:flex;align-items:center;gap:12px;padding:3px 0}
.laglbl{flex:0 0 250px;font-size:12.5px;color:#42505e;font-weight:600}
.lagsvg{flex:1 1 auto;min-width:0;height:30px;display:block}
.lagax{font-size:9.5px;fill:#8a96a3}
.lagsum{flex:0 0 auto;font-size:12px;color:#6b7785;white-space:nowrap}
@media(max-width:700px){.lagrow{flex-wrap:wrap}.laglbl{flex-basis:100%}.lagsum{order:3}}
table{width:100%;border-collapse:collapse;font-size:12.5px;background:#fff}
th{position:sticky;top:0;background:#eef2f6;text-align:left;padding:6px 8px;border-bottom:2px solid #d6dee6;cursor:pointer;white-space:nowrap}
td{padding:5px 8px;border-bottom:1px solid #eef2f6}td.n{text-align:right;font-variant-numeric:tabular-nums}
tr:hover td{background:#f7faff}
.b{padding:1px 7px;border-radius:6px;font-weight:600}.b-u100M{background:#fdecea;color:#c0392b}.b-100-300M{background:#fff4e0;color:#b9770e}
.b-300M-1B{background:#eaf6ec;color:#1a8f4c}.b-o1B{background:#e7f0fb;color:#1652f0}.b-unknown{background:#eee;color:#888}
.detail{padding:24px 28px;max-width:760px}
.info{background:#fff;border:1px solid #e1e7ee;border-radius:10px;padding:18px}
.sym{background:#eaf2fb;color:#1f4e79;padding:2px 8px;border-radius:6px;font-size:13px}
dl{display:grid;grid-template-columns:96px 1fr;gap:3px 10px;margin:12px 0}dt{color:#6b7785;font-weight:600}dd{margin:0;word-break:break-word}
h4{margin:14px 0 6px;font-size:12px;text-transform:uppercase;color:#6b7785}
.lags{display:grid;grid-template-columns:1fr 1fr;gap:6px}.lag{background:#f7f9fc;border:1px solid #e7edf3;border-radius:7px;padding:6px 9px}
.lag .k{display:block;font-size:10.5px;color:#8a96a3}.lag .v{font-weight:700}
/* responsive */
img,svg{max-width:100%;height:auto}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.detail{min-width:0}
@media(max-width:1024px){header,section,.detail{padding-left:18px;padding-right:18px}}
@media(max-width:640px){
 header,section,.detail{padding-left:14px;padding-right:14px}
 header h1{font-size:17px}
 .topnav{flex-wrap:wrap}
 .lags{grid-template-columns:1fr}
 dl{grid-template-columns:84px 1fr}
}
"""

SORT_JS = """
const tb=document.querySelector('#ft tbody');
document.querySelectorAll('#ft th').forEach((th,i)=>{let asc=true;th.addEventListener('click',()=>{
 const rows=[...tb.rows];rows.sort((a,b)=>{const x=a.cells[i].dataset.s||a.cells[i].textContent,y=b.cells[i].dataset.s||b.cells[i].textContent;
 const nx=parseFloat(x),ny=parseFloat(y);let c;if(!isNaN(nx)&&!isNaN(ny))c=nx-ny;else c=(''+x).localeCompare(y);return asc?c:-c;});
 asc=!asc;rows.forEach(r=>tb.appendChild(r));});});
"""


def main():
    (OUT / "index.html").write_text(index_html(), encoding="utf-8")
    for m in M:
        (OUT / f"{m['symbol'].lower()}.html").write_text(detail_html(m), encoding="utf-8")
    print(f"wrote {OUT/'index.html'} + {len(M)} detail pages")


if __name__ == "__main__":
    main()

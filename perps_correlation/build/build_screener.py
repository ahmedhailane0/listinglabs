"""Build the Screener tab — the manipulated-coin Binance-perp screener.

Reads cache/screener/screener.json (produced hourly off-site by
fetch/fetch_screener.py — Binance 451s CI, so it runs on the always-on box) and
renders Listinglabs/screener/ in the Manipulated report's look: a header gate
stat, segmented filter tabs (OI / Watchlist / Strategy / FDV), and a tile grid +
sortable list where each coin shows combined Binance+Bybit OI, FDV, OI/FDV%,
funding, and a Buy v1/v2/v3 badge when a signal fired in the last 72h.

    python build_screener.py        # reads cache/screener/screener.json
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # lib./build. importable
from build.build_listing_report import CSS as RCSS, page_meta, build_stamp, sibling_counts
from lib.listing_chart import fmt_usd_compact

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/
SITE = HERE / "Listinglabs" / "screener"
DATA = HERE.parent / "cache" / "screener" / "screener.json"

STRATS = ("v1", "v2", "v3")
STRAT_NAME = {"v1": "Buy v1", "v2": "Buy v2", "v3": "Buy v3"}
STRAT_TITLE = {"v1": "High-control accumulation breakout",
               "v2": "OI + EMA golden cross",
               "v3": "Washout reversal"}


def _usd(v) -> str:
    return fmt_usd_compact(v) if v else "—"


def _pct(v, dp=1) -> str:
    return f"{v:.{dp}f}%" if v is not None else "—"


def _ago(t, as_of) -> str:
    if not t or not as_of:
        return ""
    h = max(0, int((as_of - t) // 3600))
    return "now" if h == 0 else f"{h}h ago"


def _fired_strats(rec) -> list[str]:
    sig = rec.get("signals") or {}
    return [s for s in STRATS if (sig.get(s) or {}).get("fired")]


def _buy_badges(rec, as_of) -> str:
    sig = rec.get("signals") or {}
    out = []
    for s in STRATS:
        d = sig.get(s) or {}
        if not d.get("fired"):
            continue
        stop = d.get("stop")
        tip = (f"{STRAT_TITLE[s]} — fired {_ago(d.get('t'), as_of)}; "
               f"size {d.get('position') or '—'}"
               + (f", stop {stop:.6g}" if stop else ""))
        out.append(f'<span class="buy {s}" title="{html.escape(tip)}">'
                   f'{STRAT_NAME[s]} <i>{_ago(d.get("t"), as_of)}</i></span>')
    return "".join(out)


def _gate_badge(rec) -> str:
    if rec.get("pass_gate"):
        return (f'<span class="gate ok" title="(Binance+Bybit OI) / FDV '
                f'= {rec.get("oi_fdv_pct"):.1f}% ≥ 8%">gate ✓</span>')
    return ""


def _wl(rec) -> str:
    return "manip" if "tradingview" in (rec.get("sources") or []) else "other"


def _strat_attr(rec) -> str:
    return "|" + "|".join(_fired_strats(rec)) + "|"


# ── tiles + list rows ─────────────────────────────────────────────────────────
def _tile(sym, rec) -> str:
    if rec.get("data") == "no_perp":
        return (f'<div class="tile na" data-search="{sym.lower()}" data-oi="0" '
                f'data-fdvnum="-1" data-wl="{_wl(rec)}" data-strat="|">'
                f'<div class="tile-body"><div class="tile-head"><span class="sym">'
                f'{html.escape(sym)}</span><span class="na-tag">not screenable</span></div>'
                f'<div class="tile-meta"><span>no Binance USDT perp</span></div></div></div>')
    oi = rec.get("oi_combined")
    fdv = rec.get("fdv")
    fund = rec.get("funding")
    href = f"https://www.binance.com/en/futures/{html.escape(sym)}USDT"
    return f"""
    <a class="tile" href="{href}" target="_blank" rel="noopener"
       data-search="{html.escape(sym.lower())}" data-oi="{oi or 0:.0f}"
       data-fdvnum="{fdv if fdv else -1:.0f}" data-wl="{_wl(rec)}" data-strat="{_strat_attr(rec)}">
      <div class="tile-body">
        <div class="tile-head"><span class="sym">{html.escape(sym)}</span>
          {_buy_badges(rec, rec.get('as_of'))}{_gate_badge(rec)}</div>
        <div class="tile-meta">
          <span><b>OI</b> {_usd(oi)}</span>
          <span><b>FDV</b> {_usd(fdv)}</span>
          <span><b>OI/FDV</b> {_pct(rec.get('oi_fdv_pct'))}</span>
          <span><b>Fund</b> {_pct(fund * 100, 3) if fund is not None else '—'}</span>
        </div>
      </div>
    </a>"""


LIST_COLS = ["#", "Coin", "OI (BN+BYB)", "FDV", "OI/FDV %", "Funding", "Signals"]
_NEG = "-1e18"


def _num(v, kind="usd") -> str:
    if v is None:
        return f'<td class="n" data-s="{_NEG}">—</td>'
    if kind == "usd":
        return f'<td class="n" data-s="{v:.0f}">{_usd(v)}</td>'
    if kind == "pct":
        return f'<td class="n" data-s="{v:.4f}">{v:.1f}%</td>'
    if kind == "fund":
        return f'<td class="n" data-s="{v:.8f}">{v * 100:.3f}%</td>'
    return f'<td class="n">{v}</td>'


def _list_row(sym, rec) -> str:
    na = rec.get("data") == "no_perp"
    fired = _fired_strats(rec)
    sig_txt = (" ".join(f'<span class="buy {s} mini" title="{html.escape(STRAT_TITLE[s])}">'
                        f'{STRAT_NAME[s]}</span>' for s in fired)
               or ('<span class="na-tag">not screenable</span>' if na else "—"))
    sig_sort = len(fired)
    coin = (f'<td class="tok" data-s="{sym.lower()}"><span class="lname">'
            f'<span class="sym">{html.escape(sym)}</span></span>{_gate_badge(rec)}</td>')
    return (
        f'<tr class="lrow" data-search="{html.escape(sym.lower())}" '
        f'data-oi="{rec.get("oi_combined") or 0:.0f}" '
        f'data-fdvnum="{rec.get("fdv") if rec.get("fdv") else -1:.0f}" '
        f'data-wl="{_wl(rec)}" data-strat="{_strat_attr(rec)}">'
        f'<td class="rank"></td>{coin}'
        f'{_num(rec.get("oi_combined"))}{_num(rec.get("fdv"))}'
        f'{_num(rec.get("oi_fdv_pct"), "pct")}'
        f'{_num(rec.get("funding"), "fund") if rec.get("funding") is not None else _num(None)}'
        f'<td class="sig" data-s="{sig_sort}">{sig_txt}</td></tr>')


def _filter_bar() -> str:
    def seg(name, opts):
        btns = "".join(
            f'<button class="seg{" active" if i == 0 else ""}" data-filter="{name}" '
            f'data-value="{v}">{html.escape(lbl)}</button>'
            for i, (v, lbl) in enumerate(opts))
        return f'<span class="segrow"><span class="flabel">{name.upper()}</span>{btns}</span>'
    return f"""
<div class="filters">
  <input id="search" type="search" placeholder="Search coin…" autocomplete="off">
  {seg("oi", [("all", "All"), ("5m", "&gt;$5M"), ("10m", "&gt;$10M")])}
  {seg("wl", [("all", "All"), ("manip", "Manip")])}
  {seg("strat", [("all", "All"), ("v1", "Buy v1"), ("v2", "Buy v2"), ("v3", "Buy v3")])}
  {seg("fdv", [("all", "All"), ("lt150", "&lt;$150M"), ("gte150", "≥$150M")])}
  <span class="viewtoggle"><button id="view-grid" type="button" class="active">▦ Thumbnails</button>
    <button id="view-list" type="button">☰ List</button></span>
  <span id="count" class="count"></span>
</div>"""


JS = """
<script>
const tiles=[...document.querySelectorAll('.tile')];
const rows=[...document.querySelectorAll('.lrow')];const items=[...tiles,...rows];
const search=document.getElementById('search'),count=document.getElementById('count');
const F={oi:'all',wl:'all',strat:'all',fdv:'all'};
function ok(el){
  const q=search.value.trim().toLowerCase();
  if(q && !el.dataset.search.includes(q)) return false;
  const oi=parseFloat(el.dataset.oi||'0');
  if(F.oi==='5m' && !(oi>5e6)) return false;
  if(F.oi==='10m' && !(oi>1e7)) return false;
  if(F.wl==='manip' && el.dataset.wl!=='manip') return false;
  if(F.strat!=='all' && !(el.dataset.strat||'').includes('|'+F.strat+'|')) return false;
  const fdv=parseFloat(el.dataset.fdvnum||'-1');
  if(F.fdv==='lt150' && !(fdv>=0 && fdv<150e6)) return false;
  if(F.fdv==='gte150' && !(fdv>=150e6)) return false;
  return true;
}
function apply(){
  for(const el of items) el.style.display=ok(el)?'':'none';
  count.textContent=tiles.filter(ok).length+' / '+tiles.length+' coins';
}
search.addEventListener('input',apply);
document.querySelectorAll('.seg').forEach(b=>b.addEventListener('click',()=>{
  const f=b.dataset.filter;F[f]=b.dataset.value;
  b.parentElement.querySelectorAll('.seg').forEach(x=>x.classList.toggle('active',x===b));
  apply();
}));
const bG=document.getElementById('view-grid'),bL=document.getElementById('view-list');
const views=document.getElementById('views');const VK='ll-view-screener';
function setView(v,p){views.className='view-'+v;bG.classList.toggle('active',v=='grid');
  bL.classList.toggle('active',v=='list');if(p){try{localStorage.setItem(VK,v);}catch(e){}}}
bG.addEventListener('click',()=>setView('grid',true));bL.addEventListener('click',()=>setView('list',true));
let _v='grid';try{_v=localStorage.getItem(VK)||'grid';}catch(e){}setView(_v=='list'?'list':'grid',false);
const ltab=document.getElementById('ltab');
if(ltab){const tb=ltab.querySelector('tbody');
 ltab.querySelectorAll('th').forEach((th,i)=>{let asc=false;th.tabIndex=0;
  th.addEventListener('click',()=>{const rs=[...tb.rows];
   rs.sort((a,b)=>{const x=a.cells[i].dataset.s??a.cells[i].textContent,y=b.cells[i].dataset.s??b.cells[i].textContent;
   const nx=parseFloat(x),ny=parseFloat(y);const c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:(''+x).localeCompare(y);return asc?c:-c;});
   asc=!asc;ltab.querySelectorAll('th').forEach(h=>h.classList.remove('sorted'));th.classList.add('sorted');
   rs.forEach(r=>tb.appendChild(r));});});}
apply();
</script>"""


EXTRA_CSS = """
.segrow{display:inline-flex;align-items:center;gap:0;margin-right:10px}
.segrow .flabel{font-size:11px;color:#6b7785;font-weight:600;margin-right:6px;text-transform:uppercase;letter-spacing:.04em}
.seg{font:inherit;font-size:12px;border:1px solid #d7dee6;background:#fff;color:#42505e;
  padding:4px 9px;cursor:pointer;border-right:0}
.seg:first-of-type{border-radius:7px 0 0 7px}.seg:last-of-type{border-radius:0 7px 7px 0;border-right:1px solid #d7dee6}
.seg.active{background:#1f4e79;color:#fff;border-color:#1f4e79}
.buy{display:inline-block;border-radius:9px;font-size:10.5px;font-weight:700;padding:1px 8px;
  white-space:nowrap;margin-left:5px;cursor:help;color:#fff}
.buy i{font-style:normal;opacity:.8;font-weight:600}
.buy.v1{background:#1e7a46}.buy.v2{background:#1f4e79}.buy.v3{background:#9b2d8f}
.buy.mini{font-size:10px;margin:0 3px 0 0}
.gate{display:inline-block;border-radius:9px;font-size:10px;font-weight:700;padding:1px 7px;margin-left:5px}
.gate.ok{background:#eaf6ee;color:#1e7a46}
.tile.na{opacity:.55;border-style:dashed}
.na-tag{font-size:10.5px;color:#8a96a3;font-weight:600;margin-left:auto}
#ltab{table-layout:fixed;min-width:760px}
#ltab th:nth-child(1){width:4%}
#ltab th:nth-child(2){width:18%;text-align:left}
#ltab th:nth-child(3),#ltab th:nth-child(4),#ltab th:nth-child(5),#ltab th:nth-child(6){width:13%}
#ltab th:nth-child(7){width:26%;text-align:left}
td.sig{text-align:left}
.tile-head .sym{font-weight:700;font-size:15px}
"""


def _index(data) -> str:
    tokens = data.get("tokens", {})
    c = data.get("counts", {})
    # order: gate-passers first, then by combined OI desc, not-screenable last.
    def key(item):
        _s, r = item
        na = r.get("data") == "no_perp"
        return (na, not r.get("pass_gate"), -(r.get("oi_combined") or 0))
    ordered = sorted(tokens.items(), key=key)
    tiles = "\n".join(_tile(s, r) for s, r in ordered)
    head = "".join(f"<th>{html.escape(x)}</th>" for x in LIST_COLS)
    rows = "\n".join(_list_row(s, r) for s, r in ordered)

    reactions_n = len(list((HERE / "listings").glob("*.json")))
    funnel_n, scams_n = sibling_counts()
    react_lbl = f"Binance Alpha &amp; Perps ({reactions_n})" if reactions_n else "Binance Alpha &amp; Perps"
    fun_lbl = f"CEX → Korea ({funnel_n})" if funnel_n else "CEX → Korea"
    scam_lbl = f"Manipulated ({scams_n})" if scams_n else "Manipulated"

    as_of = data.get("as_of_hour")
    as_of_txt = (datetime.fromtimestamp(as_of, tz=timezone.utc).strftime("%Y-%m-%d %H:00")
                 if as_of else "—")
    statline = (f'{c.get("screenable", 0)} coins screened · '
                f'<b>{c.get("passing_gate", 0)}</b> pass (BN+BYB OI)/FDV ≥ 8% · '
                f'Buy v1 <b>{c.get("v1", 0)}</b> · v2 <b>{c.get("v2", 0)}</b> · '
                f'v3 <b>{c.get("v3", 0)}</b> · signals as of {as_of_txt} UTC')
    body = f"""
<header><h1>Screener</h1>
<nav class="topnav"><a href="../report/index.html">{react_lbl}</a>
<a href="../funnel/report/index.html">{fun_lbl}</a>
<a href="../scams/index.html">{scam_lbl}</a>
<a class="active" href="index.html">Screener ({c.get('screenable', 0)})</a></nav>
<p>{statline}</p>
<p class="sub">Hourly Binance-perp screener for manipulated coins. Buy v1/v2/v3 fire on
open-interest + price + funding setups (see each badge). Not financial advice.</p></header>
{_filter_bar()}
<div id="views" class="view-grid">
  <main class="grid">{tiles}</main>
  <div class="listwrap"><table class="list" id="ltab"><thead><tr>{head}</tr></thead>
  <tbody>{rows}</tbody></table></div>
</div>
{JS}"""
    desc = ("Manipulated-coin Binance-perp screener: combined Binance+Bybit open interest "
            "vs FDV, funding, and Buy v1/v2/v3 accumulation/washout signals, refreshed hourly.")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{page_meta("Screener — ListingLabs", desc)}'
            f'<title>Screener</title><link rel="stylesheet" href="style.css"></head>'
            f'<body>{body}</body></html>')


def main():
    SITE.mkdir(parents=True, exist_ok=True)
    if not DATA.exists():
        # Build a placeholder so the tab + nav exist even before the box's first push.
        data = {"tokens": {}, "counts": {}, "as_of_hour": None}
        print(f"{DATA} missing — wrote an empty Screener page (run fetch/fetch_screener.py on the box)")
    else:
        data = json.loads(DATA.read_text(encoding="utf-8"))
    (SITE / "style.css").write_text(RCSS + EXTRA_CSS, encoding="utf-8")
    (SITE / "index.html").write_text(_index(data), encoding="utf-8")
    print(f"wrote {SITE/'index.html'} ({data.get('counts', {}).get('screenable', 0)} coins)")


if __name__ == "__main__":
    main()

"""Build the Scam Watchlist tab — rendered like the Listing Reactions report,
but for the scam-watchlist tokens (data gathered by fetch_scam_data.py).

Same look as reactions: header + topnav, a filter bar (search + Thumbnails/List
toggle + FDV>$1B filter), a tile grid with price sparklines, a sortable list
view (with row numbers), and a per-token detail page with an interactive price
chart + stats + funding + OI + the (English-translated) memo. Chart links are
embedded, never shown as raw URLs.

    python build_scams.py        # reads cache/scam_data.json + cache/scam_prices/
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import plotly.graph_objects as go

from build_listing_report import CSS as RCSS          # reuse reactions styling
from build_listing_report import _num_cell, _pct, _NEG_INF  # reuse reactions list/stat cells
from build_funding import _investors_from_item, _excel_amounts  # same funding source as reactions
from listing_chart import fmt_usd_compact, fmt_subscript_price
from interactive_chart import _autofit_js

HERE = Path(__file__).parent
SITE = HERE / "Listinglabs" / "scams"
DATA = HERE.parent / "cache" / "scam_data.json"
PRICES = HERE.parent / "cache" / "scam_prices"
ROOTDATA = HERE.parent / "cache" / "rootdata.json"

# symbol(upper) -> {"amount", "source", "investors": [...]}. Built in main() from
# the same RootData + Excel caches the Listing Reactions report uses, so funding
# is sourced identically across both reports. Falls back to scam_data's funding.
FUNDING: dict[str, dict] = {}


def _load_funding(symbols) -> dict:
    """Funding map for the watchlist symbols, from cache/rootdata.json (amount +
    investors) with the Excel amount-only fallback — mirrors build_funding.py."""
    rd = json.loads(ROOTDATA.read_text(encoding="utf-8")) if ROOTDATA.exists() else {}
    excel = _excel_amounts()
    out = {}
    for sym in symbols:
        item = (rd.get(sym) or {}).get("item") or {}
        amount = item.get("total_funding")
        investors = _investors_from_item(item)
        source = "rootdata" if amount else None
        if not amount and sym in excel:
            amount, source = excel[sym], "excel"
        if amount or investors:
            out[sym] = {"amount": amount, "source": source, "investors": investors}
    return out

_SW, _SH = 100.0, 32.0


def _sparkline(sym: str) -> str:
    p = PRICES / f"{sym}.json"
    if not p.exists():
        return ""
    series = json.loads(p.read_text(encoding="utf-8"))
    if len(series) < 2:
        return ""
    step = max(1, len(series) // 120)
    vals = [v for _t, v in series[::step]]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    x = lambda i: round(i / (n - 1) * _SW, 2)
    y = lambda v: round(_SH - (v - lo) / span * (_SH - 2) - 1, 2)
    line = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(vals))
    area = f"0,{_SH} {line} {_SW},{_SH}"
    return (f'<svg class="thumb" viewBox="0 0 {_SW:g} {_SH:g}" preserveAspectRatio="none" '
            f'aria-hidden="true"><polygon class="spark-fill" points="{area}"/>'
            f'<polyline class="spark-line" points="{line}"/></svg>')


def _price_chart(sym: str, name: str) -> str:
    p = PRICES / f"{sym}.json"
    if not p.exists():
        return '<div class="missing">no price data</div>'
    series = json.loads(p.read_text(encoding="utf-8"))
    if len(series) < 2:
        return '<div class="missing">no price data</div>'
    import datetime as dt
    xs = [dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc) for t, _v in series]
    ys = [v for _t, v in series]
    ref = min([v for v in ys if v > 0] or [1])
    import math
    dec = max(4, 2 - math.floor(math.log10(ref))) if ref > 0 else 4
    pfmt = f".{dec}f"
    fig = go.Figure(go.Scatter(
        x=xs, y=ys, mode="lines", line=dict(color="#1f4e79", width=2),
        fill="tozeroy", fillcolor="rgba(31,78,121,0.07)",
        hovertemplate="%{x|%b %d %Y}  <b>$%{y:" + pfmt + "}</b><extra></extra>"))
    fig.update_layout(
        height=520, margin=dict(l=58, r=24, t=20, b=40),
        font=dict(family="Segoe UI, -apple-system, Roboto, sans-serif", size=12, color="#1d2733"),
        paper_bgcolor="white", plot_bgcolor="white", template="plotly_white",
        xaxis=dict(rangeslider=dict(visible=False), showgrid=False, showline=True,
                   linecolor="#e1e7ee", ticks="outside", tickcolor="#e1e7ee",
                   tickfont=dict(size=11), showspikes=True, spikemode="across",
                   spikethickness=1, spikedash="solid", spikecolor="#c5ccd3"),
        yaxis=dict(title=dict(text="Price (USD)", font=dict(size=11, color="#6b7785")),
                   tickprefix="$", tickformat=pfmt, gridcolor="#eef2f6", zeroline=False,
                   tickfont=dict(size=11)),
        hoverlabel=dict(bgcolor="white", bordercolor="#e1e7ee",
                        font=dict(size=12, color="#1d2733")),
        hovermode="x", showlegend=False, dragmode="pan")
    div_id = f"chart-{sym.lower()}"
    snippet = fig.to_html(full_html=False, include_plotlyjs=False, div_id=div_id,
                          config={"displayModeBar": False, "scrollZoom": True})
    # Same interaction as the reactions charts: y-axis auto-fits the x-range in view
    # on zoom/pan (shared engine), so the watchlist feels like one product.
    return snippet + _autofit_js(div_id)


def _usd(v):
    return fmt_usd_compact(v) if v else "—"


# CoinGecko platform key -> token explorer base (CG uses long chain names).
EXPLORERS = {
    "ethereum": "https://etherscan.io/token/",
    "binance-smart-chain": "https://bscscan.com/token/",
    "base": "https://basescan.org/token/",
    "solana": "https://solscan.io/token/",
    "polygon-pos": "https://polygonscan.com/token/",
    "arbitrum-one": "https://arbiscan.io/token/",
    "optimistic-ethereum": "https://optimistic.etherscan.io/token/",
    "avalanche": "https://snowtrace.io/token/",
    "the-open-network": "https://tonviewer.com/",
}


def _explorer(chain, contract):
    base = EXPLORERS.get(chain or "")
    return base + contract if (base and contract) else None


def _links(rec) -> str:
    """Project + data-source references, mirroring the Listing Reactions report:
    Website / X first, then CoinGecko / CoinMarketCap / chain explorer."""
    project, data = [], []
    web = rec.get("website")
    if web:
        project.append(f'<a href="{html.escape(web)}" target="_blank" rel="noopener">Website ↗</a>')
    tw = rec.get("twitter")
    if tw:
        h = tw.lstrip("@")
        project.append(f'<a href="https://x.com/{html.escape(h)}" target="_blank" rel="noopener">X (@{html.escape(h)}) ↗</a>')
    cg = rec.get("chart_link") or (f"https://www.coingecko.com/en/coins/{rec['cg_id']}"
                                   if rec.get("cg_id") else "")
    if cg.startswith("http"):
        data.append(f'<a href="{html.escape(cg)}" target="_blank" rel="noopener">CoinGecko</a>')
    slug = rec.get("cmc_slug")
    if slug:
        data.append(f'<a href="https://coinmarketcap.com/currencies/{html.escape(slug)}/" '
                    f'target="_blank" rel="noopener">CoinMarketCap</a>')
    exp = _explorer(rec.get("chain"), rec.get("contract"))
    if exp:
        data.append(f'<a href="{html.escape(exp)}" target="_blank" rel="noopener">Contract ↗</a>')
    blocks = []
    if project:
        blocks.append(f'<div class="links project">{" · ".join(project)}</div>')
    if data:
        blocks.append(f'<div class="links">{" · ".join(data)}</div>')
    return "".join(blocks) or '<div class="links note">no references on file</div>'


def _oi_str(rec) -> str:
    """OI value + % of mcap, or an explicit 'no perp market' when we checked CMC
    and found none (vs. '—' when the token was never resolved to a CMC slug)."""
    oi, pct = rec.get("oi_usd"), rec.get("oi_pct_mcap")
    if oi:
        return f"{_usd(oi)} · {pct:.0f}% of mcap" if pct else _usd(oi)
    # an unvalidated identity means the CMC slug is untrustworthy — don't claim
    # "no perp market" off the wrong coin.
    if rec.get("cmc_slug") and rec.get("resolved", True):
        return '<span class="note">no perp market</span>'
    return "—"


def _series(sym):
    p = PRICES / f"{sym}.json"
    if not p.exists():
        return []
    s = json.loads(p.read_text(encoding="utf-8"))
    return [pt for pt in s if pt and pt[1]]  # drop null prices


def _perf(rec):
    """Price-reaction metrics computed from the token's own 180-day daily price
    history — the watchlist analogue of metrics.reaction() for the reactions
    report. 'Since' = change over the full available window (no listing event to
    anchor to). Returns None when there isn't enough history."""
    s = _series(rec["symbol"])
    if len(s) < 2:
        return None
    last_t, last = s[-1]
    first = s[0][1]
    DAY = 86400_000

    def chg(n_days):
        target = last_t - n_days * DAY
        base = next((p[1] for p in reversed(s) if p[0] <= target), None)
        return (last / base - 1) * 100 if base else None

    prices = [p[1] for p in s]
    ath, atl = max(prices), min(prices)
    peak, mdd = -1.0, 0.0
    for _t, v in s:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v / peak - 1) * 100)
    return {
        "since": (last / first - 1) * 100 if first else None,
        "p24": chg(1), "p7": chg(7), "p30": chg(30), "p90": chg(90),
        "launch": first, "last": last, "ath": ath, "atl": atl,
        "dd": mdd, "peak_gain": (ath / first - 1) * 100 if first else None,
    }


def _investor_links(invs, limit=8):
    out = []
    for i in invs[:limit]:
        nm = html.escape(i.get("name", ""))
        lead = ' <span class="lead">(lead)</span>' if i.get("lead") else ""
        out.append(f'<a href="{html.escape(i["url"])}" target="_blank" rel="noopener">{nm}</a>{lead}'
                   if i.get("url") else f"{nm}{lead}")
    return out


def _fund_rec(rec):
    """Prefer the shared RootData/Excel funding map; fall back to scam_data's."""
    return FUNDING.get(rec["symbol"].upper()) or rec.get("funding") or {}


def _funding_str(rec):
    f = _fund_rec(rec)
    amt, invs = f.get("amount"), f.get("investors") or []
    if not amt and not invs:
        return "—", ""
    src = f.get("source")
    src_tag = f' <span class="src">via {html.escape(src)}</span>' if src else ""
    inv = ""
    if invs:
        more = f" +{len(invs) - 8} more" if len(invs) > 8 else ""
        inv = f'<div class="note">Backed by: {", ".join(_investor_links(invs))}{more}</div>'
    return (f"{_usd(amt) if amt else '—'}{src_tag}"), inv


def _tile(rec) -> str:
    sym = rec["symbol"]
    search = html.escape(f"{rec.get('name', sym)} {sym}".lower())
    fdv = rec.get("fdv") or rec.get("csv_fdv")
    mc = rec.get("mcap") or rec.get("csv_mc")
    px = rec.get("price") or rec.get("csv_price")
    return f"""
    <a class="tile" href="{sym.lower()}.html" data-venues="||" data-search="{search}" data-fdv="{fdv or 0:.0f}">
      {_sparkline(sym)}
      <div class="tile-body">
        <div class="tile-head"><span class="name">{html.escape(rec.get('name', sym))}</span>
          <span class="sym">{html.escape(sym)}</span></div>
        <div class="tile-meta">
          <span><b>Price</b> {fmt_subscript_price(px) if px else '—'}</span>
          <span><b>MC</b> {_usd(mc)}</span>
          <span><b>FDV</b> {_usd(fdv)}</span>
        </div>
      </div>
    </a>"""


# Same columns as the Listing Reactions list view (performance from price
# history) plus the watchlist-specific Memo (the $1B-FDV behaviour notes).
LIST_COLS = ["#", "Token", "Since", "24h", "7d", "30d", "90d", "FDV", "MC", "OI%", "Funding", "Memo"]


def _funding_cell(rec) -> str:
    """Funding amount cell with the lead/first investor as a subtitle — matches
    the reactions list's funding column."""
    f = _fund_rec(rec)
    amt = f.get("amount")
    invs = f.get("investors") or []
    if not amt and not invs:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    amt_txt = _usd(amt) if amt else "—"
    lead = next((i["name"] for i in invs if i.get("lead")), invs[0]["name"] if invs else "")
    sub = f' <span class="sub">{html.escape(lead)}</span>' if lead else ""
    return f'<td class="n" data-s="{amt or 0:.0f}">{amt_txt}{sub}</td>'


def _list_row(rec) -> str:
    sym = rec["symbol"]
    search = html.escape(f"{rec.get('name', sym)} {sym}".lower())
    fdv = rec.get("fdv") or rec.get("csv_fdv")
    mc = rec.get("mcap") or rec.get("csv_mc")
    oi = rec.get("oi_pct_mcap")
    p = _perf(rec) or {}
    tok = (f'<td class="tok" data-s="{search}"><a href="{sym.lower()}.html">{_sparkline(sym)}'
           f'<span class="lname">{html.escape(rec.get("name", sym))} '
           f'<span class="sym">{html.escape(sym)}</span></span></a></td>')
    memo = html.escape(rec.get("memo_en") or "")
    return (
        f'<tr class="lrow" data-venues="||" data-search="{search}" data-fdv="{fdv or 0:.0f}">'
        f'<td class="rank"></td>{tok}'
        f"{_num_cell(p.get('since'))}{_num_cell(p.get('p24'))}{_num_cell(p.get('p7'))}"
        f"{_num_cell(p.get('p30'))}{_num_cell(p.get('p90'))}"
        f"{_num_cell(fdv, pct=False, color=False)}{_num_cell(mc, pct=False, color=False)}"
        f"{_num_cell(oi, pct=True, color=False)}"
        f"{_funding_cell(rec)}"
        f'<td class="memo"><span>{memo or "—"}</span></td></tr>')


def _reaction_block(rec) -> str:
    """Price-reaction stats + checkpoints, mirroring the reactions detail page,
    computed from the token's 180-day price history."""
    p = _perf(rec)
    if not p:
        return ""
    stats = f"""
    <div class="stat"><span class="k">First (180d)</span><span class="v">{fmt_subscript_price(p['launch'])}</span></div>
    <div class="stat"><span class="k">All-time high</span><span class="v">{fmt_subscript_price(p['ath'])}</span></div>
    <div class="stat"><span class="k">All-time low</span><span class="v">{fmt_subscript_price(p['atl'])}</span></div>
    <div class="stat"><span class="k">Since (180d)</span><span class="v">{_pct(p['since']) if p['since'] is not None else '—'}</span></div>
    <div class="stat"><span class="k">Peak gain</span><span class="v">{_pct(p['peak_gain']) if p['peak_gain'] is not None else '—'}</span></div>
    <div class="stat"><span class="k">Max drawdown</span><span class="v">{_pct(p['dd'])}</span></div>"""
    checks = "".join(
        f'<div class="chk"><span class="k">{lbl}</span><span class="v">{_pct(v)}</span></div>'
        for lbl, v in [("24h", p["p24"]), ("7d", p["p7"]), ("30d", p["p30"]), ("90d", p["p90"])]
        if v is not None)
    checks_block = (f'<h4>Performance checkpoints</h4><div class="checks">{checks}</div>'
                    if checks else "")
    return (f'<h4>Price reaction <span class="asof">trailing 180 days</span></h4>'
            f'<div class="stats">{stats}</div>{checks_block}')


PERP = HERE.parent / "cache" / "perp_markets"
HOLDERS = HERE.parent / "cache" / "scam_holders"


def _load_perp(sym):
    p = PERP / f"{sym.upper()}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _load_holders(sym):
    p = HOLDERS / f"{sym.upper()}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _compact(v):
    """Compact plain-number formatter (token counts): 312.54M, 1.00B."""
    if v is None:
        return "—"
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:.2f}{suf}"
    return f"{v:,.0f}"


def _badge(text, risk):
    fg, bg = ("#c0392b", "#fdecea") if risk else ("#1e7e34", "#eafaf0")
    return f'<span class="badge" style="color:{fg};background:{bg}">{html.escape(text)}</span>'


def _fund_span(x, annual=False):
    """Funding cell coloured by sign (neg = shorts pay longs = red)."""
    if x is None:
        return "—"
    c = "#c0392b" if x < 0 else "#1e7e34"
    txt = f"{x * 100:+.1f}%" if annual else f"{x * 100:+.4f}%"
    return f'<span style="color:{c}">{txt}</span>'


def _supply_dl(rec) -> str:
    """Supply + circulation-ratio + peak-mcap rows for the detail <dl>."""
    circ, tot = rec.get("circ_supply"), rec.get("total_supply")
    mx = rec.get("max_supply")
    denom = tot or mx
    out = []
    if tot or mx:
        out.append(f"<dt>Total supply</dt><dd>{_compact(tot or mx)}"
                   f"{'' if tot else ' <span class=\"src\">(max)</span>'}</dd>")
    if circ is not None:
        pct = f" <span class=\"src\">({circ / denom * 100:.1f}% of supply)</span>" if denom else ""
        out.append(f"<dt>Circulating</dt><dd>{_compact(circ)}{pct}</dd>")
    ratio = rec.get("circ_ratio")
    if ratio is not None:
        p = ratio * 100
        risk = p < 30
        b = _badge(f"{p:.1f}% · {'⚠ <30% (low float)' if risk else '≥30%'}", risk)
        out.append(f"<dt>Circulation ratio</dt><dd>{b}</dd>")
    peak = rec.get("peak_mcap")
    if peak:
        out.append(f'<dt>Peak market cap</dt><dd>{_usd(peak)} '
                   f'<span class="src">≈ ATH price × circ supply</span></dd>')
    return "".join(out)


def _perp_table(perp) -> str:
    if not perp or not perp.get("venues"):
        return ('<div class="missing">No perp markets found on the tracked '
                'exchanges (keyless public APIs).</div>')
    head = ("<tr><th>Exchange</th><th>OI (USD)</th><th>Share</th>"
            "<th>Funding</th><th>Every</th><th>Annualized</th><th>OI/24h vol</th></tr>")
    all_row = (f'<tr class="allrow"><td>All</td><td>{_usd(perp["total_oi_usd"])}</td>'
               f'<td>100%</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>')
    rows = [all_row]
    for v in perp["venues"]:
        iv = f"{v.get('interval_h', 8):g}h"
        ovr = f"{v['oi_vol_ratio']:.3f}" if v.get("oi_vol_ratio") is not None else "—"
        rows.append(
            f'<tr><td class="venue">{html.escape(v["venue"])}</td>'
            f'<td>{_usd(v["oi_usd"])}</td>'
            f'<td>{v["oi_share_pct"]:.1f}%</td>'
            f'<td>{_fund_span(v.get("funding"))}</td><td class="iv">{iv}</td>'
            f'<td>{_fund_span(v.get("funding_annualized"), annual=True)}</td>'
            f'<td>{ovr}</td></tr>')
    return f'<table class="perp"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table>'


def _holders_block(rec) -> str:
    h = _load_holders(rec["symbol"])
    if not h or not h.get("available"):
        chain = html.escape(rec.get("chain") or "this chain")
        return ('<div class="missing">Top-holder data unavailable on '
                f'{chain} — the keyless source covers Ethereum only.</div>')
    tot = rec.get("total_supply") or rec.get("max_supply")
    px = rec.get("price")
    rows = []
    for hd in h["holders"]:
        share = hd["share"]
        toks = (share / 100 * tot) if tot else None
        usd = (toks * px) if (toks and px) else None
        addr = hd.get("address") or ""
        short = f"{addr[:8]}…{addr[-6:]}" if len(addr) > 16 else addr
        link = (f'<a href="https://etherscan.io/address/{html.escape(addr)}" '
                f'target="_blank" rel="noopener" class="mono">{html.escape(short)}</a>')
        rows.append(f'<tr><td>{hd["rank"]}</td><td>{link}</td>'
                    f'<td>{_compact(toks)}</td><td>{_usd(usd)}</td>'
                    f'<td>{share:.2f}%</td></tr>')
    top10, retail = h["top10_share"], h["retail_share"]
    tb = _badge(f"Top-10: {top10:.1f}% · {'⚠ ≥95% (highly concentrated)' if top10 >= 95 else '<95%'}",
                top10 >= 95)
    rb = _badge(f"Retail: {retail:.1f}% · {'⚠ <1% (negligible)' if retail < 1 else '≥1%'}",
                retail < 1)
    head = "<tr><th>#</th><th>Holder</th><th>Tokens</th><th>USD</th><th>Share</th></tr>"
    note = ('<p class="note">Retail = 100% − top-10 holders. Top-10 includes any '
            'CEX/contract/burn wallets (simple definition).</p>')
    return (f'<div class="badges">{tb} {rb}</div>'
            f'<table class="holders"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table>{note}')


def _detail(rec) -> str:
    sym = rec["symbol"]
    name = html.escape(rec.get("name", sym))
    fund_amt, fund_inv = _funding_str(rec)
    perp = _load_perp(sym)
    # OI from the summed per-exchange total (keyless), falling back to the CMC
    # aggregate; ratio is recomputed against that same total so it reconciles.
    oi_total = (perp or {}).get("total_oi_usd") or rec.get("oi_usd")
    mc_for_oi = rec.get("mcap") or rec.get("csv_mc")
    oi_ratio = (oi_total / mc_for_oi * 100) if (oi_total and mc_for_oi) else None
    oi_str = (f'{_usd(oi_total)}'
              + (f' · {oi_ratio:.0f}% of mcap' if oi_ratio else '')) if oi_total else _oi_str(rec)
    # honest freshness stamp from the perp snapshot (this data is refreshed by
    # fetch_perp_markets.py, not in the page build, so label when it was pulled).
    import datetime as _dt
    _ts = (perp or {}).get("fetched_at")
    perp_asof = (f"as of {_dt.datetime.fromtimestamp(_ts, _dt.timezone.utc):%Y-%m-%d %H:%M} UTC"
                 if _ts else "keyless public exchange APIs")
    warn = ("" if rec.get("resolved", True) else
            '<div class="cat" style="background:#fdecea;color:#c0392b">⚠ identity auto-matched by symbol — verify</div>')
    memo = html.escape(rec.get("memo_en") or "—")
    days = html.escape(rec.get("days") or "—")
    mc, fdv = rec.get("mcap") or rec.get("csv_mc"), rec.get("fdv") or rec.get("csv_fdv")
    fdvmc = f"<dt>FDV / MC</dt><dd>{fdv / mc:.1f}×</dd>" if (mc and fdv) else ""
    body = f"""
<header><a class="back" href="index.html">← all watchlist tokens</a></header>
<main><section class="card">
  <div class="info">
    <h2>{name} <span class="sym">{html.escape(sym)}</span></h2>
    {warn}
    {_links(rec)}
    <dl>
      <dt>Chain</dt><dd>{html.escape(rec.get('chain') or '—')}</dd>
      <dt>Contract</dt><dd class="mono">{html.escape(rec.get('contract') or '—')}</dd>
      <dt>Price</dt><dd>{fmt_subscript_price(rec['price']) if rec.get('price') else '—'}</dd>
      <dt>Market cap</dt><dd>{_usd(mc)}</dd>
      <dt>FDV</dt><dd>{_usd(fdv)}</dd>
      {fdvmc}
      <dt>Volume (24h)</dt><dd>{_usd(rec.get('vol') or rec.get('csv_vol'))}</dd>
      {_supply_dl(rec)}
      <dt>Open interest</dt><dd>{oi_str}</dd>
      <dt>Funding</dt><dd>{fund_amt}{fund_inv}</dd>
      <dt>Days &gt;$1B</dt><dd>{days}</dd>
      <dt>Memo</dt><dd class="note">{memo}</dd>
    </dl>
    {_reaction_block(rec)}
  </div>
  <div class="chart">{_price_chart(sym, name)}</div>
</section>
<section class="card span">
  <h3>Perp markets <span class="asof">open interest &amp; funding · per exchange · {perp_asof}</span></h3>
  {_perp_table(perp)}
</section>
<section class="card span">
  <h3>Top holders <span class="asof">on-chain distribution</span></h3>
  {_holders_block(rec)}
</section></main>"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{name} ({html.escape(sym)}) — Scam Watchlist</title>'
            f'<style>{RCSS}{EXTRA_CSS}</style>'
            f'<script src="../report/plotly.min.js"></script></head><body>{body}</body></html>')


def _filter_bar(n) -> str:
    return f"""
<div class="filters">
  <input id="search" type="search" placeholder="Search token…" autocomplete="off">
  <label class="fdv"><input type="checkbox" id="fdvchk"> FDV &gt; $
    <input type="number" id="fdvval" value="1" step="0.5" min="0" style="width:54px"> B</label>
  <span class="viewtoggle"><button id="view-grid" type="button" class="active">▦ Thumbnails</button>
    <button id="view-list" type="button">☰ List</button></span>
  <span id="count" class="count"></span>
</div>"""


def _index(recs) -> str:
    tiles = "\n".join(_tile(r) for r in recs)
    head = "".join(f'<th data-i="{i}">{html.escape(c)}</th>' for i, c in enumerate(LIST_COLS))
    rows = "\n".join(_list_row(r) for r in recs)
    body = f"""
<header><h1>Scam Watchlist</h1>
<nav class="topnav"><a href="../report/index.html">Listing Reactions</a>
<a href="../funnel/report/index.html">Listing Funnel</a>
<a class="active" href="index.html">Scam Watchlist ({len(recs)})</a></nav>
<p>{len(recs)} tokens · price, MC, FDV, OI &amp; funding · notes on $1B-FDV behaviour</p></header>
{_filter_bar(len(recs))}
<div id="views" class="view-grid">
  <main class="grid">{tiles}</main>
  <div class="listwrap"><table class="list" id="ltab"><thead><tr>{head}</tr></thead>
  <tbody>{rows}</tbody></table></div>
</div>
{JS}"""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Scam Watchlist</title><style>{RCSS}{EXTRA_CSS}</style></head>'
            f'<body>{body}</body></html>')


EXTRA_CSS = """
.fdv{font-size:13px;color:#42505e;display:inline-flex;align-items:center;gap:6px}
.links.note{color:#8a96a3;font-style:italic}
/* deterministic column widths (12 cols: #, Token, Since, 24h, 7d, 30d, 90d,
   FDV, MC, OI%, Funding, Memo). Fixed layout reads widths from the header row. */
#ltab{table-layout:fixed}
#ltab th{overflow:hidden}
#ltab th:nth-child(1){width:3%}
#ltab th:nth-child(2){width:18%;text-align:left}
#ltab th:nth-child(3),#ltab th:nth-child(4),#ltab th:nth-child(5),
#ltab th:nth-child(6),#ltab th:nth-child(7){width:6%}
#ltab th:nth-child(8),#ltab th:nth-child(9){width:8%}
#ltab th:nth-child(10){width:6%}
#ltab th:nth-child(11){width:10%}
#ltab th:nth-child(12){width:13%;text-align:left}
#ltab td{overflow:hidden}
#ltab td.rank{text-align:center}
#ltab td.memo{max-width:none;white-space:normal;font-size:12px;color:#42505e}
#ltab td.memo span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* per-token detail: full-width sections for perp + holders tables */
section.card.span{display:block;margin-top:18px}
section.card.span h3{margin:0 0 12px;font-size:15px;color:#1d2733}
section.card.span h3 .asof{font-size:12px;color:#8a96a3;font-weight:400;margin-left:8px}
.badge{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;font-weight:600;white-space:nowrap}
.badges{margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap}
table.perp,table.holders{width:100%;border-collapse:collapse;font-size:13px}
table.perp th,table.holders th{text-align:right;padding:7px 10px;color:#6b7785;font-weight:600;
  border-bottom:2px solid #e1e7ee;font-size:12px}
table.perp th:first-child,table.holders th:nth-child(2){text-align:left}
table.perp td,table.holders td{text-align:right;padding:7px 10px;border-bottom:1px solid #eef2f6}
table.perp td.venue,table.holders td:nth-child(2){text-align:left}
table.perp td.iv{color:#8a96a3}
table.perp tr.allrow td{font-weight:700;background:#f7f9fb}
table.holders td.mono,table.holders a.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
section.card.span .missing{color:#8a96a3;font-style:italic;padding:8px 0}
section.card.span p.note{font-size:12px;color:#8a96a3;margin:10px 0 0}
"""

JS = """
<script>
const boxes=[];const tiles=[...document.querySelectorAll('.tile')];
const rows=[...document.querySelectorAll('.lrow')];const items=[...tiles,...rows];
const search=document.getElementById('search'),count=document.getElementById('count');
const chk=document.getElementById('fdvchk'),val=document.getElementById('fdvval');
function apply(){
  const q=search.value.trim().toLowerCase();
  const thr=chk.checked?parseFloat(val.value||'0')*1e9:null;
  const ok=el=>(!q||el.dataset.search.includes(q))&&(thr===null||parseFloat(el.dataset.fdv)>thr);
  for(const el of items) el.style.display=ok(el)?'':'none';
  count.textContent=tiles.filter(ok).length+' / '+tiles.length+' tokens';
}
search.addEventListener('input',apply);chk.addEventListener('change',apply);val.addEventListener('input',apply);
const bG=document.getElementById('view-grid'),bL=document.getElementById('view-list');
const views=document.getElementById('views');
// remember the chosen view (per report) so returning from a token detail page
// via the back link lands on the same view instead of resetting to thumbnails.
const VIEW_KEY='ll-view-scams';
function setView(v,persist){views.className='view-'+v;bG.classList.toggle('active',v=='grid');bL.classList.toggle('active',v=='list');
  if(persist){try{localStorage.setItem(VIEW_KEY,v);}catch(e){}}}
bG.addEventListener('click',()=>setView('grid',true));bL.addEventListener('click',()=>setView('list',true));
let _v0='grid';try{_v0=localStorage.getItem(VIEW_KEY)||'grid';}catch(e){}
setView(_v0=='list'?'list':'grid',false);
const ltab=document.getElementById('ltab');
if(ltab){const tb=ltab.querySelector('tbody');
 ltab.querySelectorAll('th').forEach((th,i)=>{let asc=false;th.addEventListener('click',()=>{
  const rs=[...tb.rows];rs.sort((a,b)=>{const x=a.cells[i].dataset.s??a.cells[i].textContent,y=b.cells[i].dataset.s??b.cells[i].textContent;
  const nx=parseFloat(x),ny=parseFloat(y);const c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:(''+x).localeCompare(y);return asc?c:-c;});
  asc=!asc;ltab.querySelectorAll('th').forEach(h=>h.classList.remove('sorted'));th.classList.add('sorted');
  rs.forEach(r=>tb.appendChild(r));});});}
apply();
</script>"""


def main():
    if not DATA.exists():
        print(f"{DATA} missing — run fetch_scam_data.py first")
        return
    data = json.loads(DATA.read_text(encoding="utf-8"))
    FUNDING.update(_load_funding([r["symbol"].upper() for r in data.values()]))
    # order by FDV desc (matches the CSV's rough ordering)
    recs = sorted(data.values(), key=lambda r: -(r.get("fdv") or r.get("csv_fdv") or 0))
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(_index(recs), encoding="utf-8")
    for r in recs:
        (SITE / f"{r['symbol'].lower()}.html").write_text(_detail(r), encoding="utf-8")
    print(f"wrote {SITE/'index.html'} + {len(recs)} detail pages")


if __name__ == "__main__":
    main()

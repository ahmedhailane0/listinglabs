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
import math
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from build.build_listing_report import CSS as RCSS          # reuse reactions styling
from build.build_listing_report import _num_cell, _pct, _NEG_INF  # reuse reactions list/stat cells
from build.build_listing_report import page_meta, build_stamp     # shared CSP/OG head + stamp
from build.build_funding import _investors_from_item, _excel_amounts  # same funding source as reactions
from lib.listing_chart import fmt_usd_compact, fmt_subscript_price, parse_iso
from lib.interactive_chart import timeseries_html

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
SITE = HERE / "Listinglabs" / "scams"
DATA = HERE.parent / "cache" / "scam_data.json"
PRICES = HERE.parent / "cache" / "scam_prices"
ROOTDATA = HERE.parent / "cache" / "rootdata.json"

# symbol(upper) -> {"amount", "source", "investors": [...]}. Built in main() from
# the same RootData + Excel caches the Listing Reactions report uses, so funding
# is sourced identically across both reports. Falls back to scam_data's funding.
FUNDING: dict[str, dict] = {}

# symbol(upper) -> ISO date of the token's TGE / first listing (CMC dateAdded, with
# a CoinGecko genesis fallback), from fetch_scam_tge.py -> cache/scam_tge.json. Drives
# the TGE column + the default newest-first date sort, mirroring the Listing
# Reactions report's TGE column.
TGE: dict[str, str] = {}
TGE_CACHE = HERE.parent / "cache" / "scam_tge.json"


def _tge_dt(rec):
    iso = TGE.get((rec.get("symbol") or "").upper())
    if not iso:
        return None
    try:
        return parse_iso(iso)
    except Exception:
        return None


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

# sym -> sparkline symbol body; built once, referenced by tile AND list row via
# <use> (same dedup as the reactions index — halves the index page weight).
_SPARK_BODIES: dict[str, str] = {}


def _spark_body(sym: str) -> str:
    if sym in _SPARK_BODIES:
        return _SPARK_BODIES[sym]
    body = ""
    p = PRICES / f"{sym}.json"
    if p.exists():
        series = json.loads(p.read_text(encoding="utf-8"))
        if len(series) >= 2:
            step = max(1, len(series) // 120)
            pts = series[::step]
            if pts[-1][0] != series[-1][0]:
                pts.append(series[-1])      # always end on the real latest price
            vals = [v for _t, v in pts]
            lo, hi = min(vals), max(vals)
            span = (hi - lo) or 1.0
            n = len(vals)
            x = lambda i: round(i / (n - 1) * _SW, 2)
            y = lambda v: round(_SH - (v - lo) / span * (_SH - 2) - 1, 2)
            line = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(vals))
            area = f"0,{_SH} {line} {_SW},{_SH}"
            body = (f'<polygon class="spark-fill" points="{area}"/>'
                    f'<polyline class="spark-line" points="{line}"/>')
    _SPARK_BODIES[sym] = body
    return body


def _spark_defs() -> str:
    syms = "".join(
        f'<symbol id="sp-{s.lower()}" viewBox="0 0 {_SW:g} {_SH:g}" '
        f'preserveAspectRatio="none">{b}</symbol>'
        for s, b in _SPARK_BODIES.items() if b)
    return (f'<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
            f'<defs>{syms}</defs></svg>') if syms else ""


def _sparkline(sym: str) -> str:
    if not _spark_body(sym):
        return ""
    return (f'<svg class="thumb" aria-hidden="true">'
            f'<use href="#sp-{sym.lower()}" width="100%" height="100%"/></svg>')


def _price_chart(sym: str, name: str) -> str:
    p = PRICES / f"{sym}.json"
    if not p.exists():
        return '<div class="missing">no price data</div>'
    series = json.loads(p.read_text(encoding="utf-8"))
    if len(series) < 2:
        return '<div class="missing">no price data</div>'
    ys = [v for _t, v in series]
    ref = min([v for v in ys if v > 0] or [1])
    dec = max(4, 2 - math.floor(math.log10(ref))) if ref > 0 else 4
    # Same engine as the Listing Reactions price charts: TradingView Lightweight
    # Charts, so the price/time trading charts are consistent across the whole site.
    # (The OI/funding history + donuts on this page stay on Plotly.)
    points = [[int(t / 1000), v] for t, v in series]
    return timeseries_html(f"chart-{sym.lower()}", [{
        "data": points, "kind": "area", "color": "#1f4e79", "scale": "right",
        "name": "Price", "fmt": {"kind": "price", "dec": dec}, "fill": True,
    }], height=520, ranges=[("1M", 30), ("3M", 90), ("6M", 180), ("All", None)])


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
        # Nearest point to the target, capped at 2 days off — a gapped series
        # must omit the checkpoint rather than quote a far-away price as "n days
        # ago" (same rule as lib.metrics.CHECKPOINT_TOL).
        target = last_t - n_days * DAY
        base_t, base = min(s, key=lambda p: abs(p[0] - target))
        if abs(base_t - target) > 2 * DAY or not base:
            return None
        return (last / base - 1) * 100

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
    tge = _tge_dt(rec)
    tge_txt = tge.strftime("%Y-%m-%d") if tge else "—"
    return f"""
    <a class="tile" href="{sym.lower()}.html" data-venues="||" data-search="{search}" data-fdv="{fdv or 0:.0f}">
      {_sparkline(sym)}
      <div class="tile-body">
        <div class="tile-head"><span class="name">{html.escape(rec.get('name', sym))}</span>
          <span class="sym">{html.escape(sym)}</span>{_flag_chips(rec)}</div>
        <div class="tile-meta">
          <span><b>Price</b> {fmt_subscript_price(px) if px else '—'}</span>
          <span><b>FDV</b> {_usd(fdv)}</span>
          <span><b>TGE</b> {tge_txt}</span>
        </div>
      </div>
    </a>"""


# Same columns as the Listing Reactions list view (performance from price
# history) plus OI/Vol (the parked-positions screener) and the watchlist-
# specific Memo (the $1B-FDV behaviour notes).
LIST_COLS = ["#", "Token", "TGE", "Since", "24h", "7d", "30d", "90d", "FDV", "MC", "OI%", "OI/Vol", "Funding", "Memo"]


def _ratio_cell(rec) -> str:
    """OI/Vol ratio cell: plain number, red when it crosses the parked-OI
    threshold. Sortable so the most-parked tokens rise to the top."""
    r = _screen(rec)["ratio"]
    if r is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    cls = " neg" if r >= FLAG_OI_VOL else ""
    return f'<td class="n{cls}" data-s="{r:.4f}">{r:.2f}</td>'


def _tge_cell(rec) -> str:
    """TGE date cell (token first-listed), sorted by epoch seconds — mirrors the
    Listing Reactions report's TGE column."""
    t = _tge_dt(rec)
    if t is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    return f'<td class="tge" data-s="{t.timestamp():.0f}">{t.strftime("%Y-%m-%d")}</td>'


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
        f"{_tge_cell(rec)}"
        f"{_num_cell(p.get('since'))}{_num_cell(p.get('p24'))}{_num_cell(p.get('p7'))}"
        f"{_num_cell(p.get('p30'))}{_num_cell(p.get('p90'))}"
        f"{_num_cell(fdv, pct=False, color=False)}{_num_cell(mc, pct=False, color=False)}"
        f"{_num_cell(oi, pct=True, color=False)}"
        f"{_ratio_cell(rec)}"
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
PERP_HIST = HERE.parent / "cache" / "perp_history"
HOLDERS = HERE.parent / "cache" / "scam_holders"
PLATFORMS = HERE.parent / "cache" / "scam_platforms.json"

# Pretty chain names for the bubble-map chain picker (CG platform key -> label).
CHAIN_NAMES = {
    "ethereum": "Ethereum", "binance-smart-chain": "BNB Chain", "base": "Base",
    "polygon-pos": "Polygon", "arbitrum-one": "Arbitrum",
    "optimistic-ethereum": "Optimism", "avalanche": "Avalanche", "fantom": "Fantom",
    "cronos": "Cronos", "solana": "Solana", "mantle": "Mantle",
    "the-open-network": "TON", "hyperliquid": "Hyperliquid", "stable": "Stable",
}
# CG platform key -> Bubblemaps legacy chain code (kept in sync with fetch_holders).
CG_PLATFORM_TO_BMAPS = {
    "ethereum": "eth", "binance-smart-chain": "bsc", "base": "base",
    "polygon-pos": "poly", "arbitrum-one": "arbi", "avalanche": "avax",
    "fantom": "ftm", "cronos": "cro", "solana": "sol",
}

# Venue allowlist (see CLAUDE.md). Cached perp snapshots may still contain
# dropped venues (BingX/MEXC) until re-fetched, so the render filters to this set
# and recomputes the total + shares from the survivors.
ALLOWED_PERP_VENUES = {"Binance", "OKX", "Bybit", "KuCoin", "Bitget", "Gate"}

# Holder-address explorers per chain (token explorers live in EXPLORERS above; a
# holder is a plain address, so the path differs: /address/ not /token/).
ADDR_EXPLORERS = {
    "ethereum": "https://etherscan.io/address/",
    "binance-smart-chain": "https://bscscan.com/address/",
    "base": "https://basescan.org/address/",
    "polygon-pos": "https://polygonscan.com/address/",
    "arbitrum-one": "https://arbiscan.io/address/",
    "optimistic-ethereum": "https://optimistic.etherscan.io/address/",
    "avalanche": "https://snowtrace.io/address/",
    "solana": "https://solscan.io/account/",
}


_PERP_CACHE: dict = {}


def _load_perp(sym):
    sym = sym.upper()
    if sym in _PERP_CACHE:
        return _PERP_CACHE[sym]
    p = PERP / f"{sym}.json"
    perp = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    _PERP_CACHE[sym] = _allowed_perp(perp)
    return _PERP_CACHE[sym]


# Screening thresholds (documented in docs/OI_VOLUME_METHODOLOGY.md): OI/vol
# ≥ 0.5 = open positions are half a day's trading — parked OI, squeeze fuel;
# |annualized OI-weighted funding| ≥ 100%/yr = someone is paying heavily to
# hold a position/the price. Either is the watchlist's manipulation tell.
FLAG_OI_VOL = 0.5
FLAG_FUNDING_ANN = 1.0


def _screen(rec) -> dict:
    """Tracked-venue OI/vol ratio + OI-weighted annualized funding + ⚠ flags,
    for the list column and the tile/detail chips. The Others row is excluded
    from both sides (it has no volume data)."""
    perp = _load_perp(rec["symbol"])
    venues = [v for v in ((perp or {}).get("venues") or []) if not v.get("is_others")]
    oi = sum(v["oi_usd"] for v in venues if v.get("oi_usd"))
    vol = sum(v["vol24h_usd"] for v in venues if v.get("vol24h_usd"))
    ratio = (oi / vol) if (oi and vol) else None
    num = sum(v["funding_annualized"] * v["oi_usd"] for v in venues
              if v.get("funding_annualized") is not None and v.get("oi_usd"))
    den = sum(v["oi_usd"] for v in venues
              if v.get("funding_annualized") is not None and v.get("oi_usd"))
    fund_w = (num / den) if den else None
    flags = []
    if ratio is not None and ratio >= FLAG_OI_VOL:
        flags.append(("parked OI", f"OI is {ratio:.2f}× the 24h volume — positions "
                                   f"held against thin trading"))
    if fund_w is not None and abs(fund_w) >= FLAG_FUNDING_ANN:
        flags.append(("extreme funding", f"OI-weighted funding ≈ {fund_w * 100:+.0f}%/yr"))
    return {"ratio": ratio, "fund_w": fund_w, "flags": flags}


def _flag_chips(rec) -> str:
    chips = "".join(
        f'<span class="flag" title="{html.escape(tip)}">⚠ {html.escape(label)}</span>'
        for label, tip in _screen(rec)["flags"])
    return f'<span class="flags">{chips}</span>' if chips else ""


def _allowed_perp(perp):
    """Filter a perp snapshot to ALLOWED_PERP_VENUES and recompute total OI +
    per-venue share, so dropped venues vanish from cached files immediately (no
    re-fetch needed). The summed OI of the untracked venues (`others_oi_usd`, set
    by the CoinGecko fetch path) is folded back in as a single 'Others' row pinned
    last, so the total + shares reflect the WHOLE market while only the venues we
    trust are listed individually."""
    if not perp:
        return perp
    venues = [v for v in (perp.get("venues") or []) if v.get("venue") in ALLOWED_PERP_VENUES]
    others = perp.get("others_oi_usd") or 0.0
    n_others = perp.get("n_others") or 0
    if others > 0:
        venues = venues + [{"venue": "Others", "oi_usd": others, "funding": None,
                            "funding_annualized": None, "n_others": n_others,
                            "is_others": True}]
    total = sum(v["oi_usd"] for v in venues) or 0.0
    for v in venues:
        v["oi_share_pct"] = (v["oi_usd"] / total * 100) if total else None
    out = dict(perp)
    out.update(venues=venues, total_oi_usd=total, n_venues=len(venues),
               others_oi_usd=others, n_others=n_others)
    return out


def _load_holders(sym):
    p = HOLDERS / f"{sym.upper()}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _load_platforms():
    return json.loads(PLATFORMS.read_text(encoding="utf-8")) if PLATFORMS.exists() else {}


def _chains_for(rec, platforms):
    """{chain: contract} for a token: the platforms cache merged with the
    scam_data chain+contract (kept in sync with fetch_holders._chains_for)."""
    sym = rec["symbol"].upper()
    chains = dict(platforms.get(sym) or {})
    ch, c = rec.get("chain"), rec.get("contract")
    if ch and c and ch not in chains:
        chains[ch] = c
    return chains


def _load_holders_chain(sym, chain):
    """Per-chain holder file written by the multichain fetch_holders; falls back
    to the legacy single-file when the per-chain one isn't present yet."""
    p = HOLDERS / f"{sym.upper()}__{chain}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    legacy = _load_holders(sym)
    return legacy if (legacy and legacy.get("chain") == chain) else None


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
    # Greater/lesser threshold flags (circulation-ratio ≥/<30%, retail ≥/<1%) are
    # rendered NEUTRAL for now — no green/red — per request; the comparison text +
    # ⚠ marker still convey the signal. (`risk` is kept in the signature so callers
    # are unchanged and colour can be re-enabled later.)
    return f'<span class="badge" style="color:#42505e;background:#eef2f6">{html.escape(text)}</span>'


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
    venues = perp["venues"]
    head = ("<tr><th>Exchange</th><th>OI (USD)</th><th>Share</th>"
            "<th>Funding</th><th>Every</th><th>Annualized</th><th>OI/24h vol</th></tr>")
    # "Share" is each row's % of the WHOLE-market total (rows sum to 100%). The
    # tracked majors are listed individually; everything else is the Others row.
    all_row = (f'<tr class="allrow"><td>All venues ({len(venues)})</td>'
               f'<td>{_usd(perp["total_oi_usd"])}</td>'
               f'<td>100%</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>')
    rows = [all_row]
    for v in venues:
        if v.get("is_others"):
            k = v.get("n_others") or 0
            label = f'Others ({k})' if k else 'Others'
            rows.append(
                f'<tr class="otherrow"><td class="venue">{label}'
                f'<span class="htag">untracked</span></td>'
                f'<td>{_usd(v["oi_usd"])}</td>'
                f'<td>{v["oi_share_pct"]:.1f}%</td>'
                f'<td>—</td><td class="iv">—</td><td>—</td><td>—</td></tr>')
            continue
        iv = f"{v.get('interval_h', 8):g}h"
        ovr = f"{v['oi_vol_ratio']:.3f}" if v.get("oi_vol_ratio") is not None else "—"
        rows.append(
            f'<tr><td class="venue">{html.escape(v["venue"])}</td>'
            f'<td>{_usd(v["oi_usd"])}</td>'
            f'<td>{v["oi_share_pct"]:.1f}%</td>'
            f'<td>{_fund_span(v.get("funding"))}</td><td class="iv">{iv}</td>'
            f'<td>{_fund_span(v.get("funding_annualized"), annual=True)}</td>'
            f'<td>{ovr}</td></tr>')
    note = ('<p class="note">Shares are each row’s % of the <b>total CEX-perp</b> open '
            'interest — they sum to 100%. The major venues we track (Binance, OKX, '
            'Bybit, KuCoin, Bitget, Gate) are listed individually; <b>Others</b> '
            'aggregates the OI of all the smaller centralized exchanges (BingX, MEXC, '
            'LBank, XT…) we don’t list one by one. <b>DEX / on-chain perps</b> '
            '(Hyperliquid, dYdX, gTrade…) are excluded.</p>')
    return (f'<div class="tablewrap"><table class="perp"><thead>{head}</thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>{note}')


def _holder_tag(hd) -> str:
    """A small tag chip for a holder row: the GoPlus tag (CEX / 'Null Address'),
    else a generic 'contract' marker for non-EOA holders."""
    tag = hd.get("tag")
    if tag:
        return f'<span class="htag">{html.escape(tag)}</span>'
    if hd.get("is_contract"):
        return '<span class="htag">contract</span>'
    return ""


# Chains the Top-holders picker may offer, on top of the token's own primary
# chain: only the two majors (per user request — keep it simple). The token's
# real home chain is always shown; ETH / BNB are added when it's also there.
HOLDER_PICKER_EXTRA = ("ethereum", "binance-smart-chain")


def _bubblemaps_link(chain, contract) -> str:
    """Link out to Bubblemaps' interactive cluster map for this chain's contract.
    Their canvas can't be embedded (CSP frame-ancestors blocks iframing), so a
    link is all we surface. '' when Bubblemaps doesn't cover the chain."""
    code = CG_PLATFORM_TO_BMAPS.get(chain or "")
    if not (code and contract):
        return ""
    url = f"https://app.bubblemaps.io/{code}/token/{contract}"
    return (f'<a class="bmap-btn" href="{html.escape(url)}" target="_blank" '
            f'rel="noopener">Open cluster map on Bubblemaps ↗</a>')


def _holders_panel(rec, chain) -> str:
    """Top-10 holders table + donut for ONE chain (the per-chain holder file).
    `chain` drives the holder source, the address explorer, and the unavailable
    message; token counts/USD use the token's global supply × price (one number —
    approximate on a bridged token's secondary chain, by design)."""
    sym = rec["symbol"]
    h = _load_holders_chain(sym, chain) if chain else _load_holders(sym)
    if not h or not h.get("available"):
        nm = html.escape(CHAIN_NAMES.get(chain, chain) if chain else (rec.get("chain") or "this chain"))
        return ('<div class="missing">Top-holder data unavailable on '
                f'{nm} — no keyless on-chain holder source covers this chain.</div>')
    tot = rec.get("total_supply") or rec.get("max_supply")
    px = rec.get("price")
    base = ADDR_EXPLORERS.get(chain or rec.get("chain") or "")
    rows = []
    for hd in h["holders"]:
        share = hd["share"]
        toks = (share / 100 * tot) if tot else None
        usd = (toks * px) if (toks and px) else None
        addr = hd.get("address") or ""
        short = f"{addr[:8]}…{addr[-6:]}" if len(addr) > 16 else addr
        link = (f'<a href="{html.escape(base + addr)}" target="_blank" rel="noopener" '
                f'class="mono">{html.escape(short)}</a>' if (base and addr)
                else f'<span class="mono">{html.escape(short)}</span>')
        rows.append(f'<tr><td>{hd["rank"]}</td><td>{link} {_holder_tag(hd)}</td>'
                    f'<td>{_compact(toks)}</td><td>{_usd(usd)}</td>'
                    f'<td>{share:.2f}%</td></tr>')
    top10, retail = h["top10_share"], h["retail_share"]
    # GoPlus shares can exceed 100% of supply (LP/burn double-counting, or a
    # supply base that excludes burned tokens) — which would render a negative
    # "retail" and a self-contradictory badge. Clamp the displayed numbers and
    # say plainly that the source data is inconsistent.
    inconsistent = top10 > 100 or retail < 0
    top10_c = min(max(top10, 0.0), 100.0)
    retail_c = min(max(retail, 0.0), 100.0)
    tb = _badge(f"Top-10: {top10_c:.1f}% · {'⚠ ≥95% (highly concentrated)' if top10_c >= 95 else '<95%'}",
                top10_c >= 95)
    if inconsistent:
        rb = _badge("⚠ shares exceed supply — LP/burn double-count likely (source data inconsistent)",
                    True)
    else:
        rb = _badge(f"Retail: {retail_c:.1f}% · {'⚠ <1% (negligible)' if retail_c < 1 else '≥1%'}",
                    retail_c < 1)
    hc = h.get("holder_count")
    hc_badge = (f' <span class="badge" style="color:#42505e;background:#eef2f6">'
                f'{hc:,} holders</span>' if hc else "")
    src = html.escape(h.get("source") or "")
    head = "<tr><th>#</th><th>Holder</th><th>Tokens</th><th>USD</th><th>Share</th></tr>"
    note = ('<p class="note">Retail = 100% − top-10 holders. Top-10 includes any '
            f'CEX/contract/burn wallets (simple definition). Source: {src}.</p>')
    donut = _donut_holders(rec, h, chain)
    bmap = _bubblemaps_link(chain or rec.get("chain"), h.get("contract") or rec.get("contract"))
    bmap_row = f'<div class="bmap-row">{bmap}</div>' if bmap else ""
    table = (f'<div class="badges">{tb} {rb}{hc_badge}</div>'
             f'<div class="tablewrap"><table class="holders"><thead>{head}</thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>{note}{bmap_row}')
    return f'<div class="hol-grid">{table}{donut}</div>' if donut else table


def _holder_picker_chains(rec, platforms) -> list:
    """Chains the Top-holders picker offers: the token's own primary chain first,
    then Ethereum / BNB Chain when the token is also deployed there. Empty → fall
    back to the primary (or None) so the panel renders an unavailable message."""
    chains = _chains_for(rec, platforms)
    primary = rec.get("chain")
    picker = [primary] if primary in chains else []
    for c in HOLDER_PICKER_EXTRA:
        if c in chains and c not in picker:
            picker.append(c)
    return picker


def _holders_switch_js(sym) -> str:
    """Chain-picker panel toggle. Pure display switching — the donuts are static
    SVG now, nothing needs a resize call when a panel becomes visible."""
    s = sym.lower()
    return ("<script>(function(){"
            f"var w=document.getElementById('hwrap-{s}');if(!w)return;"
            "var ps=[].slice.call(w.querySelectorAll('.hpanel'));"
            "function act(i){ps.forEach(function(p){"
            "p.style.display=(+p.dataset.idx===i)?'':'none';});}"
            f"var s=document.getElementById('hsel-{s}');"
            "if(s)s.addEventListener('change',function(){act(+s.value);});act(0);"
            "})();</script>")


def _holders_block(rec, platforms) -> str:
    """Top-10 holders: a per-chain picker (token's home chain + ETH/BNB when
    present) swapping table+donut panels. One chain → no picker, just the panel."""
    sym = rec["symbol"]
    picker = _holder_picker_chains(rec, platforms)
    if len(picker) <= 1:
        return _holders_panel(rec, picker[0] if picker else rec.get("chain"))
    opts, panels = [], []
    for idx, chain in enumerate(picker):
        nm = html.escape(CHAIN_NAMES.get(chain, chain))
        opts.append(f'<option value="{idx}">{nm}</option>')
        hide = "" if idx == 0 else ' style="display:none"'
        panels.append(f'<div class="hpanel" data-idx="{idx}"{hide}>{_holders_panel(rec, chain)}</div>')
    sel = (f'<div class="chainsel"><label>Chain <select id="hsel-{sym.lower()}">'
           f'{"".join(opts)}</select></label>'
           f'<span class="chaincount">{len(picker)} chains</span></div>')
    return (f'<div id="hwrap-{sym.lower()}">{sel}'
            f'<div class="hpanels">{"".join(panels)}</div>{_holders_switch_js(sym)}</div>')


# ── time-series + donut charts (Lightweight Charts + inline SVG; no Plotly) ──

# slice palette (holders/venues/supply): muted, report-consistent.
_DONUT_COLORS = ["#1f4e79", "#2e6da4", "#5b9bd5", "#8ab6e0", "#9c6ade", "#d98c5f",
                 "#e0b35f", "#6aa84f", "#c0392b", "#7f8c9a", "#c5ccd3"]

_HIST_RANGES = [("1M", 30), ("3M", 90), ("All", None)]


def _hist_series(sym: str) -> list:
    p = PERP_HIST / f"{sym.upper()}.json"
    if not p.exists():
        return []
    return [pt for pt in json.loads(p.read_text(encoding="utf-8")) if pt.get("total_oi_usd")]


def _oi_funding_history_chart(sym: str) -> str:
    """Dual-axis time series of total perp OI (left) and OI-weighted funding rate
    (right), accumulated by fetch_perp_markets into cache/perp_history. Rendered
    with Lightweight Charts (same engine as every other trading chart); crosshair
    + range synced with the OI/volume chart below it. Values plotted are the raw
    cache points — the renderer never transforms them (funding ×100 to % only)."""
    series = _hist_series(sym)
    if not series:
        return ""
    if len(series) < 2:
        return ('<div class="missing">OI &amp; funding history builds over time — '
                'a point is logged each refresh; check back after a few cycles.</div>')
    oi = [[pt["t"], pt["total_oi_usd"]] for pt in series]
    fund = [[pt["t"], (pt["funding_avg"] * 100 if pt.get("funding_avg") is not None else None)]
            for pt in series]
    return timeseries_html(f"oihist-{sym.lower()}", [
        {"data": oi, "kind": "area", "color": "#1f4e79", "scale": "left",
         "name": "Total OI", "fmt": {"kind": "usdCompact"}},
        {"data": fund, "kind": "line", "color": "#c0392b", "scale": "right",
         "name": "Funding (OI-wtd)", "fmt": {"kind": "percent", "dec": 4}},
    ], height=320, ranges=_HIST_RANGES, sync=f"ph-{sym.lower()}")


def _oi_volume_history_chart(sym: str) -> str:
    """OI vs 24h-volume time series: total tracked-venue OI (line, left $ axis),
    24h volume (bars on an overlay scale pinned to the lower third) and the
    OI/volume ratio (line, right axis). Same cache/perp_history series as the
    OI/funding chart; ratio = total_oi_usd / vol24h_usd per point, computed at
    build from the raw cache values. High/rising ratio = positions parked
    against thin real trading — the watchlist's manipulation tell."""
    series = _hist_series(sym)
    if not series:
        return ""
    with_vol = [pt for pt in series if pt.get("vol24h_usd")]
    if len(with_vol) < 2:
        return ('<div class="missing">OI / volume history builds over time — '
                'volume is logged each refresh; check back after a few cycles.</div>')
    oi = [[pt["t"], pt["total_oi_usd"]] for pt in series]
    vol = [[pt["t"], pt["vol24h_usd"]] for pt in with_vol]
    ratio = [[pt["t"], pt["total_oi_usd"] / pt["vol24h_usd"]] for pt in with_vol]
    return timeseries_html(f"oivol-{sym.lower()}", [
        # bars first so the OI line draws over them; sync_main=1 anchors the
        # shared crosshair to the OI line (full time domain).
        {"data": vol, "kind": "hist", "color": "rgba(91,155,213,0.45)", "scale": "vol",
         "margins": {"top": 0.72, "bottom": 0}, "name": "24h volume",
         "fmt": {"kind": "usdCompact"}},
        {"data": oi, "kind": "line", "color": "#1f4e79", "scale": "left",
         "name": "Total OI", "fmt": {"kind": "usdCompact"}},
        {"data": ratio, "kind": "line", "color": "#9c6ade", "scale": "right",
         "name": "OI / 24h vol", "fmt": {"kind": "num", "dec": 2}},
    ], height=320, ranges=_HIST_RANGES, sync=f"ph-{sym.lower()}", sync_main=1)


def _donut(div_id, labels, values, title, colors=None, center=None, usd=False) -> str:
    """A single donut as BUILD-TIME inline SVG (no JS dependency). Slices are
    drawn with pathLength=100 stroke-dashes so each arc is exactly the value's
    share; exact raw values live in native <title> tooltips and the legend.
    Same signature as the old Plotly version — call sites unchanged."""
    total = sum(v for v in values if v) or 1.0
    cols = colors or _DONUT_COLORS
    arcs, legend = [], []
    cum = 0.0
    for i, (lbl, v) in enumerate(zip(labels, values)):
        if not v or v <= 0:
            continue
        pct = v / total * 100
        col = cols[i % len(cols)]
        val_txt = f" · {fmt_usd_compact(v)}" if usd else ""
        tip = f"{lbl} — {pct:.1f}%{val_txt}" + ("" if usd else f" ({v:,.2f})")
        arcs.append(
            f'<circle r="40" cx="60" cy="60" fill="none" stroke="{col}" stroke-width="22" '
            f'pathLength="100" stroke-dasharray="{pct:.3f} {100 - pct:.3f}" '
            f'stroke-dashoffset="{-cum:.3f}"><title>{html.escape(tip)}</title></circle>')
        legend.append(
            f'<li><i style="background:{col}"></i>{html.escape(str(lbl))}'
            f'<b>{pct:.1f}%{val_txt}</b></li>')
        cum += pct
    if not arcs:
        return ""
    ctr = (f'<text x="60" y="60" text-anchor="middle" dominant-baseline="middle" '
           f'class="sd-ctr">{html.escape(center.replace("<br>", " "))}</text>') if center else ""
    return (f'<figure class="sdonut" id="{div_id}">'
            f'<figcaption>{html.escape(title)}</figcaption>'
            f'<div class="sd-row">'
            f'<svg viewBox="0 0 120 120" class="sd-svg" role="img" '
            f'aria-label="{html.escape(title)}">'
            f'<g transform="rotate(-90 60 60)">{"".join(arcs)}</g>{ctr}</svg>'
            f'<ul class="sd-legend">{"".join(legend)}</ul>'
            f'</div></figure>')


def _donut_holders(rec, h, chain=None) -> str:
    """Top-10 holders + a 'Retail (rest)' slice. `chain` keeps the Plotly div id
    unique when the holders block stacks one panel per chain (else duplicate ids
    break the switcher)."""
    holders = h.get("holders") or []
    if not holders:
        return ""
    labels, values = [], []
    for hd in holders:
        tag = hd.get("tag")
        addr = hd.get("address") or ""
        lbl = tag if tag else (f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr or f"#{hd['rank']}")
        labels.append(f"#{hd['rank']} {lbl}")
        values.append(hd["share"])
    retail = h.get("retail_share")
    if retail and retail > 0:
        labels.append("Retail (rest)")
        values.append(retail)
    top10 = h.get("top10_share")
    if top10 is not None:
        top10 = min(max(top10, 0.0), 100.0)   # GoPlus can exceed 100% (see _holders_panel)
    center = f"Top-10<br>{top10:.0f}%" if top10 is not None else None
    suffix = f"-{CG_PLATFORM_TO_BMAPS.get(chain, chain)}" if chain else ""
    return _donut(f"dh-{rec['symbol'].lower()}{suffix}", labels, values,
                  "Holder distribution", center=center)


def _donut_oi(rec, perp) -> str:
    """Per-venue OI share (allowlisted venues only — already filtered)."""
    venues = (perp or {}).get("venues") or []
    venues = [v for v in venues if v.get("oi_usd")]
    if len(venues) < 1:
        return ""
    labels = [v["venue"] for v in venues]
    values = [v["oi_usd"] for v in venues]
    total = perp.get("total_oi_usd") or sum(values)
    # tracked venues keep the palette in order; the Others slice is muted grey.
    colors = ["#c5ccd3" if v.get("is_others") else _DONUT_COLORS[i % len(_DONUT_COLORS)]
              for i, v in enumerate(venues)]
    return _donut(f"doi-{rec['symbol'].lower()}", labels, values,
                  "OI by exchange", center=f"OI<br>{_usd(total)}", usd=True, colors=colors)


def _donut_supply(rec) -> str:
    """Circulating vs locked/not-yet-unlocked (= total − circulating)."""
    circ = rec.get("circ_supply")
    tot = rec.get("total_supply") or rec.get("max_supply")
    if not (circ and tot and tot > circ):
        return ""
    locked = tot - circ
    ratio = circ / tot * 100
    return _donut(f"dsup-{rec['symbol'].lower()}", ["Circulating", "Locked / not unlocked"],
                  [circ, locked], "Circulating vs locked",
                  colors=["#1f4e79", "#c5ccd3"], center=f"Circ<br>{ratio:.0f}%")


def _donut_fdvmc(rec) -> str:
    """Market cap (realized) vs diluted remainder (FDV − MC)."""
    mc = rec.get("mcap") or rec.get("csv_mc")
    fdv = rec.get("fdv") or rec.get("csv_fdv")
    if not (mc and fdv and fdv > mc):
        return ""
    rem = fdv - mc
    return _donut(f"dfdv-{rec['symbol'].lower()}", ["Market cap", "Diluted remainder"],
                  [mc, rem], "FDV vs market cap",
                  colors=["#2e6da4", "#e0b35f"], center=f"MC<br>{_usd(mc)}", usd=True)


def _perp_extras(rec, perp, sym) -> str:
    """OI-by-exchange donut + the OI/funding history time-series, shown under the
    perp table. Each piece self-skips when it has no data."""
    parts = []
    oi_donut = _donut_oi(rec, perp)
    if oi_donut:
        parts.append(f'<div class="donut-grid">{oi_donut}</div>')
    hist = _oi_funding_history_chart(sym)
    if hist:
        parts.append('<h4 class="hist-h">OI &amp; funding over time '
                     '<span class="asof">accumulated per refresh</span></h4>' + hist)
    oivol = _oi_volume_history_chart(sym)
    if oivol:
        parts.append('<h4 class="hist-h">OI vs 24h volume '
                     '<span class="asof">tracked venues · high ratio = parked OI, '
                     'thin trading</span></h4>' + oivol)
    return "".join(parts)


def _supply_valuation_block(rec) -> str:
    """Side-by-side supply + valuation donuts (skips whichever lacks data)."""
    donuts = [d for d in (_donut_supply(rec), _donut_fdvmc(rec)) if d]
    if not donuts:
        return ""
    return (f'<section class="card span"><h3>Supply &amp; valuation '
            f'<span class="asof">circulation ratio &amp; dilution</span></h3>'
            f'<div class="donut-grid">{"".join(donuts)}</div></section>')



def _detail(rec, platforms) -> str:
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
    _src = "via CoinGecko" if (perp or {}).get("source") == "coingecko" else "per exchange"
    perp_asof = (f"{_src} · as of {_dt.datetime.fromtimestamp(_ts, _dt.timezone.utc):%Y-%m-%d %H:%M} UTC"
                 if _ts else "keyless public exchange APIs")
    warn = ("" if rec.get("resolved", True) else
            '<div class="cat" style="background:#fdecea;color:#c0392b">⚠ identity auto-matched by symbol — verify</div>')
    chips = _flag_chips(rec)
    flagrow = f'<div class="flagrow">{chips}</div>' if chips else ""
    memo = html.escape(rec.get("memo_en") or "—")
    mc, fdv = rec.get("mcap") or rec.get("csv_mc"), rec.get("fdv") or rec.get("csv_fdv")
    fdvmc = f"<dt>FDV / MC</dt><dd>{fdv / mc:.1f}×</dd>" if (mc and fdv) else ""
    body = f"""
<header><a class="back" href="index.html">← all watchlist tokens</a></header>
<main><section class="card">
  <div class="info">
    <h2>{name} <span class="sym">{html.escape(sym)}</span></h2>
    {warn}
    {flagrow}
    {_links(rec)}
    <dl>
      <dt>TGE</dt><dd>{_tge_dt(rec).strftime('%Y-%m-%d') if _tge_dt(rec) else '—'} <span class="src">token launch (CMC)</span></dd>
      <dt>Chain</dt><dd>{html.escape(rec.get('chain') or '—')}</dd>
      <dt>Price</dt><dd>{fmt_subscript_price(rec['price']) if rec.get('price') else '—'}</dd>
      <dt>Market cap</dt><dd>{_usd(mc)}</dd>
      <dt>FDV</dt><dd>{_usd(fdv)}</dd>
      {fdvmc}
      <dt>Volume (24h)</dt><dd>{_usd(rec.get('vol') or rec.get('csv_vol'))}</dd>
      {_supply_dl(rec)}
      <dt>Open interest</dt><dd>{oi_str}</dd>
      <dt>Funding</dt><dd>{fund_amt}{fund_inv}</dd>
      <dt>Memo</dt><dd class="note">{memo}</dd>
    </dl>
    {_reaction_block(rec)}
  </div>
  <div class="chart">{_price_chart(sym, name)}</div>
</section>
<section class="card span">
  <h3>Perp markets <span class="asof">open interest &amp; funding · {perp_asof}</span></h3>
  {_perp_table(perp)}
  {_perp_extras(rec, perp, sym)}
</section>
{_supply_valuation_block(rec)}
<section class="card span">
  <h3>Top holders <span class="asof">on-chain distribution</span></h3>
  {_holders_block(rec, platforms)}
</section></main>"""
    title = f"{rec.get('name', sym)} ({sym}) — Manipulated"
    desc = (f"{rec.get('name', sym)} ({sym}) on the Manipulated watchlist — price history, "
            f"per-venue perp OI & funding, OI/volume trend, supply and top holders.")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{page_meta(title, desc)}'
            f'<title>{name} ({html.escape(sym)}) — Manipulated</title>'
            f'<link rel="stylesheet" href="style.css">'
            f'<script src="../report/lightweight-charts.standalone.production.js"></script>'
            f'</head><body>{body}</body></html>')


def _filter_bar() -> str:
    return """
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
    reactions_n = len(list((HERE / "listings").glob("*.json")))
    fm = HERE / "funnel" / "funnel_master.json"
    try:
        funnel_n = len(json.loads(fm.read_text(encoding="utf-8")))
    except Exception:
        funnel_n = None
    react_lbl = f"Binance Alpha &amp; Perps ({reactions_n})" if reactions_n else "Binance Alpha &amp; Perps"
    fun_lbl = f"CEX → Korea ({funnel_n})" if funnel_n else "CEX → Korea"
    body = f"""
<header><h1>Manipulated</h1>
<nav class="topnav"><a href="../report/index.html">{react_lbl}</a>
<a href="../funnel/report/index.html">{fun_lbl}</a>
<a class="active" href="index.html">Manipulated ({len(recs)})</a></nav>
<p>{len(recs)} tokens · price, MC, FDV, OI &amp; funding · notes on $1B-FDV behaviour · updated {build_stamp()} UTC</p></header>
{_filter_bar()}
<div id="views" class="view-grid">
  {_spark_defs()}
  <main class="grid">{tiles}</main>
  <div class="listwrap"><table class="list" id="ltab"><thead><tr>{head}</tr></thead>
  <tbody>{rows}</tbody></table></div>
</div>
{JS}"""
    desc = ("Manipulated-token watchlist — tokens propped to extreme FDV: price history, "
            "per-venue perp open interest, funding, OI/volume trend and holder concentration.")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{page_meta("Manipulated — ListingLabs", desc)}'
            f'<title>Manipulated</title><link rel="stylesheet" href="style.css"></head>'
            f'<body>{body}</body></html>')


EXTRA_CSS = """
.fdv{font-size:13px;color:#42505e;display:inline-flex;align-items:center;gap:6px}
.links.note{color:#8a96a3;font-style:italic}
/* deterministic column widths (14 cols: #, Token, TGE, Since, 24h, 7d, 30d, 90d,
   FDV, MC, OI%, OI/Vol, Funding, Memo). Fixed layout reads widths from the header
   row. A min-width keeps every column readable — when the viewport is narrower
   the wrapper (.listwrap, overflow-x:auto) scrolls horizontally instead of
   crunching the columns (esp. Memo). */
#ltab{table-layout:fixed;min-width:1160px}
#ltab th{overflow:hidden}
#ltab th:nth-child(1){width:3%}                                   /* # */
#ltab th:nth-child(2){width:15.5%;text-align:left}                /* Token */
#ltab th:nth-child(3){width:7.5%;text-align:left}                 /* TGE */
#ltab th:nth-child(4),#ltab th:nth-child(5),#ltab th:nth-child(6),
#ltab th:nth-child(7),#ltab th:nth-child(8){width:5.5%}           /* Since,24h,7d,30d,90d */
#ltab th:nth-child(9),#ltab th:nth-child(10){width:7.5%}          /* FDV, MC */
#ltab th:nth-child(11){width:5.5%}                                /* OI% */
#ltab th:nth-child(12){width:5.5%}                                /* OI/Vol */
#ltab th:nth-child(13){width:9.5%;text-align:left}                /* Funding */
#ltab th:nth-child(14){width:11%;text-align:left}                 /* Memo */
/* ⚠ screening chips (parked OI / extreme funding) on tiles + detail header */
.flags{display:inline-flex;gap:5px;flex-wrap:wrap}
.flag{background:#fdecea;color:#c0392b;border-radius:9px;font-size:10.5px;
  font-weight:700;padding:1px 8px;white-space:nowrap;cursor:help}
.tile-head .flags{margin-left:auto}
.flagrow{margin:0 0 10px}
#ltab td{overflow:hidden}
#ltab td.rank{text-align:center}
#ltab td.memo{max-width:none;white-space:normal;font-size:12px;color:#42505e}
#ltab td.memo span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* per-token detail: full-width sections for perp + holders tables.
   The base .card delegates its padding to .info/.chart children, but these span
   sections render content (h3 / tables / donuts) as DIRECT children, so they need
   their own padding — without it titles sit flush in the top-left corner and the
   right edge (donut legends, wide tables) gets clipped by .card{overflow:hidden}. */
section.card.span{display:block;margin-top:18px;padding:20px 24px}
section.card.span h3{margin:0 0 12px;font-size:15px;color:#1d2733}
section.card.span h3 .asof{font-size:12px;color:#8a96a3;font-weight:400;margin-left:8px}
.badge{display:inline-block;padding:2px 9px;border-radius:11px;font-size:12px;font-weight:600;white-space:nowrap}
/* in the narrow info panel a long ratio badge would overflow the dl value column
   and get clipped by .card{overflow:hidden}; let it wrap there instead of nowrap */
.info dl dd .badge{white-space:normal;line-height:1.6}
.info dl dd{min-width:0;overflow-wrap:anywhere}
.badges{margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap}
table.perp,table.holders{width:100%;border-collapse:collapse;font-size:13px}
/* min-widths so the wide detail tables SCROLL inside .tablewrap on phones instead
   of crushing every column to nothing (desktop containers exceed these, so no
   scroll there). Mirrors the index list table's approach. */
table.perp{min-width:520px}
table.holders{min-width:430px}
table.perp th,table.holders th{text-align:right;padding:7px 10px;color:#6b7785;font-weight:600;
  border-bottom:2px solid #e1e7ee;font-size:12px}
table.perp th:first-child,table.holders th:nth-child(2){text-align:left}
table.perp td,table.holders td{text-align:right;padding:7px 10px;border-bottom:1px solid #eef2f6}
table.perp td.venue,table.holders td:nth-child(2){text-align:left}
table.perp td.iv{color:#8a96a3}
table.perp tr.allrow td{font-weight:700;background:#f7f9fb}
/* Others = aggregate of untracked venues; muted so it reads as a catch-all */
table.perp tr.otherrow td{color:#8a96a3;background:#fbfcfd}
table.perp tr.otherrow td.venue{color:#6b7785}
table.perp tr.otherrow .htag{margin-left:7px}
table.holders td.mono,table.holders a.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
section.card.span .missing{color:#8a96a3;font-style:italic;padding:8px 0}
section.card.span p.note{font-size:12px;color:#8a96a3;margin:10px 0 0}
/* holder rows: a small tag chip for CEX/contract/burn wallets */
.htag{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:9px;font-size:11px;
  font-weight:600;color:#6b7785;background:#eef2f6;vertical-align:middle}
/* holders table + donut side by side (stacks on narrow screens) */
.hol-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:20px;align-items:start}
.hol-grid>*{min-width:0}
@media(max-width:760px){.hol-grid{grid-template-columns:1fr}}
/* donut grids: fluid, 1–2 donuts per row; min-width:0 lets each cell shrink
   with its column instead of overflowing the card */
.donut-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:8px}
.donut-grid>*{min-width:0}
/* static SVG donuts (build-time; replaced the Plotly pies) */
.sdonut{margin:0;background:#fff}
.sdonut figcaption{text-align:center;font-size:13px;font-weight:600;color:#1d2733;margin:6px 0 8px}
.sd-row{display:flex;align-items:center;gap:14px}
.sd-svg{flex:0 0 150px;width:150px;height:150px}
.sd-ctr{font-size:11.5px;fill:#42505e;font-weight:600}
.sd-legend{list-style:none;margin:0;padding:0;font-size:11.5px;color:#42505e;min-width:0}
.sd-legend li{display:flex;align-items:center;gap:6px;padding:1.5px 0;flex-wrap:wrap}
.sd-legend li i{flex:0 0 9px;width:9px;height:9px;border-radius:50%;display:inline-block}
.sd-legend li b{font-weight:600;color:#1d2733;margin-left:auto;padding-left:8px;white-space:nowrap}
@media(max-width:640px){.sd-svg{flex-basis:120px;width:120px;height:120px}}
/* wide tables (perp/holders) scroll horizontally on small screens instead of
   overflowing the card */
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
.hist-h{margin:18px 0 4px;font-size:14px;color:#1d2733}
.hist-h .asof{font-size:12px;color:#8a96a3;font-weight:400;margin-left:6px}
/* ── Top-holders chain picker + Bubblemaps link ──────────────────────────── */
.chainsel{display:flex;align-items:center;gap:12px;margin:0 0 14px;font-size:13px;color:#42505e}
.chainsel select{font:inherit;padding:5px 8px;border:1px solid #d7dee6;border-radius:7px;
  background:#fff;color:#1d2733;cursor:pointer}
.chainsel .chaincount{color:#8a96a3;font-size:12px}
.bmap-row{margin-top:14px}
.bmap-btn{display:inline-block;padding:9px 14px;border-radius:8px;background:#1f4e79;color:#fff;
  font-size:13px;font-weight:600;text-decoration:none}
.bmap-btn:hover{background:#163a5b}
@media(max-width:640px){
  section.card.span{padding:16px 14px}
  section.card.span h3{font-size:14px}
  .info dl{grid-template-columns:78px 1fr}
  /* roomier tap targets + a full-width Bubblemaps button that's easy to thumb */
  .chainsel{flex-wrap:wrap;gap:8px 12px}
  .chainsel select{padding:8px 10px}
  .bmap-row{margin-top:16px}
  .bmap-btn{display:block;text-align:center;padding:11px 14px}
  /* keep the chart-switch / hover scroll from feeling cramped */
  table.perp,table.holders{font-size:12.5px}
  table.perp th,table.holders th,table.perp td,table.holders td{padding:7px 8px}
}
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
 ltab.querySelectorAll('th').forEach((th,i)=>{let asc=false;
  th.tabIndex=0;th.setAttribute('role','button');th.setAttribute('aria-label','Sort by '+th.textContent);
  th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();th.click();}});
  th.addEventListener('click',()=>{
  const rs=[...tb.rows];rs.sort((a,b)=>{const x=a.cells[i].dataset.s??a.cells[i].textContent,y=b.cells[i].dataset.s??b.cells[i].textContent;
  const nx=parseFloat(x),ny=parseFloat(y);const c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:(''+x).localeCompare(y);return asc?c:-c;});
  const applied=asc?'asc':'desc';asc=!asc;
  ltab.querySelectorAll('th').forEach(h=>h.classList.remove('sorted','asc','desc'));th.classList.add('sorted',applied);
  rs.forEach(r=>tb.appendChild(r));});});}
apply();
</script>"""


def main():
    if not DATA.exists():
        print(f"{DATA} missing — run fetch_scam_data.py first")
        return
    data = json.loads(DATA.read_text(encoding="utf-8"))
    FUNDING.update(_load_funding([r["symbol"].upper() for r in data.values()]))
    if TGE_CACHE.exists():
        TGE.update(json.loads(TGE_CACHE.read_text(encoding="utf-8")))
    platforms = _load_platforms()
    # Default order: by TGE date, newest first (tokens without a known date last) —
    # mirrors the Listing Reactions report's newest-first ordering. The list is still
    # client-side re-sortable by any column header.
    recs = sorted(data.values(),
                  key=lambda r: (_tge_dt(r).timestamp() if _tge_dt(r) else float("-inf")),
                  reverse=True)
    SITE.mkdir(parents=True, exist_ok=True)
    # ONE shared stylesheet instead of ~35KB of CSS inlined into every page —
    # cached across the index + all detail pages.
    (SITE / "style.css").write_text(RCSS + EXTRA_CSS, encoding="utf-8")
    (SITE / "index.html").write_text(_index(recs), encoding="utf-8")
    for r in recs:
        (SITE / f"{r['symbol'].lower()}.html").write_text(_detail(r, platforms), encoding="utf-8")
    print(f"wrote {SITE/'index.html'} + {len(recs)} detail pages")


if __name__ == "__main__":
    main()

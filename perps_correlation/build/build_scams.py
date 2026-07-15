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

import hashlib
import html
import json
import math
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from build.build_listing_report import CSS as RCSS          # reuse reactions styling
from build.build_listing_report import _num_cell, _pct, _NEG_INF  # reuse reactions list/stat cells
from build.build_listing_report import page_meta, site_nav  # shared CSP/OG head + unified nav
from build.build_listing_report import theme_toggle_button, THEME_JS  # shared dark/light toggle
from build.build_funding import _investors_from_item, _excel_amounts  # same funding source as reactions
from lib.listing_chart import fmt_usd_compact, fmt_subscript_price, parse_iso
from lib.interactive_chart import timeseries_html

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
SITE = HERE / "Listinglabs" / "scams"
DATA = HERE.parent / "cache" / "scam_data.json"
PRICES = HERE.parent / "cache" / "scam_prices"
# Rolling intraday close candles per perp (5m + 1h), written hourly by the box's
# fetch_screener — gives the detail-page price chart smooth 24h/1W resolution that
# the daily scam_prices series can't (CMC-style). Missing => chart falls back to
# daily only. See fetch/fetch_screener.py (_klines_5m / price_candles write).
PRICE_CANDLES = HERE.parent / "cache" / "price_candles"
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

# ── Perp screener (cache/screener/screener.json, produced hourly by the always-on
# box) folded INTO this watchlist: Buy v1/v2/v3 signals + combined Binance+Bybit
# OI + the (BN+BYB)/FDV gate. Every screener-universe coin that isn't already a
# curated watchlist token is ADDED here as a signal-only record, so the whole
# manipulated-coin list lives on one tab.
SCREENER_FILE = HERE.parent / "cache" / "screener" / "screener.json"
SCREENER: dict[str, dict] = {}     # SYM(upper) -> screener token record
SCREENER_META: dict = {}           # as_of_hour / counts / gate / thresholds

# The AI's forward "trade journal": every live Buy-setup fire the box logged, with
# the outcome it scored 72h later (grade_fires). Public-safe — it exposes the calls
# and results, NOT the tuned thresholds (the edge). Rendered into trades.html.
FIRES_LOG = HERE.parent / "cache" / "screener" / "fires_log.json"
# The setups always shown on the site. v3 was dropped (never fired), so featuring
# it would misrepresent "what the AI thinks is a good trade". `dump` joined
# 2026-07-15: it is the only setup with a positive live expectancy AND the only
# short, so hiding it while showing three flat longs told the wrong story about
# what is actually working. It is still the youngest record here (see the t-stat
# on its P&L card) — shown, not endorsed.
CORE_SETUPS = ("v1", "v2", "v4", "dump")
# Unproven setups: still logged + graded on the box (they keep building a live
# track record) but HIDDEN from the site until each earns its way back — a setup
# auto-returns once it has >= EARN_MIN_N graded episodes AND positive avg $ P&L
# (see _visible_setups()). buy15 is the pump-odds "buy signal"; the rest are the
# experimental detectors (probe/bear_trap/spike_retrace).
HIDDEN_SETUPS = ("probe", "bear_trap", "spike_retrace", "buy15")
EARN_MIN_N = 30        # graded episodes a hidden setup needs before it may show
EARN_MIN_EXP = 0.0     # ...and its avg realized P&L must be above this to unhide
JOURNAL_SETUPS = CORE_SETUPS   # back-compat alias (kept for readability below)
# Hypothetical equal stake per paper trade, so the PnL card can show a $ figure
# (Polymarket-style) while staying honest — the model risks NO real capital. The
# card labels this assumption; change it here and the whole card rescales.
PNL_STAKE = 100

# The always-on box also serves a tiny live.json (price/24h/OI/funding, refreshed
# ~60s) over HTTPS via Caddy on an sslip.io host. LIVE_JS polls it client-side and
# updates the matching cells in place, so the Manipulated tab is live without a
# rebuild. Only this page widens its CSP connect-src to allow it (page_meta arg).
LIVE_ORIGIN = "https://45-32-102-44.sslip.io"
LIVE_SRC = f"{LIVE_ORIGIN}/live.json"
STRATS = ("v1", "v2", "v4")   # v3 dropped (never fired) — not rendered on detail cards
STRAT_NAME = {"v1": "Buy v1", "v2": "Buy v2", "v3": "Buy v3", "v4": "Buy v4",
              "buy15": "Buy signal", "probe": "Probe", "dump": "Dump (short)",
              "bear_trap": "Bear trap", "spike_retrace": "Spike & retrace"}
STRAT_TITLE = {"v1": "High-control accumulation breakout",
               "v2": "Ignition (OI-confirmed coil-break)", "v3": "Washout reversal",
               "v4": "Coiled accumulation (pre-pump build)",
               "buy15": "Pump odds crossed 20%", "probe": "Pre-vertical volume break",
               "dump": "Distribution after a markup (short)",
               "bear_trap": "Flush fully reclaimed", "spike_retrace": "Spike sold back down"}
# plain-English "why the AI bought" — for the trade table (non-expert friendly)
STRAT_WHY = {"v1": "quiet OI build",
             "v2": "coil breakout on volume",
             "v3": "washout reversal",
             "v4": "quiet accumulation",
             "buy15": "odds crossed 20%", "probe": "volume break of the coil",
             "dump": "crowded longs rolling over", "bear_trap": "flush bought back",
             "spike_retrace": "spike faded, shorts crowd"}
BUY_ODDS_GATE = 20.0   # the +50% pump_score that flags a coin as a "buy signal".
                       # KEEP IN SYNC with fetch_screener.ALERT_BUY_ODDS (the box's
                       # actual fire/alert threshold) — raised 15->20 on 2026-07-04.


def _load_screener() -> None:
    if not SCREENER_FILE.exists():
        return
    try:
        d = json.loads(SCREENER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for k in ("as_of_hour", "counts", "gate", "thresholds"):
        SCREENER_META[k] = d.get(k)
    for sym, r in (d.get("tokens") or {}).items():
        SCREENER[sym.upper()] = r


def _sig(sym) -> dict:
    return SCREENER.get((sym or "").upper()) or {}


def _has_perp(sym) -> bool:
    """True if the coin has a WORKING perpetual: live perp OI, or at least one
    Buy signal with enough perp history to evaluate (not `insufficient`). Coins
    with neither (e.g. a watchlist entry whose perp never listed or got pulled)
    are dropped from the Manipulated list — it's a perp screener. Reversible: if
    a perp later appears in the screener feed, the coin returns automatically."""
    r = _sig(sym)
    if r.get("oi_bn") is not None or r.get("oi_combined") is not None:
        return True
    sigs = r.get("signals") or {}
    return any(not (sigs.get(k) or {}).get("insufficient", True)
               for k in STRATS) if sigs else False


def _ago(t, as_of=None) -> str:
    as_of = as_of or SCREENER_META.get("as_of_hour")
    if not t or not as_of:
        return ""
    h = max(0, int((as_of - t) // 3600))
    return "now" if h == 0 else f"{h}h ago"


def _buy_badges(sym, mini=False) -> str:
    s = _sig(sym).get("signals") or {}
    cls = "buy {k} mini" if mini else "buy {k}"
    out = []
    for k in STRATS:
        d = s.get(k) or {}
        if not d.get("fired"):
            continue
        ago = _ago(d.get("t"))
        sub = "" if mini else f' <i>{ago}</i>'
        out.append(f'<span class="{cls.format(k=k)}" title="{html.escape(STRAT_TITLE[k])} — '
                   f'fired {ago}; size {d.get("position") or "—"}">{STRAT_NAME[k]}{sub}</span>')
    return "".join(out)


def _venue_badges(sym) -> str:
    # BN/BYB perp-venue chips removed per request — OI(BN+BYB) column already
    # conveys the venues; the badges were visual noise.
    return ""


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


def _spark_from_series(series) -> str:
    """Build SVG spark body from a [[t, price], ...] series."""
    if len(series) < 2:
        return ""
    step = max(1, len(series) // 120)
    pts = series[::step]
    if pts[-1][0] != series[-1][0]:
        pts.append(series[-1])
    vals = [v for _t, v in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    x = lambda i: round(i / (n - 1) * _SW, 2)
    y = lambda v: round(_SH - (v - lo) / span * (_SH - 2) - 1, 2)
    line = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(vals))
    area = f"0,{_SH} {line} {_SW},{_SH}"
    return (f'<polygon class="spark-fill" points="{area}"/>'
            f'<polyline class="spark-line" points="{line}"/>')


def _spark_body(sym: str) -> str:
    if sym in _SPARK_BODIES:
        return _SPARK_BODIES[sym]
    body = ""
    p = PRICES / f"{sym}.json"
    if p.exists():
        series = json.loads(p.read_text(encoding="utf-8"))
        body = _spark_from_series(series)
    if not body:
        spark = (_sig(sym) or {}).get("spark") or []
        body = _spark_from_series(spark)
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
    """CMC-style price chart: 24h / 1W / 1M / 1Y / All.

    Stitches three resolutions into ONE series, finest-wins where they overlap:
    box-fetched 5m candles for the last ~24h, 1h candles for the last ~12d, and the
    daily 180-day history older than that. With the box's intraday candles the 24h/1W
    views are genuinely smooth and the page opens on 1W; without them it gracefully
    falls back to the daily series (opens on All). The live poller (LIVE_DETAIL_JS)
    appends a real-time point to the right edge every ~60s via `live_sym`."""
    # Daily baseline (180d). scam_prices stores ms timestamps; the screener spark
    # fallback is already in seconds.
    p = PRICES / f"{sym}.json"
    if p.exists():
        daily = [[int(t // 1000), v] for t, v in json.loads(p.read_text(encoding="utf-8"))]
    else:
        daily = [[int(t), v] for t, v in ((_sig(sym) or {}).get("spark") or [])]

    # Intraday from the box (seconds, ascending). Either tier may be absent.
    m5, h1 = [], []
    cp = PRICE_CANDLES / f"{sym}.json"
    if cp.exists():
        try:
            cd = json.loads(cp.read_text(encoding="utf-8"))
            m5 = [[int(t), v] for t, v in (cd.get("m5") or []) if v]
            h1 = [[int(t), v] for t, v in (cd.get("h1") or []) if v]
        except Exception:
            m5, h1 = [], []

    # Stitch finest-wins: daily older than h1's start, h1 older than m5's start, m5.
    m5_lo = m5[0][0] if m5 else None
    h1_lo = h1[0][0] if h1 else None
    cut_daily = h1_lo if h1_lo is not None else m5_lo
    pts = [pt for pt in daily if cut_daily is None or pt[0] < cut_daily]
    pts += [pt for pt in h1 if m5_lo is None or pt[0] < m5_lo]
    pts += m5
    # Dedupe by timestamp, ascending (a finer tier's point wins on collision).
    by_t = {}
    for t, v in pts:
        if v and v > 0:
            by_t[int(t)] = v
    points = [[t, by_t[t]] for t in sorted(by_t)]
    if len(points) < 2:
        return '<div class="missing">no price data</div>'

    ref = min(v for _t, v in points)
    dec = max(4, 2 - math.floor(math.log10(ref))) if ref > 0 else 4
    # Open on 1W when we have smooth intraday data; else on the full daily history.
    default_days = 7 if (m5 or h1) else None
    return timeseries_html(f"chart-{sym.lower()}", [{
        "data": points, "kind": "area", "color": "#1f4e79", "scale": "right",
        "name": "Price", "fmt": {"kind": "price", "dec": dec}, "fill": True,
    }], height=520, ranges=[("24h", 1), ("1W", 7), ("1M", 30), ("1Y", 365), ("All", None)],
       default_days=default_days, live_sym=sym)


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
    """Price-reaction metrics. Uses the 180-day daily price history when
    available, otherwise falls back to CMC %changes from screener market data."""
    s = _series(rec["symbol"])
    if len(s) >= 2:
        last_t, last = s[-1]
        first = s[0][1]
        DAY = 86400_000

        def chg(n_days):
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
    mkt = (_sig(rec["symbol"]) or {}).get("market") or {}
    if mkt.get("p24h") is not None or mkt.get("p7d") is not None:
        return {"since": None, "p24": mkt.get("p24h"), "p7": mkt.get("p7d"),
                "p30": mkt.get("p30d"), "p90": mkt.get("p90d"),
                "launch": None, "last": mkt.get("price"),
                "ath": None, "atl": None, "dd": None, "peak_gain": None}
    return None


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


COND_KEY_MAP = {
    "oi_3up": "oi3up", "oi_3h>=5%": "oi3h5",
    "price_3h<=5%": "px3h8", "breaks_6h_high": "brk6h",
    # v2 — ignition (OI-confirmed coil-break)
    "tight_coil<=45%": "coiltt", "vol_ignition>=5x": "volign",
    "oi_surge>=15%": "oisrg", "breaks_coil_high": "brkcoil", "funding_cold": "fundcold",
    "oi/price>=2.0": "oipx20",
    "funding<0.1%": "fund01", "funding<0.05%": "fund005", "|funding_8h|<=0.02%": "fund002",
    "oi_dd>=15%": "oidd15", "price_dd<=10%": "pxdd10",
    "price/oi_dd<=0.5": "pxoidd", "oi_rebuild>=8%": "oireb8",
    "breaks_postwashout_high": "brkwash",
    # v4 — coiled accumulation
    "oi_leads_price>=2x": "oilead2",
    "oi_build_72h>=40%": "oibld72", "price_flat_72h<=25%": "pxflat72",
    "coiling_range<=45%": "coil45",
    "funding_squeeze_or_trend": "fundsqz",
}


def _cond_str(sym) -> str:
    """Pipe-delimited string of condition short-keys that are TRUE for this coin."""
    sigs = (_sig(sym) or {}).get("signals") or {}
    passed = []
    for strat in STRATS:
        for raw_key, ok in ((sigs.get(strat) or {}).get("conditions") or {}).items():
            short = COND_KEY_MAP.get(raw_key)
            if ok and short and short not in passed:
                passed.append(short)
    return "|" + "|".join(passed) + "|" if passed else "||"


def _filter_attrs(rec) -> str:
    """The data-* attributes the index filter JS reads. Shared by the tile and
    the list row so both filter identically."""
    sym = rec["symbol"]
    r = _sig(sym)
    search = html.escape(f"{rec.get('name', sym)} {sym}".lower())
    fdv = rec.get("fdv") or rec.get("csv_fdv") or r.get("fdv")
    oi = r.get("oi_combined")
    mc = rec.get("mcap") or rec.get("csv_mc")
    oimc = (oi / mc) if (oi and mc) else -1
    oifdv = (oi / fdv) if (oi and fdv) else -1
    fnd = r.get("funding")
    ov = _oi_spotvol(rec)
    oivol = ov["ratio"] if ov else -1
    return (f'data-search="{search}" data-fdvnum="{fdv if fdv else -1:.0f}" '
            f'data-mcnum="{mc if mc else -1:.0f}" '
            f'data-fundingnum="{fnd if fnd is not None else 999:.8f}" '
            f'data-oi="{oi or 0:.0f}" data-oimc="{oimc:.6f}" '
            f'data-oifdv="{oifdv:.6f}" data-oivol="{oivol:.4f}" '
            f'data-cond="{_cond_str(sym)}"')


def _tile(rec) -> str:
    sym = rec["symbol"]
    r = _sig(sym)
    fdv = rec.get("fdv") or rec.get("csv_fdv") or r.get("fdv")
    oi = r.get("oi_combined")
    px = rec.get("price") or rec.get("csv_price")
    buys = _buy_badges(sym, mini=True)
    metas = []
    if r.get("pump_score") is not None:
        ps = r["pump_score"]
        pc = "pump-hi" if ps >= 15 else ("pump-mid" if ps >= 8 else "pump-lo")
        metas.append(f'<span class="{pc}" title="{_PUMP_TITLE}"><b>Pump</b> {_pump_pct(ps)}</span>')
    if px:
        metas.append(f'<span><b>Price</b> {fmt_subscript_price(px)}</span>')
    metas.append(f'<span><b>OI</b> {_usd(oi)}</span>')
    metas.append(f'<span><b>FDV</b> {_usd(fdv)}</span>')
    if r.get("oi_fdv_pct") is not None:
        metas.append(f'<span><b>OI/FDV</b> {r["oi_fdv_pct"]:.0f}%</span>')
    ov = _oi_spotvol(rec)
    if ov:
        sfx = "" if ov["src"] == "spot" else "<i> (perp vol)</i>"
        metas.append(f'<span><b>OI/Vol</b> {ov["ratio"]:.1f}×{sfx}</span>')
    return f"""
    <a class="tile" data-sym="{html.escape(sym)}" href="{sym.lower()}.html" {_filter_attrs(rec)}>
      {_sparkline(sym)}
      <div class="tile-body">
        <div class="tile-head"><span class="name">{html.escape(rec.get('name', sym))}</span>
          <span class="sym">{html.escape(sym)}</span>{_venue_badges(sym)}{_flag_chips(rec)}</div>
        <div class="buyrow" data-buyrow>{buys}</div>
        <div class="tile-meta">{"".join(metas)}</div>
      </div>
    </a>"""


# Screener-focused list: Binance + combined OI, the OI/FDV and OI/MC ratios, and
# funding drive the scan; price 24h + memo carry context. Full rich data
# (chart/holders/funding rounds) stays on each coin's detail page. 11 cols — keep
# the nth-child widths in EXTRA_CSS in sync. Sort JS reads the header index +
# <td data-s>, so column changes need no JS change (but the LIVE_JS column
# resolver matches header TEXT — keep these labels in sync with it).
# Pump % = the per-coin PumpFinder +50% probability; Old % = the previous-gen
# logistic engine (for comparison). The per-setup v1/v2/v4 columns were removed per
# owner — those signals still live on the tiles, detail page, filters, and the AI
# Track Record sub-tab. The +25%/+10% odds live on the Pump Odds sub-tab.
LIST_COLS = ["#", "Token", "Pump %", "Old %", "Price / 24h",
             "BN OI", "OI (BN+BYB)", "Funding", "Vol", "FDV", "MC"]


def _pump_pct(s) -> str:
    """The pump score is a CALIBRATED PROBABILITY (P of +50% within 72h), so we
    show it straight as a percent. Sub-1% rounds to "<1%" (honest: very unlikely,
    not impossible) rather than a flat "0%"."""
    if s is None:
        return "—"
    return f"{s:.0f}%" if s >= 1 else "&lt;1%"


_PUMP_TITLE = "Model's predicted chance this pumps +50% within 72h (base rate ~5%; 15%+ is strong)"
_PUMP_TITLE_25 = "Model's predicted chance this moves +25% within 72h"
_PUMP_TITLE_10 = "Model's predicted chance this moves +10% within 72h"
_PUMP_TITLE_OLD = ("Previous-generation engine (linear logistic) for comparison. "
                   "PumpFinder (the Pump % column) replaced it — it was less "
                   "calibrated and tended to over-state already-pumped coins.")


def _odds_cell(s, title) -> str:
    """A probability cell (0-100): shared pump hi/mid/lo grading + data-s sort hook.
    Used for Pump % (+50%), Old % (logistic), and the +25%/+10% Pump-Odds columns."""
    if s is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    cls = "pump-hi" if s >= 15 else ("pump-mid" if s >= 8 else "pump-lo")
    return f'<td class="n pump {cls}" data-s="{s:.1f}" title="{title}">{_pump_pct(s)}</td>'


def _pump_cell(r) -> str:
    """Pump-probability % — calibrated chance of +50% within 72h. From screener.json."""
    return _odds_cell(r.get("pump_score"), _PUMP_TITLE)


def _pump_cell_old(r) -> str:
    """The OLD logistic engine's +50% score, rendered like Pump % for comparison."""
    return _odds_cell(r.get("pump_score_log"), _PUMP_TITLE_OLD)


def _price24_cell(rec) -> str:
    """24h column: current price (main line) + the 24h % change (colored
    subtitle). Sorts by the 24h move so the column matches its label and the
    default trending sort."""
    px = rec.get("price") or rec.get("csv_price")
    p24 = (_perf(rec) or {}).get("p24")
    if px is None and p24 is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    pxt = fmt_subscript_price(px) if px else "—"
    # `pxmain` span + data-sym-driven cell let the live poller (LIVE_JS) swap the
    # price/24h in place without a rebuild; same structure it re-renders to, so
    # only genuinely-changed cells flash on the first poll.
    if p24 is None:
        return f'<td class="n" data-live="px24" data-s="{_NEG_INF}"><span class="pxmain">{pxt}</span></td>'
    cls = "pos" if p24 >= 0 else "neg"
    return (f'<td class="n" data-live="px24" data-s="{p24:.4f}"><span class="pxmain">{pxt}</span>'
            f'<span class="p24 {cls}">{p24:+.1f}%</span></td>')


def _fundcell(r) -> str:
    """Perp funding-rate cell (per-interval %), from the screener — distinct from
    the VC funding-rounds shown on the detail page."""
    f = r.get("funding")
    if f is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    return f'<td class="n" data-s="{f:.8f}">{f * 100:.3f}%</td>'


def _num_chg_cell(v, chg, tip="") -> str:
    """Compact-USD value cell with an optional colored 24h % change subtitle (the
    OI and Vol columns). data-s stays the raw value so the column still sorts by
    magnitude; the `.vmain` span lets the live poller swap the value in place
    without wiping the (build-time) change subtitle."""
    if v is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    ti = f' title="{html.escape(tip)}"' if tip else ""
    main = f'<span class="vmain">{fmt_usd_compact(v)}</span>'
    if chg is None:
        return f'<td class="n" data-s="{v:.4f}"{ti}>{main}</td>'
    cls = "pos" if chg >= 0 else "neg"
    return (f'<td class="n" data-s="{v:.4f}"{ti}>{main}'
            f'<span class="vchg {cls}">{chg:+.1f}%</span></td>')


def _chg_span(p) -> str:
    """Inline colored 24h % change badge for the detail-page stat spans. Empty
    when unknown. Placed AFTER the value (the live poller updates the value text
    node via b.nextSibling, so a trailing badge survives the refresh)."""
    if p is None:
        return ""
    return f'<span class="vchg {"pos" if p >= 0 else "neg"}">{p:+.1f}%</span>'


def _list_row(rec) -> str:
    sym = rec["symbol"]
    r = _sig(sym)
    search = html.escape(f"{rec.get('name', sym)} {sym}".lower())
    fdv = rec.get("fdv") or rec.get("csv_fdv") or r.get("fdv")
    mcap = rec.get("mcap") or rec.get("csv_mc") or r.get("mcap")
    oi_bn = r.get("oi_bn")
    oi = r.get("oi_combined")
    vol = rec.get("vol") or rec.get("csv_vol") or (r.get("market") or {}).get("vol24h")
    chg = _oi_vol_chg(sym)
    tok =(f'<td class="tok" data-s="{search}"><a href="{sym.lower()}.html">{_sparkline(sym)}'
           f'<span class="lname"><span class="lnm">{html.escape(rec.get("name", sym))}</span>'
           f'<span class="sym">{html.escape(sym)}</span></span>{_venue_badges(sym)}'
           f'<span class="lbuys" data-buyrow>{_buy_badges(sym, mini=True)}</span></a></td>')
    return (
        f'<tr class="lrow" data-sym="{html.escape(sym)}" {_filter_attrs(rec)}>'
        f'<td class="rank"></td>{tok}'
        f'{_pump_cell(r)}'                               # Pump % (PumpFinder, +50%)
        f'{_pump_cell_old(r)}'                           # Old % (previous logistic engine)
        f'{_price24_cell(rec)}'                          # Price / 24h
        f'{_num_cell(oi_bn, pct=False, color=False)}'    # BN OI
        f'{_num_chg_cell(oi, chg["oi"], "24h change in tracked-venue perp OI")}'   # OI (BN+BYB)
        f'{_fundcell(r)}'                                # Funding
        f'{_num_chg_cell(vol, chg["vol"], "24h change in tracked-venue perp 24h volume")}'  # Vol
        f'{_num_cell(fdv, pct=False, color=False)}'      # FDV
        f'{_num_cell(mcap, pct=False, color=False)}</tr>')   # MC


def _reaction_block(rec) -> str:
    """Price-reaction stats + checkpoints, mirroring the reactions detail page,
    computed from the token's 180-day price history."""
    p = _perf(rec)
    if not p:
        return ""
    def _px(v): return fmt_subscript_price(v) if v else '—'
    stats = f"""
    <div class="stat"><span class="k">First (180d)</span><span class="v">{_px(p['launch'])}</span></div>
    <div class="stat"><span class="k">All-time high</span><span class="v">{_px(p['ath'])}</span></div>
    <div class="stat"><span class="k">All-time low</span><span class="v">{_px(p['atl'])}</span></div>
    <div class="stat"><span class="k">Since (180d)</span><span class="v">{_pct(p['since']) if p['since'] is not None else '—'}</span></div>
    <div class="stat"><span class="k">Peak gain</span><span class="v">{_pct(p['peak_gain']) if p['peak_gain'] is not None else '—'}</span></div>
    <div class="stat"><span class="k">Max drawdown</span><span class="v">{_pct(p['dd']) if p['dd'] is not None else '—'}</span></div>"""
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
# Chains we have a KEYLESS top-holder source for (kept in sync with fetch_holders:
# GoPlus covers these EVM chains; Ethplorer is the Ethereum fallback). Used to
# tell "we don't have the data yet" apart from "no source covers this chain".
HOLDER_SOURCE_CHAINS = {
    "ethereum", "binance-smart-chain", "base", "polygon-pos",
    "arbitrum-one", "optimistic-ethereum", "avalanche",
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


_OIVOL_CHG_CACHE: dict = {}


def _oi_vol_chg(sym) -> dict:
    """24h % change in tracked-venue perp OI and 24h volume, from the logged
    cache/perp_history snapshots. Compares the latest snapshot to the one closest
    to 24h earlier (must be within 12h of the mark, so a logging gap can't fake a
    baseline). Returns {"oi": pct|None, "vol": pct|None}; both None when history
    is too short. NB: basis is the tracked-venue perp series (total_oi_usd /
    vol24h_usd) — the same series the detail-page OI/volume charts plot."""
    sym = sym.upper()
    if sym in _OIVOL_CHG_CACHE:
        return _OIVOL_CHG_CACHE[sym]
    out = {"oi": None, "vol": None}
    p = PERP_HIST / f"{sym}.json"
    if p.exists():
        try:
            series = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            series = []
        if isinstance(series, list) and len(series) >= 2:
            series = sorted(series, key=lambda pt: pt.get("t", 0))
            last = series[-1]
            target = last.get("t", 0) - 86400
            base = min(series[:-1], key=lambda pt: abs(pt.get("t", 0) - target))
            if abs(base.get("t", 0) - target) <= 43200:   # within 12h of the 24h mark

                def _d(key):
                    a, b = last.get(key), base.get(key)
                    return (a / b - 1) * 100 if (a is not None and b) else None

                out = {"oi": _d("total_oi_usd"), "vol": _d("vol24h_usd")}
    _OIVOL_CHG_CACHE[sym] = out
    return out


# Screening thresholds (documented in docs/OI_VOLUME_METHODOLOGY.md): OI/vol
# ≥ 0.5 = open positions are half a day's trading — parked OI, squeeze fuel;
# |annualized OI-weighted funding| ≥ 100%/yr = someone is paying heavily to
# hold a position/the price. Either is the watchlist's manipulation tell.
FLAG_OI_VOL = 0.5
FLAG_FUNDING_ANN = 1.0
# Report (妖币猎场) Dimension 4 / Rule 2: combined perp OI ≥ 3× the 24h SPOT
# volume = price is set by the perp book, not real spot trading (a naked
# short-squeeze trap). Distinct from FLAG_OI_VOL above, which is tracked-venue
# OI / *perp* volume. This one is OI(BN+BYB) / *spot* (market) volume.
FLAG_OI_SPOTVOL = 3.0


def _oi_spotvol(rec) -> dict | None:
    """The report's OI/24h-spot-volume ratio. Numerator = combined perp OI
    (BN+BYB, the headline OI). Denominator = the token's 24h SPOT volume (CMC
    market volume — perp volume can be wash-inflated, so spot is the faithful
    figure). Falls back to tracked-venue perp volume (tagged src='perp') only
    when spot volume is missing. Returns {ratio, vol, src} or None."""
    oi = _sig(rec["symbol"]).get("oi_combined")
    if not oi:
        return None
    vol = rec.get("vol") or rec.get("csv_vol") or (_sig(rec["symbol"]).get("market") or {}).get("vol24h")
    src = "spot"
    if not vol:                                  # labeled fallback: perp turnover
        venues = [v for v in ((_load_perp(rec["symbol"]) or {}).get("venues") or [])
                  if not v.get("is_others")]
        vol = sum(v["vol24h_usd"] for v in venues if v.get("vol24h_usd")) or None
        src = "perp"
    if not vol or vol <= 0:
        return None
    return {"ratio": oi / vol, "vol": vol, "src": src}


def _oi_spotvol_stat(rec) -> str:
    """Detail-page OI/Vol stat span (empty when no ratio). Bolds the ≥3 case as a
    flag — the report's 'perp-priced' threshold."""
    ov = _oi_spotvol(rec)
    if not ov:
        return ""
    vlbl = "spot" if ov["src"] == "spot" else "perp"
    cls = " hot" if ov["ratio"] >= FLAG_OI_SPOTVOL else ""
    tip = (f"combined OI ÷ 24h {vlbl} volume. ≥3 = price set by the perp book, "
           f"not real spot trading (report Dimension 4)")
    sfx = "" if ov["src"] == "spot" else " (perp vol)"
    return (f'<span class="ovstat{cls}" title="{html.escape(tip)}">'
            f'<b>OI/Vol</b> {ov["ratio"]:.1f}×{sfx}</span>')


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
    flags = list(_screen(rec)["flags"])
    ov = _oi_spotvol(rec)
    if ov and ov["ratio"] >= FLAG_OI_SPOTVOL:
        vlbl = "spot" if ov["src"] == "spot" else "perp"
        flags.append(("perp-priced",
                      f"OI is {ov['ratio']:.1f}× the 24h {vlbl} volume — price set by "
                      f"the perp book, not real spot trading (squeeze trap; report Dim 4)"))
    chips = "".join(
        f'<span class="flag" title="{html.escape(tip)}">⚠ {html.escape(label)}</span>'
        for label, tip in flags)
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
        eff_chain = chain or rec.get("chain")
        nm = html.escape(CHAIN_NAMES.get(eff_chain, eff_chain) if eff_chain else "this chain")
        contract = rec.get("contract") or (h or {}).get("contract")
        # Three distinct reasons, so we never wrongly blame the chain:
        #  - the chain HAS a keyless holder source (GoPlus EVM / Ethplorer) but
        #    we just don't have the data yet → it fills on the next holder fetch;
        #  - the token has no on-chain contract we can query at all;
        #  - the chain genuinely has no keyless holder source we can use.
        if eff_chain in HOLDER_SOURCE_CHAINS and contract:
            msg = (f'Top-holder data for {nm} hasn’t been fetched yet '
                   '— it fills in on the next holder refresh.')
        elif not contract:
            msg = ('No on-chain contract is recorded for this token, so '
                   'top-holder data can’t be sourced.')
        else:
            msg = f'No keyless on-chain holder source covers {nm}.'
        return f'<div class="missing">Top-holder data unavailable — {msg}</div>'
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


def _dt_hour(ts) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d %H:00")


def _binance_btn(sym) -> str:
    return (f'<a class="binance-btn" href="https://www.binance.com/en/futures/{html.escape(sym)}USDT" '
            f'target="_blank" rel="noopener">Trade {html.escape(sym)} on Binance Futures ↗</a>')


def _signals_section(sym, rec=None) -> str:
    """Buy v1/v2/v4 cards + combined OI / FDV / funding + a Binance link, from
    cache/screener (hourly box). Empty for coins with no Binance perp."""
    r = _sig(sym)
    if not r or r.get("data") == "no_perp":
        return ""
    s = r.get("signals") or {}
    as_of = SCREENER_META.get("as_of_hour")
    # `sigmeta` cells the live poller (LIVE_DETAIL_JS) refreshes every ~60s,
    # matched by their <b> label — OI (BN+BYB) / Binance OI / Funding / Binance
    # 24h vol come straight from the box screener, so they stay live without a
    # rebuild. FDV / OI/FDV / Bybit OI move slowly (hourly).
    chg = _oi_vol_chg(sym)
    _ps = r.get("pump_score")
    meta = (f'<div class="sigmeta">'
            f'<span title="{_PUMP_TITLE}"><b>Pump chance</b> {_pump_pct(_ps)}</span>'
            f'<span title="{_PUMP_TITLE_25}"><b>+25% odds</b> {_pump_pct(r.get("pump_score_25"))}</span>'
            f'<span title="{_PUMP_TITLE_10}"><b>+10% odds</b> {_pump_pct(r.get("pump_score_10"))}</span>'
            f'<span title="24h change in tracked-venue perp OI"><b>OI (BN+BYB)</b> {_usd(r.get("oi_combined"))}{_chg_span(chg["oi"])}</span>'
            f'<span><b>Binance OI</b> {_usd(r.get("oi_bn"))}</span>'
            f'<span><b>Bybit OI</b> {_usd(r.get("oi_byb"))}</span>'
            f'<span><b>FDV</b> {_usd(r.get("fdv"))}</span>'
            f'<span><b>OI/FDV</b> {(format(r["oi_fdv_pct"], ".1f") + "%") if r.get("oi_fdv_pct") is not None else "—"}</span>'
            f'<span><b>Funding</b> {(format(r["funding"] * 100, ".3f") + "%") if r.get("funding") is not None else "—"}</span>'
            f'<span title="24h change in tracked-venue perp 24h volume"><b>Binance 24h vol</b> {_usd(r.get("vol24"))}{_chg_span(chg["vol"])}</span>'
            f'{_oi_spotvol_stat(rec if rec is not None else {"symbol": sym})}'
            f'</div>')
    cards = []
    for k in STRATS:
        d = s.get(k) or {}
        if d.get("insufficient"):
            cards.append(f'<div class="buycard"><div class="bc-head">{STRAT_NAME[k]} '
                         f'<span>{STRAT_TITLE[k]}</span></div>'
                         f'<div class="bc-state na">not enough hourly data yet</div></div>')
            continue
        fired = d.get("fired")
        state = (f'<span class="buy {k}">fired {_ago(d.get("t"))}</span>' if fired
                 else '<span class="bc-no">no setup now</span>')
        extra = ""
        if fired:
            stop = d.get("stop")
            extra = (f'<div class="bc-rec">Suggested size <b>{d.get("position") or "—"}</b>'
                     + (f' · stop <b>{stop:.6g}</b>' if stop else "") + '</div>')
        conds = "".join(
            f'<li class="{"ok" if ok else "x"}">{"✓" if ok else "✗"} {html.escape(c)}</li>'
            for c, ok in (d.get("conditions") or {}).items())
        cards.append(f'<div class="buycard{" fired" if fired else ""}">'
                     f'<div class="bc-head">{STRAT_NAME[k]} <span>{STRAT_TITLE[k]}</span>{state}</div>'
                     f'{extra}<ul class="conds">{conds}</ul></div>')
    # Extra detectors (probe/dump/bear_trap/spike_retrace) are HIDDEN from the site
    # while they build a live record — they render here only if they've earned their
    # way back via _visible_setups() (see the trades tab). Still logged+graded on the box.
    EXTRA = {
        "probe": ("⚡ Probe day", "volume break out of the coil — the pre-vertical test",
                  "probe", "2.5× lift to a +50% pump"),
        "dump": ("🔻 Distribution — short", "crowded longs rolling over after a markup",
                 "dump", "6.2× lift to a −20% drop"),
        "bear_trap": ("🪤 Bear trap", "a ≥15% flush fully reclaimed — the dip got bought back",
                      "beartrap", "4.6× lift to a +50% pump"),
        "spike_retrace": ("🌊 Spike & retrace", "a +30% spike sold back down while shorts crowd in",
                          "spikeret", "2.9× lift to a +50% pump"),
    }
    _vis = _visible_setups_cached()
    for k, (nm, ttl, css, cred) in EXTRA.items():
        if k not in _vis:
            continue
        d = s.get(k) or {}
        if not d or d.get("insufficient"):
            continue
        fired = d.get("fired")
        state = (f'<span class="buy {css}">fired {_ago(d.get("t"))}</span>' if fired
                 else '<span class="bc-no">no setup now</span>')
        extra = ""
        if fired:
            stop = d.get("stop")
            slbl = "invalidated above" if k == "dump" else "stop"
            extra = (f'<div class="bc-rec">Suggested size <b>{d.get("position") or "—"}</b>'
                     + (f' · {slbl} <b>{stop:.6g}</b>' if stop else "") + '</div>')
        conds = "".join(
            f'<li class="{"ok" if ok else "x"}">{"✓" if ok else "✗"} {html.escape(c)}</li>'
            for c, ok in (d.get("conditions") or {}).items())
        cards.append(f'<div class="buycard xcard {css}{" fired" if fired else ""}">'
                     f'<div class="bc-head">{nm} <span>{ttl}</span>{state}</div>'
                     f'<div class="bc-cred" title="historical backtest, 207k coin-hours">'
                     f'⌁ {cred} <i>(backtest)</i></div>'
                     f'{extra}<ul class="conds">{conds}</ul></div>')
    return (f'<section class="card span" data-livesym="{html.escape(sym)}"><h3>Perp signals '
            f'<span class="asof">hourly Binance OI · Buy setups · as of '
            f'{_dt_hour(as_of) if as_of else "—"} UTC</span>'
            f'<span id="live-badge" class="live-wait" title="OI · funding · Binance volume · price update live every ~60s">● connecting…</span></h3>'
            f'{meta}<div class="buycards">{"".join(cards)}</div>{_binance_btn(sym)}'
            f'<p class="note">Buy v1, v2 &amp; v4 are mechanical long setups on open interest, '
            f'price and funding — the model\'s live trades are tracked on the '
            f'<a href="trades.html">AI Trades</a> tab. Research signals, not financial advice.'
            f'</p></section>')


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
    buys = _buy_badges(sym)
    flagrow = f'<div class="flagrow">{buys}{chips}</div>' if (chips or buys) else ""
    memo = html.escape(rec.get("memo_en") or "—")
    mc, fdv = rec.get("mcap") or rec.get("csv_mc"), rec.get("fdv") or rec.get("csv_fdv")
    fdvmc = f"<dt>FDV / MC</dt><dd>{fdv / mc:.1f}×</dd>" if (mc and fdv) else ""
    body = f"""
<header style="display:flex;align-items:center"><a class="back" href="index.html">← all watchlist tokens</a>{theme_toggle_button()}</header>
<main><section class="card">
  <div class="info">
    <h2>{name} <span class="sym">{html.escape(sym)}</span> {_venue_badges(sym)}</h2>
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
{_signals_section(sym, rec)}
{_supply_valuation_block(rec)}
<section class="card span">
  <h3>Top holders <span class="asof">on-chain distribution</span></h3>
  {_holders_block(rec, platforms)}
</section></main>
{LIVE_DETAIL_JS if _sig(sym) and _sig(sym).get("data") != "no_perp" else ""}
{THEME_JS}"""
    title = f"{rec.get('name', sym)} ({sym}) — Manipulated"
    desc = (f"{rec.get('name', sym)} ({sym}) on the Manipulated watchlist — price history, "
            f"per-venue perp OI & funding, OI/volume trend, supply and top holders.")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{page_meta(title, desc, connect_extra=LIVE_ORIGIN)}'
            f'<title>{name} ({html.escape(sym)}) — Manipulated</title>'
            f'<link rel="stylesheet" href="style.css?v={_CSS_VER}">'
            f'<script src="../report/lightweight-charts.standalone.production.js"></script>'
            f'</head><body>{body}</body></html>')


def _filter_bar() -> str:
    def _cb(key, label):
        return (f'<label class="fcb"><input type="checkbox" data-k="{key}">'
                f'<span>{label}</span></label>')
    return """
<div class="filters">
  <input id="search" type="search" placeholder="Search coin…" autocomplete="off">
  <span class="viewtoggle">
    <button id="btn-filter" type="button">⚙ Filter</button>
    <button id="view-grid" type="button" class="active">▦ Thumbnails</button>
    <button id="view-list" type="button">☰ List</button></span>
  <span id="count" class="count"></span>
</div>
<div id="fpanel" class="fpanel" style="display:none">
  <div class="fp-presets">
    <button class="fp-pre" data-pre="v1">Buy v1</button>
    <button class="fp-pre" data-pre="v2">Buy v2</button>
    <button class="fp-pre" data-pre="v4">Buy v4</button>
  </div>
  <div class="fp-grid">
    <fieldset class="fp-group"><legend>OI momentum</legend>
      """ + _cb("oi3up", "OI rising 3 candles") + _cb("oi3h5", "OI 3h ≥5%") + """
    </fieldset>
    <fieldset class="fp-group"><legend>Price action</legend>
      """ + _cb("px3h8", "Price 3h ≤5%") + _cb("brk6h", "Breaks 6h high") + """
    </fieldset>
    <fieldset class="fp-group"><legend>Ignition (v2)</legend>
      """ + _cb("coiltt", "Tight coil ≤45%") + _cb("volign", "Volume ≥5×") + _cb("oisrg", "OI surge ≥15%") + _cb("brkcoil", "Breaks coil high") + _cb("fundcold", "Funding cold") + """
    </fieldset>
    <fieldset class="fp-group"><legend>OI / price ratio</legend>
      """ + _cb("oipx20", "OI/price ≥2×") + """
    </fieldset>
    <fieldset class="fp-group"><legend>Funding</legend>
      """ + _cb("fund01", "Funding &lt;0.1%") + _cb("fund002", "|Funding 8h| ≤0.02%") + """
    </fieldset>
    <fieldset class="fp-group"><legend>Coiled accumulation (v4)</legend>
      """ + _cb("oibld72", "OI +40% (72h)") + _cb("pxflat72", "Price flat ≤25% (72h)") + _cb("coil45", "Coiling range ≤45%") + _cb("oilead2", "OI leads price ≥2×") + _cb("fundsqz", "Funding squeeze/trend") + """
    </fieldset>
    <fieldset class="fp-group"><legend>Funding level</legend>
      """ + _cb("fundneg", "Funding &lt;0 (squeeze)") + _cb("fundn01", "Funding ≤ −0.1%") + _cb("fundn03", "Funding ≤ −0.3%") + """
    </fieldset>
    <fieldset class="fp-group"><legend>Thresholds</legend>
      """ + _cb("oi5m", "OI &gt; $5M") + _cb("oi10m", "OI &gt; $10M") + _cb("fdvlt150", "FDV &lt; $150M") + _cb("fdvgte150", "FDV ≥ $150M") + _cb("oifdv8", "OI/FDV ≥ 8%") + _cb("oifdv15", "OI/FDV ≥ 15%") + _cb("oimc25", "OI/MC ≥ 25%") + _cb("oimc50", "OI/MC &gt; 50%") + _cb("oivol3", "OI/Vol ≥ 3× (perp-priced)") + """
    </fieldset>
  </div>
  <div class="fp-actions">
    <button class="fp-apply" id="fp-apply" type="button">Apply</button>
    <button class="fp-clear" id="fp-clear" type="button">Clear</button>
    <span class="fp-hint" id="fp-hint"></span>
  </div>
</div>"""


# Client-side live updater: polls the box's live.json every ~60s and swaps the
# 24h (price+%), OI(BN+BYB) and Funding cells in the list + the Price/OI tile
# metas in place. Column positions are resolved from the header text (survives a
# column reorder). Formatting mirrors fmt_subscript_price / fmt_usd_compact and
# the Python cell formatters so live values look identical to a fresh build.
LIVE_JS = (
    "<script>(function(){"
    f'var SRC="{LIVE_SRC}";'
    'var SUB="\\u2080\\u2081\\u2082\\u2083\\u2084\\u2085\\u2086\\u2087\\u2088\\u2089";'
    'function fmtUsd(v){if(v==null)return "\\u2014";v=+v;if(!isFinite(v)||v===0)return "\\u2014";'
    'if(v>=1e9)return "$"+(v/1e9).toFixed(2)+"B";if(v>=1e6)return "$"+(v/1e6).toFixed(1)+"M";'
    'if(v>=1e3)return "$"+(v/1e3).toFixed(1)+"K";return "$"+Math.round(v).toLocaleString("en-US");}'
    'function fmtPrice(p){if(p==null)return "\\u2014";p=+p;if(!(p>0))return "$0";'
    'if(p>=1)return "$"+p.toLocaleString("en-US",{minimumFractionDigits:4,maximumFractionDigits:4});'
    'var s=p.toFixed(20).split(".")[1],st=s.replace(/^0+/,""),nz=s.length-st.length;'
    'if(nz<4)return "$"+p.toFixed(nz+3);'
    'var dg=(st+"000").slice(0,3),sb=String(nz).split("").map(function(d){return SUB[+d];}).join("");'
    'return "$0.0"+sb+dg;}'
    'function flash(el){if(!el)return;el.classList.remove("liveflash");void el.offsetWidth;el.classList.add("liveflash");}'
    'function setCell(td,h,sv){if(!td)return;if(sv!=null)td.setAttribute("data-s",sv);if(td.innerHTML!==h){td.innerHTML=h;flash(td);}}'
    # OI/Vol cells carry a build-time 24h change badge (.vchg); update only the .vmain value so the badge survives the live refresh.
    'function setVmain(td,txt,sv){if(!td)return;if(sv!=null)td.setAttribute("data-s",sv);var m=td.querySelector(".vmain");if(!m){setCell(td,txt,sv);return;}if(m.textContent!==txt){m.textContent=txt;flash(td);}}'
    'function p24cell(price,p24){var pxt=fmtPrice(price);'
    'if(p24==null)return "<span class=\\"pxmain\\">"+pxt+"</span>";'
    'var cls=p24>=0?"pos":"neg",sg=p24>=0?"+":"";'
    'return "<span class=\\"pxmain\\">"+pxt+"</span><span class=\\"p24 "+cls+"\\">"+sg+p24.toFixed(1)+"%</span>";}'
    'function tileMeta(t,label,txt){var sp=t.querySelectorAll(".tile-meta span");'
    'for(var i=0;i<sp.length;i++){var b=sp[i].querySelector("b");if(b&&b.textContent.trim()===label){'
    'var n=sp[i].lastChild;if(n&&n.nodeType===3&&n.textContent!==" "+txt){n.textContent=" "+txt;flash(sp[i]);}return;}}}'
    'function buyBadges(sig){var o="";["v1","v2","v4"].forEach(function(k){'
    'if(sig&&sig[k])o+="<span class=\\"buy "+k+" mini\\" title=\\""+k+" fired\\">Buy "+k+"</span>";});return o;}'
    # Rebuild the pipe-delimited data-cond string from the live fired flags, so
    # the (checkbox-driven) Buy v1-v4 preset filters track the live verdicts:
    # a strategy fires iff ALL its conditions are true, so the union of fired
    # strategies' condition keys is exactly what their checkboxes test for.
    'function condFromSig(sig){var P=window.__PRESETS;if(!P)return null;var seen={},o="|";'
    '["v1","v2","v4"].forEach(function(k){if(sig&&sig[k])(P[k]||[]).forEach(function(c){'
    'if(!seen[c]){seen[c]=1;o+=c+"|";}});});return o==="|"?"||":o;}'
    'function setCount(id,val){var el=document.getElementById(id);'
    'if(el&&val!=null&&el.textContent!==String(val)){el.textContent=val;flash(el);}}'
    'var tab=document.getElementById("ltab");if(!tab)return;'
    'var ths=tab.tHead.rows[0].cells,ci={};'
    'for(var i=0;i<ths.length;i++){var t=ths[i].textContent.trim().toLowerCase();'
    'if(t.indexOf("24h")>-1||t.indexOf("price")>-1)ci.px24=i;'
    'else if(t==="bn oi")ci.oibn=i;'
    'else if(t.indexOf("oi (bn+byb)")>-1)ci.oicomb=i;'
    'else if(t.indexOf("funding")>-1)ci.fund=i;'
    'else if(t==="vol"||t.indexOf("volume")>-1)ci.vol=i;}'
    'var rb={},rows=tab.tBodies[0].rows;'
    'for(var r=0;r<rows.length;r++){var s=rows[r].getAttribute("data-sym");if(s)rb[s]=rows[r];}'
    'var tb={};document.querySelectorAll(".tile[data-sym]").forEach(function(t){tb[t.getAttribute("data-sym")]=t;});'
    'var badge=document.getElementById("live-badge");'
    'function apply(d){var tk=(d&&d.tokens)||{};'
    'for(var sym in tk){var v=tk[sym],row=rb[sym];'
    # live-rewrite data-cond (drives the Buy v1-v4 preset filter) on both the
    # row and the tile, so a filtered list stays in lock-step with the header
    # counts without waiting for a site rebuild.
    'if(v.sig){var cs=condFromSig(v.sig);if(cs!=null){if(row)row.setAttribute("data-cond",cs);var tlf=tb[sym];if(tlf)tlf.setAttribute("data-cond",cs);}}'
    'if(row){if(ci.px24!=null&&(v.price!=null||v.p24!=null))setCell(row.cells[ci.px24],p24cell(v.price,v.p24),v.p24!=null?(+v.p24).toFixed(4):null);'
    'if(ci.oibn!=null&&v.oi_bn!=null)setCell(row.cells[ci.oibn],fmtUsd(v.oi_bn),(+v.oi_bn).toFixed(0));'
    'if(ci.oicomb!=null&&v.oi_combined!=null)setVmain(row.cells[ci.oicomb],fmtUsd(v.oi_combined),(+v.oi_combined).toFixed(0));'
    'if(ci.fund!=null&&v.funding!=null)setCell(row.cells[ci.fund],(v.funding*100).toFixed(3)+"%",(+v.funding).toFixed(8));'
    'if(ci.vol!=null&&v.vol24!=null)setVmain(row.cells[ci.vol],fmtUsd(v.vol24),(+v.vol24).toFixed(0));'
    'if(v.sig){var lbr=row.querySelector("[data-buyrow]");if(lbr){var lnb=buyBadges(v.sig);if(lbr.innerHTML!==lnb){lbr.innerHTML=lnb;flash(lbr);}}}}'
    'var tile=tb[sym];if(tile){if(v.price!=null)tileMeta(tile,"Price",fmtPrice(v.price));if(v.oi_combined!=null)tileMeta(tile,"OI",fmtUsd(v.oi_combined));'
    'if(v.sig){var br=tile.querySelector("[data-buyrow]");if(br){var nb=buyBadges(v.sig);if(br.innerHTML!==nb){br.innerHTML=nb;flash(br);}}}}}'
    'if(d.sig_counts){setCount("cnt-v1",d.sig_counts.v1);setCount("cnt-v2",d.sig_counts.v2);setCount("cnt-v4",d.sig_counts.v4);}'
    # re-run the filter (data-cond just changed) so a Buy v1-v4 preset shows
    # exactly the coins now firing, then re-apply the active sort so the row
    # order matches the live values (e.g. the top 24h mover stays on top).
    'if(window.__refilter)window.__refilter();'
    'if(window.__resort)window.__resort();'
    'if(badge){badge.textContent="\\u25cf LIVE \\u00b7 "+new Date().toLocaleTimeString();badge.className="live-on";}}'
    'function poll(){fetch(SRC,{cache:"no-store"}).then(function(r){return r.json();}).then(apply)'
    '.catch(function(){if(badge){badge.textContent="\\u25cf live paused";badge.className="live-off";}});}'
    'poll();setInterval(poll,60000);'
    "})();</script>"
)

# Per-token detail-page live updater: refreshes the perp block's OI (BN+BYB) /
# Binance OI / Funding / Binance 24h vol cells (matched by their <b> label) and
# the Price field every ~60s from the same box live.json. Scoped to the one token
# via data-livesym on the Perp-signals section.
LIVE_DETAIL_JS = (
    "<script>(function(){"
    f'var SRC="{LIVE_SRC}";'
    'var SUB="\\u2080\\u2081\\u2082\\u2083\\u2084\\u2085\\u2086\\u2087\\u2088\\u2089";'
    'function fmtUsd(v){if(v==null)return "\\u2014";v=+v;if(!isFinite(v)||v===0)return "\\u2014";'
    'if(v>=1e9)return "$"+(v/1e9).toFixed(2)+"B";if(v>=1e6)return "$"+(v/1e6).toFixed(1)+"M";'
    'if(v>=1e3)return "$"+(v/1e3).toFixed(1)+"K";return "$"+Math.round(v).toLocaleString("en-US");}'
    'function fmtPrice(p){if(p==null)return "\\u2014";p=+p;if(!(p>0))return "$0";'
    'if(p>=1)return "$"+p.toLocaleString("en-US",{minimumFractionDigits:4,maximumFractionDigits:4});'
    'var s=p.toFixed(20).split(".")[1],st=s.replace(/^0+/,""),nz=s.length-st.length;'
    'if(nz<4)return "$"+p.toFixed(nz+3);'
    'var dg=(st+"000").slice(0,3),sb=String(nz).split("").map(function(d){return SUB[+d];}).join("");'
    'return "$0.0"+sb+dg;}'
    'function flash(el){if(!el)return;el.classList.remove("liveflash");void el.offsetWidth;el.classList.add("liveflash");}'
    'var sec=document.querySelector("[data-livesym]");if(!sec)return;'
    'var sym=sec.getAttribute("data-livesym"),badge=document.getElementById("live-badge");'
    'function metaSet(label,txt){var sp=sec.querySelectorAll(".sigmeta span");'
    'for(var i=0;i<sp.length;i++){var b=sp[i].querySelector("b");if(b&&b.textContent.trim()===label){'
    # value is the text node right after the <b> label; a trailing change badge (.vchg) may follow it, so target b.nextSibling not lastChild.
    'var n=b.nextSibling;if(n&&n.nodeType===3&&n.textContent!==" "+txt){n.textContent=" "+txt;flash(sp[i]);}return;}}}'
    'function priceSet(txt){var dt=document.querySelectorAll("dl dt");'
    'for(var i=0;i<dt.length;i++){if(dt[i].textContent.trim()==="Price"){var dd=dt[i].nextElementSibling;'
    'if(dd&&dd.textContent!==txt){dd.textContent=txt;flash(dd);}return;}}}'
    'function apply(d){var v=d&&d.tokens&&d.tokens[sym];if(!v)return;'
    'if(v.oi_combined!=null)metaSet("OI (BN+BYB)",fmtUsd(v.oi_combined));'
    'if(v.oi_bn!=null)metaSet("Binance OI",fmtUsd(v.oi_bn));'
    'if(v.funding!=null)metaSet("Funding",(v.funding*100).toFixed(3)+"%");'
    'if(v.vol24!=null)metaSet("Binance 24h vol",fmtUsd(v.vol24));'
    'if(v.price!=null)priceSet(fmtPrice(v.price));'
    # append the live price to the right edge of the price chart (registered by
    # timeseries_html via live_sym) so the chart ticks in real time too.
    'if(v.price!=null&&window.__llLive&&window.__llLive[sym]){window.__llLive[sym](+v.price);}'
    'if(badge){badge.textContent="\\u25cf LIVE \\u00b7 "+new Date().toLocaleTimeString();badge.className="live-on";}}'
    'function poll(){fetch(SRC,{cache:"no-store"}).then(function(r){return r.json();}).then(apply)'
    '.catch(function(){if(badge){badge.textContent="\\u25cf live paused";badge.className="live-off";}});}'
    'poll();setInterval(poll,60000);'
    "})();</script>"
)


# Setup explainer: a button (#setup-help) opens this panel. Plain-language so a
# non-quant can read what each Buy setup looks for. Conditions mirror lib/signals.
SETUP_PANEL = """
<div id="setup-modal" class="setup-modal" hidden>
  <div class="sm-backdrop" data-close></div>
  <div class="sm-card" role="dialog" aria-modal="true" aria-label="How the setups work">
    <button class="sm-x" data-close aria-label="Close">×</button>
    <h2>How the Buy setups work</h2>
    <p class="sm-lead">Every setup scans for the fingerprint of a coin being
      quietly <b>set up before a pump</b>: money (open interest) piling in while
      the operator keeps the price calm — then the markup. The four setups catch
      different points of that arc. A green <span class="buy v1 mini">Buy</span>
      badge = firing right now. Research signals, not financial advice.</p>
    <div class="sm-grid">
      <div class="sm-setup"><h3><span class="buy v4 mini">Buy v4</span> Coiled accumulation <em>· earliest</em></h3>
        <p>Open interest building <b>+40% over ~3 days</b> while price stays
          <b>flat (±25%)</b> and tight — the quiet loading. Fires <b>days before</b>
          the vertical, so it's the earliest (and noisiest) read.</p>
        <ul><li><b>Squeeze</b> flavour — funding is negative (shorts trapped = fuel).</li>
        <li><b>Trend</b> flavour — funding calm/slightly positive with an EMA20&gt;EMA60 uptrend.</li></ul>
      </div>
      <div class="sm-setup"><h3><span class="buy v1 mini">Buy v1</span> Accumulation breakout</h3>
        <p>OI surges <b>≥5% in 3h</b> while price barely moves (<b>≤5%</b>), then
          price <b>breaks the 6h high</b> with funding still low. Catches the
          breakout as it starts — later than v4, but more confirmed.</p>
      </div>
      <div class="sm-setup"><h3><span class="buy v2 mini">Buy v2</span> Ignition (coil-break)</h3>
        <p>After a <b>tight multi-day coil</b>, price <b>breaks the coil high</b> on a
          <b>volume burst (≥5×)</b> with open interest <b>surging ≥15%</b> and funding
          still cold — leveraged money igniting the move. Catches "cold" pumps that
          skip v1/v4's quiet-accumulation tell (caught VELVET at its breakout).</p>
      </div>
    </div>
    <p class="sm-foot">Use the <b>⚙ Filter</b> panel to build your own screen
      (OI +40% / price flat / funding &lt;0 / OI/MC…), or click a preset
      (Buy v1, v2 or v4) to load that setup's conditions. Signals recompute each
      hour and update live.</p>
  </div>
</div>
<script>(function(){
  var m=document.getElementById("setup-modal"),b=document.getElementById("setup-help");
  if(!m||!b)return;
  function open(){m.hidden=false;document.body.style.overflow="hidden";}
  function close(){m.hidden=true;document.body.style.overflow="";}
  b.addEventListener("click",open);
  m.querySelectorAll("[data-close]").forEach(function(e){e.addEventListener("click",close);});
  document.addEventListener("keydown",function(e){if(e.key==="Escape"&&!m.hidden)close();});
})();</script>"""


def _load_fires() -> list:
    try:
        l = json.loads(FIRES_LOG.read_text(encoding="utf-8"))
        return l if isinstance(l, list) else []
    except Exception:
        return []


def _episodes(fires, hours=72, breadth_max=7):
    """Honest fire counting for the Models page (same rules the graders use):
    one row per (sym, setup) per 72h (collapses pre-refractory hourly re-fires),
    then market-wide clusters (> breadth_max coins, same setup, same hour = one
    market event) keep a single representative sample."""
    out, last = [], {}
    for e in sorted(fires, key=lambda e: (e.get("t") or 0, str(e.get("sym")))):
        k = (e.get("sym"), e.get("strat"))
        t = e.get("t") or 0
        if t - last.get(k, -10 ** 18) >= hours * 3600:
            out.append(e)
            last[k] = t
    csize = {}
    for e in out:
        ck = (e.get("strat"), e.get("t"))
        csize[ck] = csize.get(ck, 0) + 1
    kept, seen = [], set()
    for e in out:
        ck = (e.get("strat"), e.get("t"))
        if csize[ck] > breadth_max:
            if ck in seen:
                continue
            seen.add(ck)
        kept.append(e)
    return kept


def _dt_day(ts) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _jpct(v, signed=True) -> str:
    """Colored percent span (fraction in -> %), green/red. '—' when missing."""
    if v is None:
        return '<span class="jp">—</span>'
    pct = v * 100
    cls = "pos" if pct >= 0 else "neg"
    sg = "+" if (signed and pct >= 0) else ""
    return f'<span class="jp {cls}">{sg}{pct:.1f}%</span>'


def _real_pnl(e):
    """Realized P&L per trade — the realistic tiered exit net of fees+slippage
    (pnl_real, grade_fires #5), falling back to the idealized own-stop number for
    trades graded before that field existed."""
    o = e.get("outcome") or {}
    return o.get("pnl_real") if o.get("pnl_real") is not None else o.get("pnl_ownstop")


# The exit plan every graded trade is scored on — mirrors tools/grade_fires.py
# (TP1/TP1_FRAC/TP2/DUMP_TARGET). Sell half at +25%, the rest at +50%; otherwise
# the setup's own stop or the 72h close. Shorts (dump) cover at −20%.
TP1, TP1_FRAC, TP2 = 0.25, 0.5, 0.50
DUMP_TARGET = 0.20


def _entry_px(e):
    """The price the trade actually filled at.

    NOT `entry_price` — that is only the mark at log time, and the two diverge
    whenever detection lagged the fire (38/197 v1/v2/v4 fires are >5% apart, worst
    55.8%). grade_fires computes every P&L from `outcome.entry` (the open of the
    candle after the fire hour, no look-ahead), so the ledger must show that price
    or a row's "bought at" contradicts its own result."""
    o = e.get("outcome") or {}
    return o.get("entry") or e.get("entry_price")


def _target_px(e):
    """The price the trade was aiming to sell at ("expected sell").

    Recomputed from the real fill rather than read from the logged `tp1`/`tp2`,
    which fetch_screener anchors to `entry_price` — wrong on 83/502 long fires
    (16.5%), because the grader targets `outcome.entry * (1 + TP2)` instead."""
    entry = _entry_px(e)
    if not entry:
        return None
    if _is_short(e):
        return entry * (1 - DUMP_TARGET)
    return entry * (1 + TP2)


def _is_short(e) -> bool:
    return (e.get("outcome") or {}).get("side") == "short" or e.get("side") == "short" \
        or e.get("strat") == "dump"


def _exit_px(e):
    """The price a closed trade actually sold at ("sold at").

    grade_fires records the realized P&L but never the exit price, so reconstruct
    it from what it did record. A stop/target exit IS its own level; a 72h timeout
    closes at the final candle, recoverable by inverting grade_fires._net_hold on
    the stored buy-&-hold return:
        bh_coin = close*(1-slip) / (entry*(1+slip)) - 1 - 2*fee
    A tiered exit sold half at +25% and the rest later, so report the blended
    average of the two fills. Verified against all 564 graded long fires:
    re-deriving pnl_real from these prices reproduces the stored value to <1e-4."""
    o = e.get("outcome") or {}
    entry, reason = _entry_px(e), o.get("exit_reason")
    if not entry:
        return None
    fee = (o.get("fees_bps") or 5) / 1e4
    slip = (o.get("slip_bps") or 10) / 1e4
    bh = o.get("bh_coin")
    last = ((bh + 1 + 2 * fee) * entry * (1 + slip) / (1 - slip)) if bh is not None else None
    stop = e.get("stop")
    if _is_short(e):                      # cover at −20%, the stop above entry, or the close
        return {"stopped": stop, "target": entry * (1 - DUMP_TARGET)}.get(reason, last)
    tp1_px = entry * (1 + TP1)
    if reason == "stopped":
        return stop
    if reason == "tp2":                   # half at +25%, half at +50%
        return TP1_FRAC * tp1_px + (1 - TP1_FRAC) * entry * (1 + TP2)
    if reason == "tp1+stop" and stop:
        return TP1_FRAC * tp1_px + (1 - TP1_FRAC) * stop
    if reason == "tp1+timeout" and last is not None:
        return TP1_FRAC * tp1_px + (1 - TP1_FRAC) * last
    return last


# exit_reason -> how to describe the fill(s) behind the "sold at" price.
_EXIT_NOTE = {
    "stopped": "stopped out at the setup's safety stop",
    "timeout": "closed at the 72h mark",
    "tp2": "blended: sold half at +25%, the rest at +50%",
    "tp1+timeout": "blended: sold half at +25%, the rest at the 72h close",
    "tp1+stop": "blended: sold half at +25%, the rest at the stop",
    "target": "covered the −20% short target",
}


def _setup_stats(sid, eps) -> dict:
    """Episode-deduped live record for one setup, over pre-computed episodes `eps`
    (from _episodes). exp = mean realized P&L per graded trade — the honest edge
    read the site gates visibility on."""
    es = [e for e in eps if e.get("strat") == sid]
    g = [e for e in es if e.get("outcome")]
    wins = sum(1 for e in g if (e.get("outcome") or {}).get("pump"))
    pnls = [_real_pnl(e) for e in g if _real_pnl(e) is not None]
    return {"eps": len(es), "graded": len(g), "wins": wins,
            "hit": (wins / len(g)) if g else None,
            "exp": (sum(pnls) / len(pnls)) if pnls else None,
            "pending": len(es) - len(g)}


def _setup_curve(sid, eps) -> list:
    """[(fire_t, realized_pnl), …] for a setup's graded episodes, in fire order —
    feeds the per-model equity small-multiples (_equity_grid)."""
    es = sorted((e for e in eps if e.get("strat") == sid and _real_pnl(e) is not None),
                key=lambda e: e.get("t") or 0)
    return [(e.get("t"), _real_pnl(e)) for e in es]


def _tstat(rets) -> float | None:
    """t = mean ÷ standard error of a setup's per-trade returns. The conventional
    "probably not luck" bar is |t| >= 2."""
    n = len(rets)
    if n < 3:
        return None
    mu = sum(rets) / n
    se = ((sum((x - mu) ** 2 for x in rets) / n) / n) ** 0.5
    return (mu / se) if se else None


def _edge_verdict(rets) -> tuple:
    """Plain-English honesty label for a model's live record → (css_class, text).

    A $ total is meaningless without this. These setups run sd ≈ 15%/trade on
    n ≈ 40-70, so the standard error swamps the mean: a ±$20 move is noise. Saying
    so on the card is the whole point — reading a good week as an edge is exactly
    what sent this page's combined P&L from +$160 to −$6 and back."""
    n = len(rets)
    if n < 10:
        return ("early", f"only {n} graded — far too early to tell")
    t = _tstat(rets)
    if t is None:
        return ("early", "not enough data yet")
    if abs(t) >= 2:
        return ("proven", f"statistically real — t={t:+.2f}, unlikely to be luck")
    mu = sum(rets) / n
    sd = (sum((x - mu) ** 2 for x in rets) / n) ** 0.5
    tail = ""
    if mu > 0 and sd:
        need = int((2 * sd / mu) ** 2)
        if n < need < 10 ** 7:
            tail = f" — needs ~{need:,} trades to prove"
    return ("noise", f"not yet distinguishable from luck (t={t:+.2f}){tail}")


def _visible_setups(eps=None) -> list:
    """The setups the site shows: CORE_SETUPS always, plus any HIDDEN_SETUPS that
    have earned their way back (>= EARN_MIN_N graded episodes AND avg realized P&L
    > EARN_MIN_EXP). This is the single gate — everything the trade tab renders
    filters through it, so a hidden setup reappears automatically the moment its
    live record turns positive at a real sample size (no code change needed)."""
    if eps is None:
        eps = _episodes(_load_fires())
    vis = list(CORE_SETUPS)
    for sid in HIDDEN_SETUPS:
        st = _setup_stats(sid, eps)
        if st["graded"] >= EARN_MIN_N and (st["exp"] or 0) > EARN_MIN_EXP:
            vis.append(sid)
    return vis


_VIS_CACHE = None


def _visible_setups_cached() -> tuple:
    """Build-time cache of _visible_setups() — the fire ledger doesn't change during
    a build, so the per-coin detail pages can gate on this without re-reading it 140×."""
    global _VIS_CACHE
    if _VIS_CACHE is None:
        _VIS_CACHE = tuple(_visible_setups())
    return _VIS_CACHE


def _winrate_chart(rows, pnl_fn=_real_pnl, target_label="+50% / 72h") -> str:
    """Win rate (pnl_fn > 0) per day, broken out per setup (v1/v2/v4) — grouped
    bars, one cluster per day, bar height = that setup's win % that day (hover for
    win/total). `pnl_fn` selects the target (pump +50%/72h or mover +10%/24h). A
    static SVG (no JS) so it renders inside a hidden tab. Honest about tiny daily
    samples — far less misleading than a cumulative running average."""
    import datetime as _dt
    SETS = ("v1", "v2", "v4")
    COL = {"v1": "#2e8b57", "v2": "#3b7cc4", "v4": "#c47a3a"}

    def _day(e):                                   # bucket by the day it fired
        return (e.get("t") or 0)

    # day -> strat -> [wins, total]; plus per-strat overall for the legend
    buckets, totals = {}, {s: [0, 0] for s in SETS}
    for e in rows:
        s = e.get("strat")
        if s not in COL:
            continue
        d = _dt.datetime.fromtimestamp(_day(e), _dt.timezone.utc).strftime("%Y-%m-%d")
        wn = buckets.setdefault(d, {}).setdefault(s, [0, 0])
        won = 1 if (pnl_fn(e) or 0) > 0 else 0
        wn[0] += won
        wn[1] += 1
        totals[s][0] += won
        totals[s][1] += 1
    if not buckets:
        return ""
    days = sorted(buckets)

    W, H, L, R, T, B = 380, 188, 30, 10, 16, 28
    pw, ph = W - L - R, H - T - B
    gslot = pw / len(days)
    inner = gslot * 0.74
    subw = inner / len(SETS)
    bw = subw * 0.82

    grid = []
    for pct in (0, 50, 100):
        y = T + (1 - pct / 100) * ph
        cls = "wc-grid wc-base" if pct == 0 else "wc-grid"
        grid.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" class="{cls}"/>')
        grid.append(f'<text x="{L-6}" y="{y+3:.1f}" class="wc-yl">{pct}%</text>')

    bars = []
    for i, d in enumerate(days):
        gleft = L + gslot * i + (gslot - inner) / 2
        for j, s in enumerate(SETS):
            wn = buckets[d].get(s)
            if not wn:
                continue
            w, n = wn
            wr = w / n * 100
            bh = wr / 100 * ph
            x = gleft + subw * j + (subw - bw) / 2
            lbl = _dt.datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
            bars.append(f'<rect class="wc-bar" x="{x:.1f}" y="{T+ph-bh:.1f}" width="{bw:.1f}" '
                        f'height="{max(bh,1.4):.1f}" rx="2" fill="{COL[s]}" '
                        f'data-s="{s}" data-d="{lbl}" data-wr="{round(wr)}" data-wn="{w}/{n}" '
                        f'data-col="{COL[s]}">'
                        f'<title>{s} {lbl}: {w}/{n} ({round(wr)}%)</title></rect>')
        cx = L + gslot * (i + 0.5)
        lbl = _dt.datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
        bars.append(f'<text x="{cx:.1f}" y="{H-7:.1f}" class="wc-xl" '
                    f'text-anchor="middle">{lbl}</text>')

    leg_html = " ".join(
        f'<span class="wc-leg"><i style="background:{COL[s]}"></i>{s} '
        f'{(totals[s][0]/totals[s][1]*100):.0f}% <small>({totals[s][0]}/{totals[s][1]})</small>'
        f'</span>' for s in SETS if totals[s][1])
    svg = (f'<svg class="wc-svg" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="win rate per day by setup">'
           + "".join(grid) + "".join(bars) + '</svg>')
    return (f'<section class="card span wc-card">'
            f'<h3>Win rate per day <span class="asof">per setup · win&gt;0 · {target_label}</span></h3>'
            f'<div class="wc-legend">{leg_html}</div>{svg}'
            f"<p class=\"jnote\">Each bar is that setup's win rate on the day it fired "
            f'(hover a bar for win/total). Legend shows each setup\'s overall rate. '
            f'Daily samples are small, so read across days, not any single bar.</p></section>')


_WINRATE_TIP_JS = """
<script>(function(){
  var tip=document.createElement('div');tip.className='wc-tip';
  tip.setAttribute('role','tooltip');document.body.appendChild(tip);
  function isBar(t){return t&&t.classList&&t.classList.contains('wc-bar');}
  function show(r){tip.innerHTML='<span class="dot" style="background:'+r.getAttribute('data-col')+'"></span>'
    +'<b>'+r.getAttribute('data-s')+'</b> &middot; '+r.getAttribute('data-d')
    +' &mdash; <span class="wr">'+r.getAttribute('data-wr')+'%</span> '
    +'<span class="wn">('+r.getAttribute('data-wn')+')</span>';tip.classList.add('show');}
  function move(e){var x=e.clientX+14,y=e.clientY+14,w=tip.offsetWidth,h=tip.offsetHeight;
    if(x+w>innerWidth-8)x=e.clientX-w-14;if(y+h>innerHeight-8)y=e.clientY-h-14;
    tip.style.left=x+'px';tip.style.top=y+'px';}
  document.addEventListener('mouseover',function(e){if(isBar(e.target)){show(e.target);move(e);}});
  document.addEventListener('mousemove',function(e){if(tip.classList.contains('show')&&isBar(e.target))move(e);});
  document.addEventListener('mouseout',function(e){if(isBar(e.target))tip.classList.remove('show');});
})();</script>"""


_JOURNAL_TABS_JS = """
<script>(function(){
  var tabs=[].slice.call(document.querySelectorAll('.jtab'));
  var views={pumps:document.getElementById('view-pumps'),movers:document.getElementById('view-movers')};
  function setv(v){tabs.forEach(function(b){b.classList.toggle('active',b.dataset.view===v);});
    for(var k in views){if(views[k])views[k].style.display=(k===v)?'':'none';}
    try{localStorage.setItem('ll-journal-view',v);}catch(e){}}
  tabs.forEach(function(b){b.addEventListener('click',function(){setv(b.dataset.view);});});
  var jv='pumps';try{jv=localStorage.getItem('ll-journal-view')||'pumps';}catch(e){}
  setv(jv==='movers'?'movers':'pumps');
})();</script>"""


def scams_subnav(active: str) -> str:
    """Sub-tabs under the Manipulated top tab — Screener (the coin list) and AI
    Trades (the merged v1/v2/v4 trade ledger; absorbed the old Journal / Pump Odds
    / Buy Signals / Models pages). Both live in scams/; the Manipulated TOP tab
    stays active for both. `active` ∈ {screener, trades}."""
    tabs = [("screener", "index.html", "Screener"),
            ("trades", "trades.html", "AI Trades")]
    parts = []
    for key, href, text in tabs:
        cls = ' class="active"' if key == active else ""
        parts.append(f'<a{cls} href="{href}">{text}</a>')
    return f'<nav class="subnav">{"".join(parts)}</nav>'


def _dts(ts) -> str:
    """Compact UTC date for the journal tables (frees column width): 'Jun 26 00:00'."""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%b %d %H:%M")


def _outcome_story(o, short=False) -> str:
    """Plain-English "what happened" — to THE TRADE, not to the coin.

    Keyed on exit_reason, deliberately. It used to key on `mfe` (the coin's peak),
    which produced rows reading "🟢 Pumped … result $100": BILL peaked +59% AFTER
    stopping the trade out flat, and the table called that a win. The coin's move
    is its own column now ("Actually pumped"); this column answers what the trade
    did, and the two disagreeing is the single most informative thing on the page
    (it is how v1's too-tight stop shows itself)."""
    o = o or {}
    mfe = o.get("mfe") or 0
    peak = (f"the coin fell {mfe*100:.0f}% at best" if short
            else f"the coin peaked +{mfe*100:.0f}% at some point in the 72h")
    xr = o.get("exit_reason")
    stories = {
        "tp2": ("jhit", "🟢 Hit target", "sold half at +25%, the rest at the +50% target"),
        "target": ("jhit", "🟢 Hit target", "covered the −20% short target"),
        "tp1+timeout": ("jhit", "🟡 Half-win", "banked +25% on half; the rest closed at 72h"),
        "tp1+stop": ("jhit", "🟡 Half-win", "banked +25% on half; the rest stopped out"),
        "stopped": ("jstop", "🔴 Stopped out", "hit the safety stop and closed for a loss"),
        # "Closed at 72h", not "went nowhere" — a timeout can still land well clear of
        # flat (a dump that fell 15% without reaching its −20% target pays +11.9%).
        # How far it actually travelled is the next column's job, not this one's.
        "timeout": ("jmiss", "⚪ Closed at 72h", "never hit target or stop; closed at the 72h mark"),
    }
    cls, txt, what = stories.get(xr, ("jmiss", "⚪ Closed at 72h", "closed at the 72h mark"))
    # The painful case worth naming: stopped out, then the coin ran anyway.
    if xr == "stopped" and mfe >= 0.25 and not short:
        cls, txt = "jstop", "🔴 Stopped, then ran"
        what = "the stop closed the trade first — then the coin went on without it"
    return f'<span class="{cls}" title="{html.escape(what)} — {html.escape(peak)}">{txt}</span>'


# ── Forward paper-trade experiment: v4 + high open-interest + tight take-profit ──
# Frozen 2026-07-11 after a walk-forward test showed a tight-TP high-OI v4 long
# hitting ~90% in-sample. This panel scores ONLY fires logged AFTER the freeze —
# a genuinely out-of-sample forward ledger that grows on its own as the box logs
# and grades new fires. The rule params are derived from the already-public
# fires_log (oi_combined is committed per fire), so nothing here leaks a tuned
# edge. Mirrors tools/paper_trade_tracker.py (the box-side/local version).
PT_FREEZE_TS = 1783813238            # 2026-07-11T23:40:38Z — the out-of-sample boundary
PT_FREEZE_TXT = "2026-07-11"
PT_OI_CUTOFF = 6_928_091             # 60th-pct open interest of v4 fires at freeze
PT_COSTS = 0.0015                    # 0.05% fee + 0.10% slippage per round trip
PT_TP_MAIN = 0.03


def _papertrade_panel() -> str:
    """A section on the AI Journal: the v4 + high-OI + tight-TP forward experiment,
    scored only on fires that fired AFTER the frozen out-of-sample boundary."""
    fires = [e for e in _episodes(_load_fires())
             if e.get("strat") == "v4"
             and (e.get("oi_combined") or 0) >= PT_OI_CUTOFF
             and (e.get("t") or e.get("logged_utc") or 0) >= PT_FREEZE_TS]
    closed = [e for e in fires if e.get("outcome")]
    pending = [e for e in fires if not e.get("outcome")]

    def _mfe(e):
        return (e.get("outcome") or {}).get("mfe") or 0.0

    def _stat(label, val, cls=""):
        return (f'<div class="jstat {cls}"><span class="jstat-v">{val}</span>'
                f'<span class="jstat-l">{label}</span></div>')

    def _score(tp):
        nets = [(tp - PT_COSTS) if _mfe(e) >= tp
                else ((e.get("outcome") or {}).get("pnl_real") or 0.0)
                for e in closed]
        w = sum(1 for e in closed if _mfe(e) >= tp)
        return {"w": w, "wr": w / len(closed) * 100, "net": sum(nets) / len(nets)}

    intro = (
        '<p class="jnote">A <b>forward, out-of-sample experiment</b> frozen on '
        f'<b>{PT_FREEZE_TXT}</b>: go <b>long a Buy&nbsp;v4</b> signal only when the coin '
        'has <b>high open interest</b>, then take profit fast at <b>+2–3%</b> (own ~16% '
        'stop, 72h max). A walk-forward test hinted this could win ~90% of the time — so '
        'this panel scores <b>only fires that triggered after the freeze</b>, an honest '
        'unseen test. <b>Paper only — not executed trades, not financial advice.</b></p>')

    if not closed:
        cards = (
            '<div class="jcards">'
            + _stat("forward fires", len(fires))
            + _stat("awaiting 72h grade", len(pending))
            + _stat("win rate", "—")
            + _stat("net / trade", "—")
            + '</div>'
            + '<p class="jnote">Day&nbsp;0 — no qualifying v4 fire has matured since the '
              'freeze yet. This fills on its own as high-OI v4 setups fire and grade '
              '(expect only a few per week).</p>')
        table = ""
    else:
        s2, s3 = _score(0.02), _score(0.03)
        worst = min((e.get("outcome") or {}).get("pnl_real") or 0.0 for e in closed)
        cards = (
            '<div class="jcards">'
            + _stat("forward fires (graded)", len(closed))
            + _stat("win rate @ +2%", f'{s2["wr"]:.0f}%',
                    "good" if s2["net"] > 0 else "bad")
            + _stat("win rate @ +3%", f'{s3["wr"]:.0f}%',
                    "good" if s3["net"] > 0 else "bad")
            + _stat("net / trade @ +3%", f'{s3["net"]*100:+.2f}%',
                    "good" if s3["net"] > 0 else "bad")
            + _stat("worst single trade", f'{worst*100:+.1f}%',
                    "bad" if worst < 0 else "")
            + '</div>')
        rows = []
        for e in sorted(closed, key=lambda e: e.get("t") or 0, reverse=True):
            o = e.get("outcome") or {}
            mfe = _mfe(e)
            ep = e.get("entry_price") or o.get("entry")
            c2 = "jhit" if mfe >= 0.02 else "jmiss"
            c3 = "jhit" if mfe >= 0.03 else "jmiss"
            rows.append(
                f'<tr><td>{_dts(e.get("t") or e.get("logged_utc") or 0)}</td>'
                f'<td class="jt">{html.escape((e.get("sym") or "").upper())}</td>'
                f'<td class="jnum">{fmt_subscript_price(ep) if ep else "—"}</td>'
                f'<td class="jnum {c2}">{"✓" if mfe >= 0.02 else "✗"}</td>'
                f'<td class="jnum {c3}">{"✓" if mfe >= 0.03 else "✗"}</td>'
                f'<td class="jnum">{mfe*100:+.1f}%</td>'
                f'<td class="jnum">{_jpct(o.get("pnl_real"))}</td></tr>')
        table = (
            '<div class="tablewrap jscroll"><table class="jtable"><thead><tr>'
            '<th>When</th><th>Token</th>'
            '<th title="entry reference price (Binance mark at the signal)">Bought at</th>'
            '<th title="did price reach +2%?">+2%?</th>'
            '<th title="did price reach +3%?">+3%?</th>'
            '<th title="best move within 72h">Best move</th>'
            '<th title="realistic P&amp;L, net of fees + slippage">Result</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')

    return (
        '<section class="card span jscore" id="papertrade">'
        '<h3>V4 High-OI Paper Trade '
        '<span class="asof">forward · out-of-sample · tight +2–3% take-profit</span></h3>'
        + intro + cards + table
        + '<p class="jnote"><b>Honest caveat:</b> a high win rate here is bought with a '
          'tiny profit target — the ~16% stop makes the few losers big, and they tend to '
          'cluster together in a market pullback. <b>Not proven until it survives a '
          'downturn.</b></p>'
        '</section>')


def _trades_page(recs) -> str:
    """The merged AI TRADES tab: one broker-style ledger of the model's live
    v1/v2/v4 setups (plus any hidden setup that has earned its way back — see
    _visible_setups). Each fire is logged with an entry the moment it triggers,
    then graded 72h later on real price. Shows a per-setup scorecard, OPEN
    positions, and CLOSED history with the model's confidence + realistic $ P&L.
    Research paper signals scored on real prices — not executed trades, not advice."""
    built = {r["symbol"].upper() for r in recs}
    # Honest counting (2026-07-09 audit F-19): episode-dedup BEFORE the strat
    # filter — the same rules the box graders use (one fire per coin per 72h; a
    # market-wide burst counts once). Without this the ledger + PnL card booked
    # pre-refractory hourly re-fires as separate $100 trades.
    eps = _episodes(_load_fires())
    vis = _visible_setups(eps)                 # v1/v2/v4 + any earned-back setup
    fires = [e for e in eps if e.get("strat") in vis]
    closed = [e for e in fires if e.get("outcome")]
    open_ = [e for e in fires if not e.get("outcome")]
    # `t` is the actual signal/fire time (the entry); logged_utc is just when the
    # box first recorded it (often a batch). The 72h grade runs from the fire time.
    _key = lambda e: e.get("t") or e.get("logged_utc") or 0
    closed.sort(key=_key, reverse=True)
    open_.sort(key=_key, reverse=True)

    def _o(e, k):
        return (e.get("outcome") or {}).get(k)

    _pnl = _real_pnl                       # realistic net-of-cost P&L per trade

    n = len(closed)
    pnls = [_pnl(e) for e in closed if _pnl(e) is not None]
    mfes = [_o(e, "mfe") for e in closed if _o(e, "mfe") is not None]
    wins = sum(1 for e in closed if (_pnl(e) or 0) > 0)
    hits = sum(1 for e in closed if _o(e, "pump"))
    win_rate = wins / n * 100 if n else None
    hit_rate = hits / n * 100 if n else None
    avg_pnl = sum(pnls) / len(pnls) if pnls else None
    avg_mfe = sum(mfes) / len(mfes) if mfes else None
    # #3 benchmarks: same-window buy-&-hold the same coins, and BTC.
    bh = [_o(e, "bh_coin") for e in closed if _o(e, "bh_coin") is not None]
    bt = [_o(e, "bh_btc") for e in closed if _o(e, "bh_btc") is not None]
    avg_bh = sum(bh) / len(bh) if bh else None
    avg_bt = sum(bt) / len(bt) if bt else None

    def _stat(label, val, cls=""):
        return (f'<div class="jstat {cls}"><span class="jstat-v">{val}</span>'
                f'<span class="jstat-l">{label}</span></div>')

    def _pct0(v):
        return f"{v:.0f}%" if v is not None else "—"

    def _signed(v):
        return (f"{v*100:+.1f}%") if v is not None else "—"

    pnl_cls = "good" if (avg_pnl or 0) > 0 else ("bad" if avg_pnl is not None else "")
    scorecard = (
        '<div class="jcards">'
        + _stat("closed paper trades", n)
        + _stat("hit +50% / 72h", f"{hits}/{n}" if n else "—",
                "good" if hits else "")
        + _stat("win rate (P&amp;L &gt; 0)", _pct0(win_rate))
        + _stat("avg result / trade (net)", _signed(avg_pnl), pnl_cls)
        + _stat("avg best move", _signed(avg_mfe))
        + '</div>')

    # Per-setup mini-breakdown.
    by = []
    for k in vis:
        cs = [e for e in closed if e.get("strat") == k]
        if not cs:
            continue
        h = sum(1 for e in cs if (e["outcome"] or {}).get("pump"))
        w = sum(1 for e in cs if (_pnl(e) or 0) > 0)
        ap = [(_pnl(e)) for e in cs if _pnl(e) is not None]
        avg = sum(ap) / len(ap) if ap else None
        by.append(f'<li><b>{STRAT_NAME[k]}</b> <span>{STRAT_TITLE[k]}</span> — '
                  f'{len(cs)} closed · {h} hit +50% · {w} green · avg {_signed(avg)}</li>')
    by_html = f'<ul class="jby">{"".join(by)}</ul>' if by else ""

    # #3 edge check: strategy vs simply holding the same coins (and BTC) over the
    # exact same windows. The strategy beating a flat hold is the proof of edge —
    # an absolute +P&L could just be a rising market.
    def _bcell(label, v, lead=False):
        cls = "pos" if (v or 0) > 0 else ("neg" if v is not None else "")
        return (f'<div class="jb-item{" lead" if lead else ""}">'
                f'<span class="jb-v {cls}">{_signed(v)}</span>'
                f'<span class="jb-l">{label}</span></div>')

    edge = (avg_pnl - avg_bh) if (avg_pnl is not None and avg_bh is not None) else None
    edge_txt = (f'<div class="jb-edge {"pos" if (edge or 0) >= 0 else "neg"}">'
                f'Edge vs just holding: <b>{("+" if (edge or 0) >= 0 else "−")}'
                f'{abs(edge)*100:.1f} pts</b></div>') if edge is not None else ""
    bench = (
        '<section class="card span jbench">'
        '<h3>Edge check <span class="asof">avg result per closed trade · same window, '
        'same cost</span></h3>'
        '<div class="jb-row">'
        + _bcell("AI strategy (net of fees)", avg_pnl, lead=True)
        + _bcell("Buy &amp; hold same coins", avg_bh)
        + '</div>' + edge_txt
        + '<p class="jnote">The strategy beating a flat hold of the same coins is the '
        'proof of edge — its timed exits bank pumps that holders give back, and its '
        'stops cut the losers. Net of 0.05% fee + 0.10% slippage per fill.</p>'
        '</section>') if (avg_pnl is not None) else ""

    def _tok(sym):
        s = (sym or "").upper()
        return (f'<a href="{s.lower()}.html">{html.escape(s)}</a>'
                if s in built else html.escape(s))

    def _setup_chip(k):
        return (f'<span class="jset {k}" title="{html.escape(STRAT_TITLE.get(k, ""))}">'
                f'{STRAT_NAME.get(k, k)}</span>')

    def _px(v):
        return fmt_subscript_price(v) if v else "—"

    def _moved(e):
        """"How much did it actually pump" — the best move the trade reached (max
        favourable excursion): up for a long, DOWN for a short. The grader stops
        measuring at +200%, so report anything there as a floor, not a number."""
        o = e.get("outcome") or {}
        m = o.get("mfe")
        if m is None:
            return '<span class="jp">—</span>'
        cap, short = m >= 2.0, _is_short(e)
        ttl = (f"fell {m*100:.0f}% below entry — a short wants this"
               if short else f"peaked {m*100:.0f}% above entry")
        if cap:
            ttl = "reached +200%, where the grader stops measuring — the real peak was higher"
        return (f'<span class="jp {"pos" if m > 0 else ""}" title="{html.escape(ttl)}">'
                f'{"≥" if cap else ""}{"−" if short else "+"}{m*100:.0f}%</span>')

    def _result(vf):
        """What the hypothetical stake came back as: $100 in → $120 out on a win."""
        if vf is None:
            return '<span class="jp">—</span>'
        val = PNL_STAKE * (1 + vf)
        cls = "pos" if vf > 0 else ("neg" if vf < 0 else "")
        word = "win" if vf > 0 else ("loss" if vf < 0 else "flat")
        return (f'<span class="jp {cls}" title="{word} — ${PNL_STAKE:,.0f} in, '
                f'${val:,.2f} out, net of fees + slippage">${val:,.0f}</span>'
                f'<span class="jsub">{_jpct(vf)}</span>')

    # Closed-trades table (newest first). 11 columns — keep the #jclosed nth-child
    # width block in EXTRA_CSS in sync (table-layout:fixed gives an unlisted column
    # ZERO width, which is what broke this table before).
    if closed:
        crows = []
        for e in closed:
            o = e["outcome"] or {}
            note = _EXIT_NOTE.get(o.get("exit_reason"), "")
            sold = (f'<td class="jnum" title="{html.escape(note)}">{_px(_exit_px(e))}</td>'
                    if note else f'<td class="jnum">{_px(_exit_px(e))}</td>')
            crows.append(
                f'<tr><td>{_dts(_key(e))}</td>'
                f'<td class="jt">{_tok(e.get("sym"))}</td>'
                f'<td>{_setup_chip(e.get("strat"))}</td>'
                f'<td class="jnum">{_pump_pct(e.get("pump_score"))}</td>'
                f'<td class="jnum">{_px(_entry_px(e))}</td>'
                f'{sold}'
                f'<td class="jnum">{_px(_target_px(e))}</td>'
                f'<td>{_outcome_story(o, short=_is_short(e))}</td>'
                f'<td class="jnum">{_moved(e)}</td>'
                f'<td class="jnum">${PNL_STAKE}</td>'
                f'<td class="jnum jpnl">{_result(_real_pnl(e))}</td></tr>')
        closed_tbl = (
            '<div class="tablewrap jscroll"><table class="jtable" id="jclosed"><thead><tr>'
            '<th>When</th><th>Token</th>'
            '<th title="which setup fired this trade">Model</th>'
            '<th title="the model&#39;s own odds of a +50% move, as of that hour">Expected pump</th>'
            '<th title="the price the trade actually filled at">Bought at</th>'
            '<th title="the price it actually closed at">Sold at</th>'
            '<th title="the take-profit the plan was aiming for">Expected sell</th>'
            '<th>What happened</th>'
            '<th title="the best move it actually reached before closing">Actually pumped</th>'
            '<th title="hypothetical stake per paper trade">Cost</th>'
            '<th title="what the stake came back as, net of fees + slippage">Result</th>'
            '</tr></thead><tbody>'
            + "".join(crows) + '</tbody></table></div>'
            + '<p class="jnote">Each row: <b>when</b> the AI bought, <b>which model</b> fired and '
            'the odds it gave that coin at the time, the price it actually <b>bought</b> and '
            '<b>sold</b> at versus the sell it was aiming for, then <b>what happened</b>, how far '
            f'the coin really moved, and what a hypothetical ${PNL_STAKE} came back as (net of '
            'fees + slippage). Odds read “—” on trades logged before the model began scoring them; '
            'a blended “sold at” (hover it) means half was sold at +25% and the rest later.</p>')
    else:
        closed_tbl = ('<p class="jnote">No trades have matured yet — each is '
                      'graded 72h after it fires. Check back as the ledger fills.</p>')

    # Open positions (fired, not yet matured) — what the model is "in" right now,
    # with the full plan: entry, stop, take-profit target and the hypothetical stake.
    # Open positions mirror the closed columns, minus the four that only exist once
    # a trade is graded (sold at / what happened / actually pumped / result).
    # 8 columns — keep the #jopen nth-child width block in EXTRA_CSS in sync.
    if open_:
        orows = []
        for e in open_:
            short = _is_short(e)
            tgt = _target_px(e)
            tcell = (f'{_px(tgt)} <small>{"−20%" if short else "+50%"}</small>'
                     if tgt else f'<small>{"−20%" if short else "+50%"}</small>')
            orows.append(
                f'<tr><td>{_dts(_key(e))}</td>'
                f'<td class="jt">{_tok(e.get("sym"))}</td>'
                f'<td>{_setup_chip(e.get("strat"))}</td>'
                f'<td class="jnum">{_pump_pct(e.get("pump_score"))}</td>'
                f'<td class="jnum">{_px(_entry_px(e))}</td>'
                f'<td class="jnum">{tcell}</td>'
                f'<td class="jnum">${PNL_STAKE}</td>'
                f'<td>{_dts(_key(e) + 72 * 3600)} <span class="jpending">⏳</span></td></tr>')
        open_tbl = (
            '<h3>Open positions <span class="asof">live · what the model is in now, '
            'waiting for the 72h grade</span></h3>'
            '<div class="tablewrap jscroll"><table class="jtable" id="jopen"><thead><tr>'
            '<th>When</th><th>Token</th>'
            '<th title="which setup fired this trade">Model</th>'
            '<th title="the model&#39;s own odds of a +50% move, as of that hour">Expected pump</th>'
            '<th title="entry reference price (the mark when it fired)">Bought at</th>'
            '<th title="the take-profit it is aiming for (first tier sells half at +25%)">Expected sell</th>'
            '<th title="hypothetical stake per paper trade">Cost</th>'
            '<th>Grades on</th>'
            '</tr></thead><tbody>' + "".join(orows) + '</tbody></table></div>')
    else:
        open_tbl = ""

    # ── +10% MOVER track (same fires, easier/faster target: +10% within 24h) ──
    def _mpx(v):
        return fmt_subscript_price(v) if v else "—"
    m_closed = [e for e in closed if _o(e, "m_pnl_real") is not None]
    if m_closed:
        mn = len(m_closed)
        mh24 = sum(1 for e in m_closed if _o(e, "m_hit_24h"))
        mh12 = sum(1 for e in m_closed if _o(e, "m_hit_12h"))
        mw = sum(1 for e in m_closed if (_o(e, "m_pnl_real") or 0) > 0)
        mps = [_o(e, "m_pnl_real") for e in m_closed]
        m_avg = sum(mps) / len(mps) if mps else None
        m_score = (
            '<div class="jcards">'
            + _stat("closed mover trades", mn)
            + _stat("hit +10% / 24h", f"{mh24}/{mn}", "good" if mh24 else "")
            + _stat("hit +10% / 12h", f"{mh12}/{mn}")
            + _stat("win rate (P&amp;L &gt; 0)", _pct0(mw / mn * 100))
            + _stat("avg result / trade (net)", _signed(m_avg),
                    "good" if (m_avg or 0) > 0 else "bad")
            + '</div>')
        _mx = {"target": ("+10% hit ✓", "jhit"), "stopped": ("Stopped ✗", "jstop"),
               "timeout": ("24h close", "jmiss")}
        mrows = []
        for e in m_closed[:300]:
            ep = e.get("entry_price") or _o(e, "entry")
            h12, h24 = _o(e, "m_hit_12h"), _o(e, "m_hit_24h")
            k = e.get("strat")
            why = (f'{_setup_chip(k)} '
                   f'<span class="jwhy">{html.escape(STRAT_WHY.get(k, ""))}</span>')
            c12 = f'<td class="jnum {"jhit" if h12 else "jmiss"}">{"✓" if h12 else "✗"}</td>'
            c24 = f'<td class="jnum {"jhit" if h24 else "jmiss"}">{"✓" if h24 else "✗"}</td>'
            if h24:
                story = '<span class="jhit">🟢 Hit +10%</span>'
            elif _o(e, "m_exit") == "stopped":
                story = '<span class="jstop">🔴 Stopped out</span>'
            else:
                story = '<span class="jmiss">⚪ No move</span>'
            mrows.append(
                f'<tr><td>{_dts(_key(e))}</td>'
                f'<td class="jt">{_tok(e.get("sym"))}</td>'
                f'<td class="jwhycell">{why}</td>'
                f'<td class="jnum">{_mpx(ep)}</td>{c12}{c24}'
                f'<td>{story}</td>'
                f'<td class="jnum">{_jpct(_o(e, "m_pnl_real"))}</td></tr>')
        mover_table = (
            '<section class="card span" id="movers">'
            '<h3>Mover trades <span class="asof">graded on real price 24h after entry</span></h3>'
            '<div class="tablewrap jscroll"><table class="jtable" id="jmover"><thead><tr>'
            '<th>When</th><th>Token</th><th>Why it bought</th><th>Bought at</th>'
            '<th title="reached +10% within 12h">+10% ≤12h</th>'
            '<th title="reached +10% within 24h">+10% ≤24h</th>'
            '<th>What happened</th>'
            '<th title="realistic P&amp;L: +10% TP / own stop / 24h close, net of fees+slippage">Result</th>'
            '</tr></thead><tbody>' + "".join(mrows) + '</tbody></table></div></section>')
    else:
        m_score = ('<p class="jnote">No mover trades graded yet — each fire is '
                   'scored once it matures. Check back as the ledger fills.</p>')
        mover_table = ""
    mover_scorecard = (
        '<section class="card span jscore" id="movers">'
        '<h3>+10% Movers <span class="asof">same setups · easier target: +10% within 24h</span></h3>'
        '<p class="jnote">A faster, higher-frequency <b>early-mover</b> track: the SAME '
        'v1/v2/v4 fires, scored against a <b>+10% target within 24h</b> (the first leg of '
        'a move) instead of +50%/72h. More trades, smaller wins — read it as an '
        'early-warning feed, not the main pump bet.</p>'
        f'{m_score}</section>')

    # Per-setup equity small-multiples (absorbs the old Models tab's per-model view).
    _eq_curves = [(sid, STRAT_NAME.get(sid, sid), _MODEL_COLORS.get(sid, "#888"),
                   _setup_curve(sid, eps)) for sid in vis]

    body = f"""
<header><h1>AI Trades</h1>
{site_nav("scams")}
{scams_subnav("trades")}
<p class="sub">One ledger for every trade the AI's live setups make — the token it bought, which
model fired, the odds it gave at the time, the price it bought and sold at, and what a hypothetical
${PNL_STAKE} came back as. Each trade is logged the moment it triggers, then <b>scored on the real
price</b> 72h later. <b>Every model keeps its own profit and loss</b> below — a combined total would
let one setup's wins hide another's losses. Two targets: the headline <b>+50% pump</b> (72h) and a
faster <b>+10% mover</b> (24h). <b>Research paper signals scored on real prices — not executed
trades, not financial advice</b>; the samples are small, and most are still too small to tell skill
from luck (each card says so).</p></header>
<main>
<div class="jtabs" role="tablist">
  <button class="jtab active" data-view="pumps" type="button">+50% Pumps</button>
  <button class="jtab" data-view="movers" type="button">+10% Movers</button>
</div>
<div id="view-pumps" class="jview">
<div class="jtop">
<section class="card span jscore">
  <h3>Scorecard <span class="asof">live setups · +50% / 72h · closed (graded)</span></h3>
  {scorecard}
  {by_html}
</section>
</div>
{_equity_grid(_eq_curves)}
{f'<section class="card span">{open_tbl}</section>' if open_tbl else ''}
{_papertrade_panel()}
<div class="jrow">{bench}{_winrate_chart(closed)}</div>
<section class="card span">
  <h3>Closed trades <span class="asof">graded on real price 72h after entry</span></h3>
  {closed_tbl}
</section>
</div>
<div id="view-movers" class="jview" style="display:none">
<div class="jrow">{mover_scorecard}{_winrate_chart(m_closed, lambda e: _o(e, "m_pnl_real"), "+10% / 24h")}</div>
{mover_table}
</div>
</main>
{THEME_JS}{_JOURNAL_TABS_JS}{_WINRATE_TIP_JS}"""
    desc = ("The AI's live trade ledger on the Manipulated tab — every Buy v1/v2/v4 setup "
            "it bought, graded 72h later on real price: which token, the model's confidence, "
            "stop, take-profit, stake and the realistic $ result. Research paper signals, "
            "not executed trades.")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{page_meta("AI Trades — Manipulated — ListingLabs", desc)}'
            f'<title>AI Trades — Manipulated</title>'
            f'<link rel="stylesheet" href="style.css?v={_CSS_VER}"></head><body>{body}</body></html>')


# v1/v2/v4 match _winrate_chart's COL (journal.html) so a setup looks the same
# color on both pages; the rest are pulled from _DONUT_COLORS for consistency.
_MODEL_COLORS = {
    "v1": "#2e8b57", "v2": "#3b7cc4", "v4": "#c47a3a",
    "probe": "#9c6ade", "buy15": "#d98c5f",
    "dump": "#c0392b", "bear_trap": "#6aa84f", "spike_retrace": "#7f8c9a",
}


def _equity_grid(curves) -> str:
    """Per-model equity curves: one small multiple per setup, X = trade order
    (not calendar time — sparse irregular fire gaps would make a time axis messy
    in a small box), Y = simple running SUM of realized P&L (not compounded —
    matches the plain-mean 'exp/trade' shown elsewhere on this page)."""
    curves = [c for c in curves if c[3]]
    if not curves:
        return ""
    minis = []
    for sid, name, color, pts in curves:
        W, H, L, R, T, B = 240, 120, 8, 8, 10, 18
        pw, ph = W - L - R, H - T - B
        cum, running = [], 0.0
        for _t, pnl in pts:
            running += pnl
            cum.append(running)
        lo, hi = min(cum + [0.0]), max(cum + [0.0])
        span = (hi - lo) or 1.0
        pad = span * 0.1
        lo, hi = lo - pad, hi + pad
        span = hi - lo

        def _xy(i, v):
            x = L + (i / (len(cum) - 1) * pw if len(cum) > 1 else pw / 2)
            y = T + (1 - (v - lo) / span) * ph
            return x, y

        zy = T + (1 - (0.0 - lo) / span) * ph
        zero_line = f'<line x1="{L}" y1="{zy:.1f}" x2="{W-R}" y2="{zy:.1f}" class="eq-zero"/>'

        if len(cum) == 1:
            x, y = _xy(0, cum[0])
            path = (f'<circle class="eq-pt" r="2.8" cx="{x:.1f}" cy="{y:.1f}" fill="{color}">'
                    f'<title>{html.escape(name)} #1 — pnl {pts[0][1]*100:+.1f}%, '
                    f'cum {cum[0]*100:+.1f}%</title></circle>')
        else:
            coords = [_xy(i, v) for i, v in enumerate(cum)]
            poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            pts_svg = "".join(
                f'<circle class="eq-pt" r="2.2" cx="{x:.1f}" cy="{y:.1f}" stroke="{color}"><title>'
                f'{html.escape(name)} #{i+1} — pnl {pts[i][1]*100:+.1f}%, '
                f'cum {cum[i]*100:+.1f}%</title></circle>'
                for i, (x, y) in enumerate(coords))
            path = (f'<polyline class="eq-line" points="{poly}" stroke="{color}" fill="none"/>'
                    + pts_svg)

        final = cum[-1]
        svg = (f'<svg class="eq-svg" viewBox="0 0 {W} {H}" role="img" '
               f'aria-label="{html.escape(name)} equity curve">{zero_line}{path}</svg>')
        # Each model carries its OWN P&L — a combined figure hides that one setup
        # can be paying for another's losses.
        rets = [p for _t, p in pts]
        dollars = PNL_STAKE * final
        d_cls = "pos" if dollars >= 0 else "neg"
        wins = sum(1 for r in rets if r > 0)
        vcls, vtxt = _edge_verdict(rets)
        minis.append(
            f'<figure class="eq-mini"><figcaption><i style="background:{color}"></i>'
            f'{html.escape(name)}</figcaption>'
            f'<div class="eq-pnl {d_cls}">{"+" if dollars >= 0 else "−"}${abs(dollars):,.2f}</div>'
            f'<div class="eq-sub">{len(pts)} trades · {100*wins/len(rets):.0f}% won · '
            f'{sum(rets)/len(rets)*100:+.2f}%/trade</div>{svg}'
            f'<div class="eq-verdict {vcls}" title="{html.escape(vtxt)}">{html.escape(vtxt)}</div>'
            f'</figure>')

    return (f'<section class="card span"><h3>Profit / loss by model <span class="asof">each model '
            f'stands on its own · hypothetical ${PNL_STAKE}/trade</span></h3>'
            f'<div class="eq-grid">{"".join(minis)}</div>'
            f'<p class="jnote">Every model keeps its own books — a combined total would let one '
            f'setup\'s wins paper over another\'s losses. The line is the running SUM of that '
            f'model\'s realized P&amp;L, one point per graded trade in fire order (X is trade #, '
            f'not date; Y is a plain running total, not a compounded account). Hover a point for '
            f'that trade. <b>Read the grey line under each chart first</b> — at these sample sizes '
            f'a ±$20 swing is usually luck, not skill.</p></section>')


def _index(recs) -> str:
    tiles = "\n".join(_tile(r) for r in recs)
    head = "".join(f'<th data-i="{i}">{html.escape(c)}</th>' for i, c in enumerate(LIST_COLS))
    rows = "\n".join(_list_row(r) for r in recs)
    c = SCREENER_META.get("counts") or {}
    as_of = SCREENER_META.get("as_of_hour")
    asof_txt = _dt_hour(as_of) if as_of else "—"
    body = f"""
<header><h1>Manipulated</h1>
{site_nav("scams")}
{scams_subnav("screener")}
<p>{len(recs)} coins · Buy v1 <b id="cnt-v1">{c.get('v1', 0)}</b> · v2 <b id="cnt-v2">{c.get('v2', 0)}</b> · v4 <b id="cnt-v4">{c.get('v4', 0)}</b> · <b>{c.get('passing_gate', 0)}</b> pass (BN+BYB OI)/FDV ≥ 8% · signals as of {asof_txt} UTC <button id="setup-help" type="button" class="setup-help" title="What do Buy v1/v2/v4 mean?">ℹ How the setups work</button> <span id="live-badge" class="live-wait" title="Price · 24h · OI · funding + Buy v1/v2/v4 update live from the box (~60s; signals at each hour close)">● connecting…</span></p>
<p class="sub">Manipulated-coin perp screener — combined Binance+Bybit OI, funding &amp; Buy v1/v2/v4 setups; click a coin for its detail + signals. Not financial advice.</p></header>
{SETUP_PANEL}
{_filter_bar()}
<div id="views" class="view-grid">
  {_spark_defs()}
  <main class="grid">{tiles}</main>
  <div class="listwrap"><table class="list" id="ltab"><thead><tr>{head}</tr></thead>
  <tbody>{rows}</tbody></table></div>
</div>
{JS}
{LIVE_JS}
{THEME_JS}"""
    desc = ("Manipulated-token watchlist — tokens propped to extreme FDV: price history, "
            "per-venue perp open interest, funding, OI/volume trend and holder concentration.")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'{page_meta("Manipulated — ListingLabs", desc, connect_extra=LIVE_ORIGIN)}'
            f'<title>Manipulated</title><link rel="stylesheet" href="style.css?v={_CSS_VER}"></head>'
            f'<body>{body}</body></html>')


_ODDS_SORT_JS = """<script>(function(){
  var tab=document.getElementById('otab');if(!tab)return;
  var tb=tab.tBodies[0],ths=tab.tHead.rows[0].cells;
  function val(c){var d=c.dataset.s;if(d===undefined)return c.textContent.toLowerCase();
    var f=parseFloat(d);return isNaN(f)?d:f;}
  function sort(i,dir){var rs=[].slice.call(tb.rows);
    rs.sort(function(a,b){var x=val(a.cells[i]),y=val(b.cells[i]);return x<y?-dir:x>y?dir:0;});
    rs.forEach(function(r){tb.appendChild(r);});   // CSS counter renumbers .rank by DOM order
    for(var k=0;k<ths.length;k++)ths[k].classList.remove('sorted','asc','desc');
    ths[i].classList.add('sorted',dir>0?'asc':'desc');}
  for(var i=0;i<ths.length;i++)(function(i){ths[i].addEventListener('click',function(){
    sort(i,ths[i].classList.contains('asc')?-1:1);});})(i);
  sort(2,-1);   // default: +50% odds, highest first
})();</script>"""


EXTRA_CSS = """
.fdv{font-size:13px;color:var(--text-2);display:inline-flex;align-items:center;gap:6px}
.links.note{color:var(--text-4);font-style:italic}
/* ── Models report (models.html) ── */
.goal .rhythm{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:10px;font-size:12.5px;color:var(--text-2)}
.mtable{width:100%;min-width:880px;border-collapse:collapse;font-size:12.5px}
.mtable th{text-align:left;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-4);padding:6px 10px;border-bottom:1px solid var(--border)}
.mtable td{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:top;color:var(--text-2)}
.mtable tr:last-child td{border-bottom:none}
.mname{font-weight:650;color:var(--text);min-width:210px}
.mreads{font-weight:400;font-size:11.5px;color:var(--text-4);margin-top:3px;line-height:1.45}
.mcred{font-size:11.5px;color:var(--text-3)}
.sched .swhen{font-weight:650;color:var(--text);white-space:nowrap}
.sched .slook{font-size:11.5px;color:var(--text-3)}
.sched tr.due .swhen{color:var(--pos)}
.sched tr.due td{background:color-mix(in srgb,var(--pos) 5%,transparent)}
.mline{list-style:none;margin:6px 0 0;padding:0}
.mline li{display:flex;gap:14px;padding:9px 0;border-bottom:1px dashed var(--border);font-size:12.5px;color:var(--text-2);line-height:1.55}
.mline li:last-child{border-bottom:none}
.mline .tld{flex:0 0 52px;font-weight:700;color:var(--text-3);font-size:11.5px;padding-top:1px}
.mtable .pos{color:var(--pos)}.mtable .neg{color:var(--neg)}
.msc-svg{width:100%;height:auto;max-height:300px;margin-top:4px}
.msc-grid{stroke:var(--border);stroke-width:1}
.msc-grid.msc-base{stroke:var(--text-4)}
.msc-axis{font-size:10px;fill:var(--text-4)}
.msc-dot{stroke:var(--bg-subtle);stroke-width:1.5;cursor:pointer}
.msc-dot:hover{stroke:var(--text);r:8}
.msc-lbl{font-size:10.5px;fill:var(--text-2);font-weight:650}
.msc-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:8px;font-size:11.5px;color:var(--text-2)}
.msc-leg{display:inline-flex;align-items:center;gap:5px}
.msc-leg i{width:9px;height:9px;border-radius:50%;display:inline-block}
.eq-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-top:6px}
.eq-mini{margin:0;background:var(--bg-subtle);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.eq-mini figcaption{display:flex;align-items:center;gap:6px;font-size:12px;font-weight:650;color:var(--text);margin-bottom:4px}
.eq-mini figcaption i{width:9px;height:9px;border-radius:50%;display:inline-block;flex:0 0 auto}
.eq-sub{font-weight:500;font-size:11px;color:var(--text-3);margin-bottom:6px;
  font-variant-numeric:tabular-nums}
.eq-sub .pos{color:var(--pos)}.eq-sub .neg{color:var(--neg)}
.eq-svg{width:100%;height:auto;display:block}
.eq-zero{stroke:var(--border);stroke-width:1;stroke-dasharray:3 3}
.eq-line{stroke-width:1.8}
.eq-pt{fill:var(--bg-subtle);stroke-width:1.4;cursor:pointer}
.eq-pt:hover{r:4}
/* ── AI Track Record (journal.html) ── */
.jcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:6px 0 4px}
.jstat{background:var(--bg-2);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px;display:flex;flex-direction:column;gap:4px}
.jstat-v{font-size:26px;font-weight:700;line-height:1.1;font-family:var(--font-mono);font-variant-numeric:tabular-nums;letter-spacing:-.03em}
.jstat-l{font-size:12px;color:var(--text-4)}
.jstat.good .jstat-v{color:#1a8a4f}
.jstat.bad .jstat-v{color:#c0392b}
/* Buy Signals journal: result cell colors + the "watching" (ungraded) note */
.jtable td.good{color:var(--pos);font-weight:600}
.jtable td.bad{color:var(--neg);font-weight:600}
.jtable td.jwatch{color:var(--text-3)}
.jby{list-style:none;padding:0;margin:14px 0 0;display:flex;flex-direction:column;gap:6px}
.jby li{font-size:13px;color:var(--text-2)}
.jby li b{color:var(--text-1)}
.jby li span{color:var(--text-4)}
.jtable{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
.jtable th{text-align:left;color:var(--text-4);font-weight:600;font-size:12px;
  padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
.jtable td{padding:9px 10px;border-bottom:1px solid var(--border-2,var(--border))}
.jtable tr:hover td{background:var(--bg-2)}
.jtable .jnum{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;font-family:var(--font-mono);letter-spacing:-.02em}
.jtable .jt a{font-weight:600;color:var(--link,#2e6da4);text-decoration:none}
.jtable .jt a:hover{text-decoration:underline}
.jp.pos,.jnum.pos{color:#1a8a4f}
.jp.neg{color:#c0392b}
.jset{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;font-weight:600;
  border:1px solid var(--border)}
.jset.v1{background:rgba(46,109,164,.14);color:#2e6da4}
.jset.v2{background:rgba(31,78,121,.18);color:#3b7cc4}
.jset.v4{background:rgba(156,106,222,.16);color:#7d4ec9}
/* dump is a SHORT — red, so it reads as the opposite bet at a glance */
.jset.dump{background:rgba(192,57,43,.16);color:#c0392b}
.jpending{color:var(--text-4);font-size:12px}
.jtable .jpnl{line-height:1.2}
.jsub{display:block;font-size:11px;opacity:.68;font-weight:400}
.jnote{color:var(--text-4);font-style:italic;margin:8px 0}
.jtable .jx{font-size:12px;white-space:nowrap;color:var(--text-3,var(--text-2))}
.jtable .jhit{color:#1a8a4f}
.jtable .jstop{color:#c0392b}
.jtable .jmiss{color:var(--text-4)}
/* segmented toggle between the +50% Pumps and +10% Movers views */
.jtabs{display:flex;gap:4px;margin:0 0 18px;background:var(--bg-subtle);border:1px solid var(--border);
  border-radius:11px;padding:4px;max-width:330px}
.jtab{flex:1;font:inherit;font-size:13px;font-weight:700;color:var(--text-3);background:none;border:none;
  border-radius:8px;padding:9px 10px;cursor:pointer;white-space:nowrap}
.jtab:hover:not(.active){color:var(--text-1)}
.jtab.active{background:var(--primary);color:#fff}
/* win-rate-over-time chart (one polyline per setup) */
.wc-card{max-width:460px}
.wc-svg{width:100%;height:auto;margin-top:8px;background:var(--bg-subtle);border-radius:10px;display:block}
.wc-grid{stroke:var(--border);stroke-width:1;stroke-dasharray:2 4;opacity:.7}
.wc-base{stroke-dasharray:none;opacity:1}
.wc-yl,.wc-xl{fill:var(--text-4);font-size:9px}
.wc-val{fill:var(--text-2);font-size:10px;font-weight:700}
.wc-n{fill:var(--text-4);font-size:8.5px}
/* hoverable bars: dim the rest, light up the hovered one */
.wc-bar{opacity:.9;cursor:pointer;transition:opacity .12s}
.wc-svg:hover .wc-bar{opacity:.4}
.wc-svg .wc-bar:hover{opacity:1}
/* floating value tooltip (one shared node, follows the cursor) */
.wc-tip{position:fixed;z-index:60;pointer-events:none;background:var(--bg-card);
  border:1px solid var(--border);border-radius:8px;box-shadow:var(--shadow-md);
  padding:7px 11px;font-size:12px;color:var(--text);white-space:nowrap;
  opacity:0;transform:translateY(3px);transition:opacity .1s,transform .1s}
.wc-tip.show{opacity:1;transform:none}
.wc-tip .wr{font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-weight:700;font-size:13px}
.wc-tip .wn{color:var(--text-4);font-family:var(--font-mono)}
.wc-tip .dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;vertical-align:middle}
.wc-legend{display:flex;gap:16px;flex-wrap:wrap;margin:2px 0 0;font-size:12.5px;font-weight:700}
.wc-leg{display:inline-flex;align-items:center;gap:6px;color:var(--text-2)}
.wc-leg i{width:11px;height:11px;border-radius:3px}
.wc-leg small{color:var(--text-4);font-weight:400}
/* mobile: the journal trade tables SCROLL inside their wrap instead of crushing
   their columns into each other; stat cards stay 2-up but less chunky */
@media(max-width:640px){
  /* NB: no min-width override here. It used to say 560px and was dead code anyway
     (same specificity as `.jscroll .jtable`, which wins on source order) — and
     cramming 11 columns into 560px is not what we want regardless. The tables keep
     their real min-width and scroll sideways; only the type/padding tighten. */
  .jtable{font-size:12px}
  .jtable th,.jtable td{padding:7px 8px}
  .jcards{grid-template-columns:1fr 1fr;gap:8px}
  .jstat{padding:11px 13px}
  .jstat-v{font-size:21px}
  .jb-v{font-size:22px}
}
.jtable .jx.jhit{font-weight:600}
.jtable .jx.jstop{font-weight:600}
/* edge check (#3 benchmark) */
.jbench .jb-row{display:flex;gap:14px;flex-wrap:wrap;margin:4px 0 2px}
.jb-item{flex:1 1 150px;background:var(--bg-2);border:1px solid var(--border);
  border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:4px}
.jb-item.lead{border-color:#9c6ade;box-shadow:inset 0 0 0 1px rgba(156,106,222,.35)}
.jb-v{font-size:24px;font-weight:700;line-height:1.1}
.jb-v.pos{color:#1a8a4f}.jb-v.neg{color:#c0392b}
.jb-l{font-size:12px;color:var(--text-4)}
.jb-edge{margin-top:10px;font-size:14px;color:var(--text-2)}
.jb-edge b{font-size:15px}
.jb-edge.pos b{color:#1a8a4f}.jb-edge.neg b{color:#c0392b}
/* top row: scorecard left, PnL card top-right */
.jtop{display:flex;gap:18px;align-items:stretch;flex-wrap:wrap;margin-bottom:18px}
.jtop .jscore{flex:1 1 360px;margin:0}
/* paired half-rows (Edge check + Win-rate chart, mover scorecard + chart) */
.jrow{display:flex;gap:18px;align-items:stretch;flex-wrap:wrap;margin-top:18px}
.jrow>.card.span{margin-top:0;flex:1 1 360px;min-width:0}
.jrow>.wc-card{flex:1 1 360px;max-width:460px}
@media(max-width:760px){.jrow>.wc-card{max-width:none}}
/* deterministic column widths (11 cols: #, Token, Pump, Old, Price/24h,
   BN OI, OI (BN+BYB), Funding, Vol, FDV, MC). */
#ltab{table-layout:fixed;min-width:940px}
#ltab th{overflow:hidden}
#ltab th:nth-child(1){width:3%}                    /* # */
#ltab th:nth-child(2){width:24%;text-align:left}   /* Token */
#ltab th:nth-child(3){width:6%}                    /* Pump */
#ltab th:nth-child(4){width:6%}                    /* Old */
#ltab th:nth-child(5){width:11%}                   /* Price / 24h */
#ltab th:nth-child(6){width:9%}                    /* BN OI */
#ltab th:nth-child(7){width:11%}                   /* OI (BN+BYB) */
#ltab th:nth-child(8){width:9%}                    /* Funding */
#ltab th:nth-child(9){width:9%}                    /* Vol */
#ltab th:nth-child(10){width:6%}                   /* FDV */
#ltab th:nth-child(11){width:6%}                   /* MC */
/* Token cell degrades cleanly as the column narrows: the sparkline + symbol keep
   their slots, and only the NAME ellipsis-truncates (so the symbol — the key id —
   is never the thing that gets cut). min-width:0 lets the flex children shrink. */
#ltab td.tok a{min-width:0;width:100%;overflow:hidden}
#ltab td.tok .thumb{width:46px;height:24px;flex:0 0 auto}
/* name+symbol size to content (so badges stay grouped left), and only the NAME
   ellipsis-truncates if the row genuinely runs out of room — never prematurely */
#ltab td.tok .lname{flex:0 1 auto;min-width:0;display:flex;align-items:baseline;gap:5px;overflow:hidden}
#ltab td.tok .lnm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ltab td.tok .sym{flex:0 0 auto}
/* in-cell Buy badges are redundant now that v1/v2/v4 are their own columns —
   hide them in the list so the token name always has room (markup kept intact
   so live-refresh still works on the hidden node) */
#ltab td.tok .lbuys{display:none}
/* ── responsive: de-chunk the layout in two tiers ────────────────────────
   pills wrap as whole chips (never break their text); the dense 14-col list
   sheds low-priority columns at each tier so the essentials always fit with NO
   horizontal scroll (tablet ≤1080: drop #, BN OI, Vol, FDV, MC; phone ≤640:
   keep only Token · Pump · Price/24h · OI · Funding). */
.topnav{flex-wrap:wrap}
.topnav a{white-space:nowrap}
@media(max-width:1080px){
  #ltab{min-width:0;width:100%}
  #ltab th:nth-child(1),#ltab td:nth-child(1),
  #ltab th:nth-child(6),#ltab td:nth-child(6),
  #ltab th:nth-child(9),#ltab td:nth-child(9),
  #ltab th:nth-child(10),#ltab td:nth-child(10),
  #ltab th:nth-child(11),#ltab td:nth-child(11){display:none}
  #ltab th:nth-child(2){width:26%}                 /* Token */
  #ltab th:nth-child(3){width:9%}                  /* Pump */
  #ltab th:nth-child(4){width:9%}                  /* Old */
  #ltab th:nth-child(5){width:18%}                 /* Price / 24h */
  #ltab th:nth-child(7){width:19%}                 /* OI (BN+BYB) */
  #ltab th:nth-child(8){width:19%}                 /* Funding */
  /* token cell: smaller sparkline + drop the in-cell buy badges (the v1/v2/v4
     columns now carry that), so the name+symbol stay legible in a tight column */
  #ltab td.tok a{gap:7px}
  #ltab td.tok .thumb{width:44px;height:22px}
  #ltab td.tok .lbuys{display:none}
}
@media(max-width:640px){
  header{padding:12px 14px}
  header h1{font-size:17px}
  header p{font-size:11.5px;line-height:1.45}
  .topnav a{font-size:12px;padding:3px 10px}
  .filters{padding:10px 14px}
  /* keep Token · Pump · Price/24h · OI(BN+BYB) · Funding; hide the rest
     (Old %, BN OI, Vol, FDV, MC — low-priority on a phone). */
  #ltab{min-width:0;width:100%}
  #ltab th:nth-child(1),#ltab td:nth-child(1),
  #ltab th:nth-child(4),#ltab td:nth-child(4),
  #ltab th:nth-child(6),#ltab td:nth-child(6),
  #ltab th:nth-child(9),#ltab td:nth-child(9),
  #ltab th:nth-child(10),#ltab td:nth-child(10),
  #ltab th:nth-child(11),#ltab td:nth-child(11){display:none}
  #ltab th:nth-child(2){width:38%}                 /* Token */
  #ltab th:nth-child(3){width:12%}                 /* Pump */
  #ltab th:nth-child(5){width:20%}                 /* Price / 24h */
  #ltab th:nth-child(7){width:16%}                 /* OI (BN+BYB) */
  #ltab th:nth-child(8){width:14%}                 /* Funding */
  #ltab th{font-size:10.5px}
  #ltab td,#ltab th{padding:7px 4px}
  #ltab td.n{font-size:12px}
  #ltab td.tok a{gap:7px;flex-wrap:wrap}
  #ltab td.tok .thumb{width:40px;height:22px}
  #ltab td.tok .lname{white-space:normal;word-break:normal;overflow-wrap:normal}
  #ltab td .p24,#ltab td .vchg{font-size:10.5px}
}
/* pump-probability cell/badge: bold in the list, color-graded by strength
   (.list scope so both the Screener #ltab and the Pump Odds #otab share it) */
.list td.pump{font-weight:700}
.pump-hi{color:var(--pos)}
.pump-mid{color:var(--text-1)}
.pump-lo{color:var(--text-3)}
/* sub-tabs under Manipulated (Screener / AI Track Record / Pump Odds) */
.subnav{display:flex;gap:4px;margin:2px 0 16px;background:var(--bg-subtle);
  border:1px solid var(--border);border-radius:11px;padding:4px;max-width:440px}
.subnav a{flex:1;text-align:center;font-weight:700;color:var(--text-3);border-radius:8px;
  padding:9px 10px;text-decoration:none;white-space:nowrap;font-size:13px}
.subnav a.active{background:var(--primary);color:#fff}
.subnav a:hover:not(.active){color:var(--text-1)}
/* Pump Odds table (#otab): wide Token, three equal odds columns */
#otab{table-layout:fixed;min-width:420px;max-width:760px}
#otab th:nth-child(1){width:8%}
#otab th:nth-child(2){width:46%;text-align:left}
#otab th:nth-child(3),#otab th:nth-child(4),#otab th:nth-child(5){width:15.3%}
#otab td.tok a{display:flex;align-items:baseline;gap:5px;min-width:0;overflow:hidden}
#otab td.tok .lnm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:520px){#otab{min-width:0;width:100%}}
/* "Buy now" list scrolls INSIDE its own box (page stays put), with the column
   header pinned so you can scroll through every coin. */
.buynow-scroll{max-height:340px;overflow:auto;
  border:1px solid var(--border);border-radius:10px}
.buynow-scroll thead th{position:sticky;top:0;z-index:3;background:var(--bg-thead)}
/* AI Journal trade tables. DETERMINISTIC responsive table: table-layout:fixed
   (columns never hog/cram) + a width rule for EVERY column below + a min-width so
   a narrow screen scrolls SIDEWAYS instead of squishing. (Screener #ltab uses the
   same approach.)
   ⚠ Under table-layout:fixed a column with NO width rule gets whatever is LEFT —
   i.e. ZERO once the listed widths already sum to 100%. That is what broke this
   table on 2026-07-15: #jclosed had 11 columns and 9 rules, so "Stake"/"P&L ($)"
   collapsed, the P&L header wrapped one letter per line, and vertical-align:bottom
   then padded every other header down to match. If you add a column, add its width
   here — the count in each comment below must match the <thead>. */
/* The ledger is ~200 rows, so it scrolls inside its own box (header pinned) rather
   than turning the page into a wall of rows. Sideways scroll only kicks in below
   the table's min-width. */
.jscroll{max-height:520px;overflow:auto;-webkit-overflow-scrolling:touch;
  border:1px solid var(--border);border-radius:10px}
.jscroll .jtable{table-layout:fixed;min-width:1220px}
/* nowrap + overflow:hidden on TH is the guard that keeps a header from wrapping
   one-letter-per-line if a column is ever short on space (see the ⚠ above);
   vertical-align:middle means a stray tall header can't pad the whole row. */
.jscroll .jtable thead th{position:sticky;top:0;z-index:3;background:var(--bg-thead);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;vertical-align:middle;
  line-height:1.2}
.jscroll .jtable td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* The closed ledger is ~200 rows and grows forever. Skip render work for rows
   outside the scroll box — CSS-only virtualization, no JS, no library. The
   intrinsic size must match the real row height or the scrollbar jumps. */
.jscroll .jtable tbody tr{content-visibility:auto;contain-intrinsic-size:auto 36px}
.jwhy{color:var(--text-3);font-size:12px}
/* per-table column widths — one rule PER COLUMN, each set summing to 100% */
/* #jclosed — 11 cols: When Token Model ExpPump BoughtAt SoldAt ExpSell What Moved Cost Result
   (cols 8 + 9 carry the longest headers — "What happened"/"Actually pumped" — and
   were verified unclipped down to a 760px viewport; don't shave them.) */
#jclosed th:nth-child(1){width:9%}#jclosed th:nth-child(2){width:8%}
#jclosed th:nth-child(3){width:8%}#jclosed th:nth-child(4){width:9%}
#jclosed th:nth-child(5){width:9%}#jclosed th:nth-child(6){width:9%}
#jclosed th:nth-child(7){width:9%}#jclosed th:nth-child(8){width:11%}
#jclosed th:nth-child(9){width:11%}#jclosed th:nth-child(10){width:7%}
#jclosed th:nth-child(11){width:10%}
/* #jopen — 8 cols: When Token Model ExpPump BoughtAt ExpSell Cost GradesOn */
#jopen th:nth-child(1){width:13%}#jopen th:nth-child(2){width:11%}
#jopen th:nth-child(3){width:11%}#jopen th:nth-child(4){width:12%}
#jopen th:nth-child(5){width:12%}#jopen th:nth-child(6){width:14%}
#jopen th:nth-child(7){width:9%}#jopen th:nth-child(8){width:18%}
/* #jmover — 8 cols: When Token Why BoughtAt +10%12h +10%24h What Result */
#jmover th:nth-child(1){width:13%}#jmover th:nth-child(2){width:10%}
#jmover th:nth-child(3){width:19%}#jmover th:nth-child(4){width:12%}
#jmover th:nth-child(5){width:9%}#jmover th:nth-child(6){width:9%}
#jmover th:nth-child(7){width:16%}#jmover th:nth-child(8){width:12%}
#jmover{min-width:920px}#jopen{min-width:900px}
/* the mover table still shows the "why it bought" prose; let it wrap at spaces */
.jscroll .jtable td.jwhycell{white-space:normal;word-break:normal;overflow-wrap:normal}
.jscroll .jtable td.jwhycell .jset{white-space:nowrap}
/* Per-model P&L cards (_equity_grid) — each model's own books. */
.eq-pnl{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:2px 0 1px;
  font-variant-numeric:tabular-nums;font-family:var(--font-mono)}
.eq-pnl.pos{color:var(--pos)}.eq-pnl.neg{color:var(--neg)}
.eq-verdict{font-size:10.5px;line-height:1.35;margin-top:6px;color:var(--text-3);
  text-wrap:pretty}
.eq-verdict.proven{color:var(--pos)}
.eq-verdict.noise,.eq-verdict.early{color:var(--text-3);font-style:italic}
/* sticky header: the column titles pin to the top of the viewport as the WHOLE
   PAGE scrolls. Critically, .listwrap must NOT be a scroll container here (no
   max-height/overflow) — an overflow box would capture the sticky thead and make
   the table scroll inside its own window instead of with the page. */
#ltab thead th{position:sticky;top:0;z-index:3;background:var(--bg-thead)}
/* ⚠ screening chips (parked OI / extreme funding) on tiles + detail header */
.flags{display:inline-flex;gap:5px;flex-wrap:wrap}
.flag{background:var(--flag-bg);color:var(--neg);border-radius:9px;font-size:10.5px;
  font-weight:700;padding:1px 8px;white-space:nowrap;cursor:help}
.tile-head .flags{margin-left:auto}
.ovstat.hot{color:var(--neg);font-weight:700}
.flagrow{margin:0 0 10px}
#ltab td{overflow:hidden}
#ltab td.rank{text-align:center}
#ltab td.tge{font-size:12px;color:var(--text-2);text-align:center}
/* 24h column: price on top, 24h % change as a colored subtitle */
#ltab td .p24{display:block;font-size:11px;font-weight:600}
#ltab td .p24.pos{color:var(--pos)}
#ltab td .p24.neg{color:var(--neg)}
/* OI / Vol columns: value on top, 24h % change as a colored subtitle */
#ltab td .vchg{display:block;font-size:11px;font-weight:600}
#ltab td .vchg.pos{color:var(--pos)}
#ltab td .vchg.neg{color:var(--neg)}
/* detail-page stat badge: 24h change shown inline after the value */
.sigmeta .vchg{margin-left:5px;font-size:11px;font-weight:600}
.sigmeta .vchg.pos{color:var(--pos)}
.sigmeta .vchg.neg{color:var(--neg)}
/* live badge + cell flash (LIVE_JS polls the box every ~60s) */
#live-badge{font-size:11px;font-weight:600;padding:1px 7px;border-radius:10px;white-space:nowrap;
  margin-left:6px;border:1px solid var(--border)}
#live-badge.live-wait{color:var(--text-2)}
#live-badge.live-on{color:var(--pos);border-color:var(--pos)}
#live-badge.live-off{color:var(--neg);border-color:var(--neg)}
@keyframes liveflash{from{background:rgba(88,166,255,.35)}to{background:transparent}}
.liveflash{animation:liveflash 1s ease-out}
.tile .buyrow:empty{display:none}  /* a coin with no live Buy signal yet */
#ltab td.memo{max-width:none;white-space:normal;font-size:12px;color:var(--text-2)}
#ltab td.memo span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
/* per-token detail: full-width sections for perp + holders tables.
   The base .card delegates its padding to .info/.chart children, but these span
   sections render content (h3 / tables / donuts) as DIRECT children, so they need
   their own padding — without it titles sit flush in the top-left corner and the
   right edge (donut legends, wide tables) gets clipped by .card{overflow:hidden}. */
section.card.span{display:block;margin-top:22px;padding:22px 26px}
section.card.span h3{margin:0 0 14px;font-size:15px;color:var(--text);letter-spacing:-.01em}
section.card.span h3 .asof{font-size:12px;color:var(--text-4);font-weight:400;margin-left:8px}
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
table.perp th,table.holders th{text-align:right;padding:7px 10px;color:var(--text-3);font-weight:600;
  border-bottom:2px solid var(--border);font-size:12px}
table.perp th:first-child,table.holders th:nth-child(2){text-align:left}
table.perp td,table.holders td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--border-2)}
table.perp td.venue,table.holders td:nth-child(2){text-align:left}
table.perp td.iv{color:var(--text-4)}
table.perp tr.allrow td{font-weight:700;background:var(--bg-subtle)}
/* Others = aggregate of untracked venues; muted so it reads as a catch-all */
table.perp tr.otherrow td{color:var(--text-4);background:var(--bg-subtle)}
table.perp tr.otherrow td.venue{color:var(--text-3)}
table.perp tr.otherrow .htag{margin-left:7px}
table.holders td.mono,table.holders a.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
section.card.span .missing{color:var(--text-4);font-style:italic;padding:8px 0}
section.card.span p.note{font-size:12px;color:var(--text-4);margin:10px 0 0}
/* holder rows: a small tag chip for CEX/contract/burn wallets */
.htag{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:9px;font-size:11px;
  font-weight:600;color:var(--text-3);background:var(--bg-thead);vertical-align:middle}
/* holders table + donut side by side (stacks on narrow screens) */
.hol-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:20px;align-items:start}
.hol-grid>*{min-width:0}
@media(max-width:760px){.hol-grid{grid-template-columns:1fr}}
/* donut grids: fluid, 1–2 donuts per row; min-width:0 lets each cell shrink
   with its column instead of overflowing the card */
.donut-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:8px}
.donut-grid>*{min-width:0}
/* static SVG donuts (build-time; replaced the Plotly pies) */
.sdonut{margin:0;background:var(--bg-card)}
.sdonut figcaption{text-align:center;font-size:13px;font-weight:600;color:var(--text);margin:6px 0 8px}
.sd-row{display:flex;align-items:center;gap:14px}
.sd-svg{flex:0 0 150px;width:150px;height:150px}
.sd-ctr{font-size:11.5px;fill:var(--text-2);font-weight:600}
.sd-legend{list-style:none;margin:0;padding:0;font-size:11.5px;color:var(--text-2);min-width:0}
.sd-legend li{display:flex;align-items:center;gap:6px;padding:1.5px 0;flex-wrap:wrap}
.sd-legend li i{flex:0 0 9px;width:9px;height:9px;border-radius:50%;display:inline-block}
.sd-legend li b{font-weight:600;color:var(--text);margin-left:auto;padding-left:8px;white-space:nowrap}
@media(max-width:640px){.sd-svg{flex-basis:120px;width:120px;height:120px}}
/* wide tables (perp/holders) scroll horizontally on small screens instead of
   overflowing the card */
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
.hist-h{margin:18px 0 4px;font-size:14px;color:var(--text)}
.hist-h .asof{font-size:12px;color:var(--text-4);font-weight:400;margin-left:6px}
/* ── Top-holders chain picker + Bubblemaps link ──────────────────────────── */
.chainsel{display:flex;align-items:center;gap:12px;margin:0 0 14px;font-size:13px;color:var(--text-2)}
.chainsel select{font:inherit;padding:5px 8px;border:1px solid var(--border-input);border-radius:7px;
  background:var(--bg-card);color:var(--text);cursor:pointer}
.chainsel .chaincount{color:var(--text-4);font-size:12px}
.bmap-row{margin-top:14px}
.bmap-btn{display:inline-block;padding:9px 14px;border-radius:8px;background:var(--primary);color:#fff;
  font-size:13px;font-weight:600;text-decoration:none}
.bmap-btn:hover{background:var(--primary-deep)}
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
/* ── screener: filter panel, Buy/venue badges, signal cards ───────────── */
.filters{flex-wrap:wrap;gap:8px 12px}
#btn-filter{background:var(--primary);color:#fff;border:none;border-radius:7px;padding:5px 12px;font:inherit;font-size:12px;font-weight:700;cursor:pointer}
#btn-filter.open{background:var(--primary-deep)}
.fpanel{background:var(--bg-subtle);border:1px solid var(--border);border-radius:10px;padding:14px 18px;margin:10px 0 0}
.fp-presets{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.fp-pre{font:inherit;font-size:12px;font-weight:700;border:1px solid var(--border-input);background:var(--bg-card);color:var(--text-2);padding:4px 10px;border-radius:7px;cursor:pointer}
.fp-pre.active{background:var(--primary);color:#fff;border-color:var(--primary)}
.fp-actions{display:flex;align-items:center;gap:10px;margin-top:14px;padding-top:12px;border-top:1px solid var(--border)}
.fp-apply{font:inherit;font-size:13px;font-weight:700;background:var(--primary);color:#fff;border:none;border-radius:8px;padding:8px 22px;cursor:pointer}
.fp-apply:hover{background:var(--primary-deep)}
.fp-clear{font:inherit;font-size:13px;font-weight:600;background:var(--bg-card);color:var(--text-2);border:1px solid var(--border-input);border-radius:8px;padding:8px 16px;cursor:pointer}
.fp-clear:hover{color:var(--neg)}
.fp-hint{font-size:11.5px;color:var(--text-4);font-style:italic}
@media(max-width:640px){.fp-apply,.fp-clear{flex:1;padding:10px 0;text-align:center}}
.fp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px 18px}
.fp-group{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin:0}
.fp-group legend{font-size:11px;font-weight:700;color:var(--text-3);text-transform:uppercase;letter-spacing:.04em;padding:0 4px}
.fcb{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text-2);cursor:pointer;padding:2px 0}
.fcb input{margin:0;accent-color:var(--primary)}
.fcb span{user-select:none}
.buy{display:inline-block;border-radius:9px;font-size:10.5px;font-weight:700;padding:1px 8px;white-space:nowrap;color:#fff;margin:0 4px 2px 0}
.buy i{font-style:normal;opacity:.8;font-weight:600}
.buy.v1{background:#1e7a46}.buy.v2{background:#1f4e79}.buy.v3{background:#9b2d8f}.buy.v4{background:#b5642a}
.buy.mini{font-size:10px;padding:1px 6px}
/* setup explainer button + modal */
.setup-help{font:inherit;font-size:11px;font-weight:600;border:1px solid var(--border);
  background:var(--bg-card);color:var(--text-2);padding:1px 8px;border-radius:10px;cursor:pointer;margin-left:6px}
.setup-help:hover{color:var(--text);border-color:var(--primary)}
.setup-modal[hidden]{display:none}
.setup-modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:20px}
.sm-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.55)}
.sm-card{position:relative;max-width:760px;width:100%;max-height:86vh;overflow:auto;
  background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:24px 26px;box-shadow:0 18px 50px rgba(0,0,0,.4)}
.sm-x{position:absolute;top:12px;right:14px;border:none;background:none;color:var(--text-3);font-size:24px;line-height:1;cursor:pointer}
.sm-card h2{margin:0 0 8px;font-size:19px;color:var(--text)}
.sm-lead{font-size:13px;color:var(--text-2);margin:0 0 16px;line-height:1.6}
.sm-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:620px){.sm-grid{grid-template-columns:1fr}}
.sm-setup{border:1px solid var(--border);border-radius:10px;padding:12px 14px;background:var(--bg-subtle)}
.sm-setup h3{margin:0 0 6px;font-size:13.5px;color:var(--text);display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.sm-setup h3 em{font-style:normal;color:var(--text-4);font-weight:500;font-size:11.5px}
.sm-setup p,.sm-setup li{font-size:12.5px;color:var(--text-2);line-height:1.55;margin:4px 0}
.sm-setup ul{margin:4px 0 0;padding-left:16px}
.sm-foot{font-size:12px;color:var(--text-3);margin:16px 0 0;line-height:1.6}
.buyrow{margin:2px 0 0;display:flex;flex-wrap:wrap}
.lbuys{margin-left:2px}.lbuys:empty{display:none}
.lbuys .buy{margin:0 3px 0 0;vertical-align:middle}
.ven{display:inline-block;border-radius:6px;font-size:9.5px;font-weight:700;padding:1px 5px;margin-left:5px;vertical-align:middle;white-space:nowrap}
.ven.bn{background:#f3ba2f;color:#3a2c00}
.ven.byb{background:#e9eef5;color:#1f4e79;border:1px solid #cdd9e8}
td.sig .buy{margin:1px 3px 1px 0}
.sigmeta{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 14px;font-size:13px;color:var(--text-2)}
.sigmeta b{color:var(--text-3);font-weight:600;margin-right:4px;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em}
.buycards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:12px}
.buycard{border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;background:var(--bg-subtle);transition:border-color .16s var(--ease)}
.buycard.fired{border-color:var(--pos);background:var(--fired-bg);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pos) 22%,transparent)}
.buy.probe{background:#7d4ec9}.buy.dump{background:#c0392b}
.buy.beartrap{background:#1e8e64}.buy.spikeret{background:#c47f17}
.xcard{border-style:dashed}
.xcard.dump{border-color:#c0392b55}
.xcard.dump.fired{border-color:#c0392b;background:rgba(192,57,43,.06)}
.xcard.beartrap.fired{border-color:#1e8e64;background:rgba(30,142,100,.07)}
.xcard.spikeret.fired{border-color:#c47f17;background:rgba(196,127,23,.07)}
.bc-cred{font-size:11px;color:#9c6ade;margin:-2px 0 8px}
.xcard.dump .bc-cred{color:#c0392b}
.xcard.beartrap .bc-cred{color:#1e8e64}
.xcard.spikeret .bc-cred{color:#c47f17}
.bc-cred i{color:var(--text-4);font-style:italic}
.bc-head{font-weight:700;font-size:13px;color:var(--text);display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.bc-head span{font-weight:400;font-size:11.5px;color:var(--text-4)}
.bc-head .buy{margin:0}
.bc-no{font-size:11px;color:var(--text-4);font-weight:600;margin-left:auto}
.bc-rec{font-size:12px;color:var(--text-2);margin:-2px 0 8px}
.bc-state.na{font-size:12px;color:var(--text-4);font-style:italic}
.conds{list-style:none;margin:0;padding:0;font-size:11.5px;line-height:1.5;display:grid;gap:4px;font-family:var(--font-mono);letter-spacing:-.02em}
.conds li.ok{color:var(--pos)}
.conds li.x{color:var(--text-4);opacity:.85}
.binance-btn{display:inline-block;margin-top:14px;padding:9px 16px;border-radius:8px;background:#f3ba2f;color:#3a2c00;font-weight:700;font-size:13px;text-decoration:none}
.binance-btn:hover{background:#e0a91d}
"""

# style.css is served with Cache-Control: max-age=600 (GitHub Pages default) and
# every page links it with a bare "style.css" — so a browser that visited within
# 10min of a CSS-changing deploy keeps rendering NEW html against OLD cached CSS
# (this actually happened to the Models charts on ship day). A content-hash query
# param makes the URL itself change whenever the CSS changes, forcing an
# immediate re-fetch, while leaving cache-busting inert on the ~20-min data-only
# rebuilds where the CSS text is byte-identical.
_CSS_VER = hashlib.md5((RCSS + EXTRA_CSS).encode("utf-8")).hexdigest()[:8]

JS = """
<script>
const tiles=[...document.querySelectorAll('.tile')];
const rows=[...document.querySelectorAll('.lrow')];const items=[...tiles,...rows];
const search=document.getElementById('search'),count=document.getElementById('count');
const panel=document.getElementById('fpanel'),btnF=document.getElementById('btn-filter');
const cbs=[...panel.querySelectorAll('input[data-k]')];
// Buy v1-v4 presets TICK their condition checkboxes (so you see what each setup
// means) and filter through them. data-cond is kept live by LIVE_JS — rewritten
// from the box's fired flags each poll — so a preset still matches the live
// header counts even though it filters on the per-condition checkboxes.
const PRESETS={
  v1:['oi3up','oi3h5','px3h8','brk6h','fund01','oipx20'],
  v2:['coiltt','volign','oisrg','brkcoil','fundcold'],   // Ignition (OI-confirmed coil-break)
  v3:['oidd15','pxdd10','pxoidd','fund002','oireb8','brkwash'],
  v4:['oibld72','pxflat72','coil45','oilead2','fundsqz']};
window.__PRESETS=PRESETS;   // shared with LIVE_JS for the live data-cond rewrite
const pres=[...document.querySelectorAll('.fp-pre')];
const NUM_KEYS=new Set(['oi5m','oi10m','fdvlt150','fdvgte150','oifdv8','oifdv15','oimc25','oimc50','oivol3','fundneg','fundn01','fundn03']);
btnF.addEventListener('click',()=>{const open=panel.style.display==='none';
  panel.style.display=open?'':'none';btnF.classList.toggle('open',open);});
function checked(){return cbs.filter(c=>c.checked).map(c=>c.dataset.k);}
function numOk(el,k){
  if(k==='oi5m')return parseFloat(el.dataset.oi||'0')>5e6;
  if(k==='oi10m')return parseFloat(el.dataset.oi||'0')>1e7;
  if(k==='fdvlt150'){const f=parseFloat(el.dataset.fdvnum||'-1');return f>=0&&f<150e6;}
  if(k==='fdvgte150')return parseFloat(el.dataset.fdvnum||'-1')>=150e6;
  if(k==='oifdv8')return parseFloat(el.dataset.oifdv||'-1')>=0.08;
  if(k==='oifdv15')return parseFloat(el.dataset.oifdv||'-1')>=0.15;
  if(k==='oimc25')return parseFloat(el.dataset.oimc||'-1')>=0.25;
  if(k==='oimc50')return parseFloat(el.dataset.oimc||'-1')>=0.5;
  if(k==='oivol3')return parseFloat(el.dataset.oivol||'-1')>=3;
  if(k==='fundneg')return parseFloat(el.dataset.fundingnum||'999')<0;
  if(k==='fundn01')return parseFloat(el.dataset.fundingnum||'999')<=-0.001;
  if(k==='fundn03')return parseFloat(el.dataset.fundingnum||'999')<=-0.003;
  return true;}
let appliedCond=[];   // conditions snapshot — the list filters on THIS, set only by Apply
function ok(el){
  const q=search.value.trim().toLowerCase();
  if(q&&!el.dataset.search.includes(q))return false;
  const ck=appliedCond;if(!ck.length)return true;
  const cond=el.dataset.cond||'||';
  for(const k of ck){
    if(NUM_KEYS.has(k)){if(!numOk(el,k))return false;}
    else{if(!cond.includes('|'+k+'|'))return false;}}
  return true;}
function apply(){for(const el of items)el.style.display=ok(el)?'':'none';
  count.textContent=tiles.filter(ok).length+' / '+tiles.length+' coins';}
window.__refilter=apply;   // LIVE_JS re-runs the filter after it rewrites data-cond
// a preset is "active" when all its condition checkboxes are ticked
function syncPresets(){const ck=new Set(checked());
  pres.forEach(b=>{const keys=PRESETS[b.dataset.pre]||[];
    b.classList.toggle('active',keys.length>0&&keys.every(k=>ck.has(k)));});}
// Conditions/presets are DEFERRED — they only change the list when Apply is
// pressed (search stays live). A hint nudges you to Apply when the ticked boxes
// differ from what's currently applied.
const hint=document.getElementById('fp-hint');
function markDirty(){const ck=checked();
  const same=ck.length===appliedCond.length&&ck.every(k=>appliedCond.includes(k));
  if(hint)hint.textContent=same?'':'unapplied changes';}
search.addEventListener('input',apply);                       // search filters live
cbs.forEach(c=>c.addEventListener('change',()=>{syncPresets();markDirty();}));
// clicking a preset ticks/unticks its condition checkboxes (apply on Apply)
pres.forEach(b=>b.addEventListener('click',()=>{
  const keys=PRESETS[b.dataset.pre]||[];const isOn=b.classList.contains('active');
  cbs.forEach(c=>{if(keys.includes(c.dataset.k))c.checked=!isOn;});
  syncPresets();markDirty();}));
document.getElementById('fp-apply').addEventListener('click',()=>{
  appliedCond=checked();apply();markDirty();});               // commit the conditions
document.getElementById('fp-clear').addEventListener('click',()=>{
  cbs.forEach(c=>c.checked=false);search.value='';appliedCond=[];syncPresets();apply();markDirty();});
const bG=document.getElementById('view-grid'),bL=document.getElementById('view-list');
const views=document.getElementById('views');
const VIEW_KEY='ll-view-scams';
function setView(v,persist){views.className='view-'+v;bG.classList.toggle('active',v=='grid');bL.classList.toggle('active',v=='list');
  if(persist){try{localStorage.setItem(VIEW_KEY,v);}catch(e){}}}
bG.addEventListener('click',()=>setView('grid',true));bL.addEventListener('click',()=>setView('list',true));
let _v0='grid';try{_v0=localStorage.getItem(VIEW_KEY)||'grid';}catch(e){}
setView(_v0=='list'?'list':'grid',false);
const ltab=document.getElementById('ltab');
if(ltab){const tb=ltab.querySelector('tbody');
 let sortCol=-1,sortDir=-1;             // dir: -1 desc, 1 asc
 const ths=ltab.querySelectorAll('th');
 function doSort(i,dir){
  const rs=[...tb.rows];rs.sort((a,b)=>{const x=a.cells[i].dataset.s??a.cells[i].textContent,y=b.cells[i].dataset.s??b.cells[i].textContent;
   const nx=parseFloat(x),ny=parseFloat(y);const c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:(''+x).localeCompare(y);return dir>0?c:-c;});
  ths.forEach(h=>h.classList.remove('sorted','asc','desc'));ths[i].classList.add('sorted',dir>0?'asc':'desc');
  rs.forEach(r=>tb.appendChild(r));sortCol=i;sortDir=dir;}
 // re-apply the active sort after the live feed changes values, so the order
 // always matches what's shown (e.g. the top 24h mover stays on top in realtime).
 window.__resort=function(){if(sortCol>=0)doSort(sortCol,sortDir);};
 ths.forEach((th,i)=>{
  th.tabIndex=0;th.setAttribute('role','button');th.setAttribute('aria-label','Sort by '+th.textContent);
  th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();th.click();}});
  th.addEventListener('click',()=>{doSort(i,sortCol===i?-sortDir:-1);});});
 // default order = 24h column, biggest movers first (client-side, using LIVE values)
 let d=-1;ths.forEach((th,i)=>{if(/24h|price/i.test(th.textContent))d=i;});
 if(d>=0)doSort(d,-1);}
apply();
</script>"""


def _enrich_from_screener(rec):
    """Map screener market data into the same field names scam_data uses, so
    all existing renderers (_detail, _tile, _perf, _supply_dl, etc.) work."""
    sym = rec["symbol"]
    sr = _sig(sym)
    mkt = sr.get("market") or {}
    if mkt.get("name") and rec.get("name") == sym:
        rec["name"] = mkt["name"]
    if mkt.get("price") and not rec.get("price"):
        rec["price"] = mkt["price"]
    if mkt.get("fdv") and not (rec.get("fdv") or rec.get("csv_fdv")):
        rec["fdv"] = mkt["fdv"]
    if mkt.get("mcap") and not (rec.get("mcap") or rec.get("csv_mc")):
        rec["mcap"] = mkt["mcap"]
    if mkt.get("vol24h") and not rec.get("vol"):
        rec["vol"] = mkt["vol24h"]
    if mkt.get("circ_supply") and not rec.get("circ_supply"):
        rec["circ_supply"] = mkt["circ_supply"]
    if mkt.get("total_supply") and not rec.get("total_supply"):
        rec["total_supply"] = mkt["total_supply"]
    if mkt.get("max_supply") and not rec.get("max_supply"):
        rec["max_supply"] = mkt["max_supply"]
    if mkt.get("chain") and not rec.get("chain"):
        rec["chain"] = mkt["chain"]
    if mkt.get("contract") and not rec.get("contract"):
        rec["contract"] = mkt["contract"]
    if mkt.get("slug") and not rec.get("cmc_slug"):
        rec["cmc_slug"] = mkt["slug"]
    # The box's combined Binance+Bybit OI is the detail-page fallback when the
    # CoinGecko 6-venue perp snapshot (fetch_perp_markets) hasn't reached this
    # screener coin yet — keeps the OI line consistent with the tile.
    if sr.get("oi_combined") and not rec.get("oi_usd"):
        rec["oi_usd"] = sr["oi_combined"]
    if mkt.get("tge") and not TGE.get(sym):
        TGE[sym] = mkt["tge"]
    tot = rec.get("total_supply") or rec.get("max_supply")
    circ = rec.get("circ_supply")
    if circ and tot and tot > 0:
        rec["circ_ratio"] = circ / tot


def main():
    if not DATA.exists():
        print(f"{DATA} missing -- run fetch_scam_data.py first")
        return
    data = json.loads(DATA.read_text(encoding="utf-8"))
    FUNDING.update(_load_funding([r["symbol"].upper() for r in data.values()]))
    if TGE_CACHE.exists():
        TGE.update(json.loads(TGE_CACHE.read_text(encoding="utf-8")))
    _load_screener()
    platforms = _load_platforms()

    recs = list(data.values())
    have = {r["symbol"].upper() for r in recs}
    for sym, sr in SCREENER.items():
        if sym in have:
            continue
        recs.append({"symbol": sym, "name": sym,
                     "sections": sr.get("sections", []), "sources": sr.get("sources", [])})

    for r in recs:
        _enrich_from_screener(r)

    # Perp-only: this tab is a perpetuals screener, so drop any coin without a
    # working perp (no live OI and no evaluable signal). Reversible — a coin
    # returns the moment its perp shows up in the screener feed.
    dropped = sorted(r["symbol"].upper() for r in recs if not _has_perp(r["symbol"]))
    recs = [r for r in recs if _has_perp(r["symbol"])]
    if dropped:
        print(f"perp-only filter: dropped {len(dropped)} non-perp coin(s): {dropped}")

    # Default order = trending: biggest 24h price movers first. Tokens with no
    # 24h change sink to the bottom (ordered by OI as a tiebreaker). The list
    # stays click-sortable on every column.
    def _trend_key(r):
        p24 = (_perf(r) or {}).get("p24")
        oi = _sig(r["symbol"]).get("oi_combined") or 0
        return (0 if p24 is None else 1, p24 if p24 is not None else 0, oi)
    recs.sort(key=_trend_key, reverse=True)

    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "style.css").write_text(RCSS + EXTRA_CSS, encoding="utf-8")
    (SITE / "index.html").write_text(_index(recs), encoding="utf-8")
    (SITE / "trades.html").write_text(_trades_page(recs), encoding="utf-8")
    for r in recs:
        page = _detail(r, platforms)
        (SITE / f"{r['symbol'].lower()}.html").write_text(page, encoding="utf-8")
    # Prune orphaned pages: a coin that leaves the screener universe (or a sub-tab
    # we retired) would otherwise linger as a stale file and still deploy — carrying
    # old content (e.g. a dropped setup). Keep only the pages this build wrote.
    keep = {"index.html", "trades.html"} | {f"{r['symbol'].lower()}.html" for r in recs}
    for f in SITE.glob("*.html"):
        if f.name not in keep:
            f.unlink()
    n_curated = len(data)
    n_screener = len(recs) - n_curated
    n_market = sum(1 for r in recs if r not in data.values() and
                   (_sig(r["symbol"]) or {}).get("market"))
    print(f"wrote {SITE/'index.html'} + {len(recs)} detail pages "
          f"({n_curated} curated + {n_screener} screener, {n_market} enriched)")


if __name__ == "__main__":
    main()

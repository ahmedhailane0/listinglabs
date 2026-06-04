"""Build an HTML report of all tracked Alpha listings.

Generates an index page (one tile per token) plus a detail page per token
(info + full chart). Reads every `listings/*.json` and pairs it with its
rendered `charts/<token>_listing_reaction.png`. Regenerate any time after
re-rendering charts:

    python build_listing_report.py

Outputs into the unified `Listinglabs/report/` folder (build everything with
`python build_all.py`).
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from listing_chart import fmt_usd_compact, fmt_subscript_price, parse_iso
from interactive_chart import chart_html
from venues import venue_color, venue_url
import metrics

HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
CHARTS = HERE / "charts"
OUT_DIR = HERE / "Listinglabs" / "report"
SOCIALS_CACHE = HERE.parent / "cache" / "token_socials.json"
ANN_CACHE = HERE.parent / "cache" / "announcements.json"
OI_CACHE = HERE / "oi_cmc.json"
FUNDING_CACHE = HERE.parent / "cache" / "funding.json"
BWENEWS_SIGNALS = HERE.parent / "cache" / "bwenews_signals.json"

# token symbol (upper) -> {"oi_usd", "n_pairs", "top": [...], "fetched_utc", ...}.
# CURRENT/live open interest from CMC, summed across perp pairs. Populated in
# main(). Snapshot, not at-listing — always rendered with its fetch date.
OI: dict[str, dict] = {}

# token symbol (upper) -> {"amount": float|None, "source": str|None,
# "investors": [{"name","url","lead"}]}. Built by build_funding.py. Populated in main().
FUNDING: dict[str, dict] = {}

# slug -> {exchange_label: {"url": str, "title": str}}. Exact announcement URLs
# resolved from venue APIs; falls back to scoped search when absent.
ANN: dict[str, dict] = {}

# slug -> {"website": str|None, "twitter": str|None}. Populated in main().
SOCIALS: dict[str, dict] = {}

def _alpha_time(cfg: dict):
    for ev in cfg.get("events", []):
        if ev["exchange"] == "Binance Alpha":
            return parse_iso(ev["iso_time_utc"])
    return parse_iso(cfg["window_start_utc"])


# Notes that mark an event as a coarse daily-resolution sweep rather than a real
# observed listing time. Such events sit at midnight UTC and routinely predate the
# true listing (e.g. CTR's "Gate.io Spot 00:00Z" precedes its real Alpha open at
# 13:00Z), so they must NOT be treated as the TGE.
_SWEEP_MARKERS = ("earliest-candle sweep", "daily resolution")


def _is_sweep(ev: dict) -> bool:
    note = (ev.get("note") or "").lower()
    return any(m in note for m in _SWEEP_MARKERS)


def _tge_time(cfg: dict):
    """Best estimate of the token's generation/first-listing moment.

    Earliest *precisely observed* venue event (sweep artifacts excluded). Falls
    back to the Binance Alpha time, then the window start.
    """
    precise = [parse_iso(ev["iso_time_utc"]) for ev in cfg.get("events", [])
               if ev.get("iso_time_utc") and not _is_sweep(ev)]
    if precise:
        return min(precise)
    return _alpha_time(cfg)


def _slug(cfg: dict) -> str:
    return cfg["token"].lower()


# Fixed filter chips, always shown even when no token matches yet. Binance is
# split into its distinct signals (Alpha / Spot / Perp); the other majors match
# at the exchange-family level (any market type), then the Korean venues.
# Each chip: (label, group, prefixes-that-match an event's exchange name).
CHIP_SPECS = [
    ("Binance Alpha", "Binance", ("Binance Alpha",)),
    ("Binance Spot", "Binance", ("Binance Spot",)),
    ("Binance Perp", "Binance", ("Binance Perp",)),
    ("Coinbase", "Majors", ("Coinbase",)),
    ("OKX", "Majors", ("OKX",)),
    ("Bybit", "Majors", ("Bybit",)),
    ("Kraken", "Majors", ("Kraken",)),
    ("KuCoin", "Majors", ("KuCoin",)),
    ("Bitget", "Majors", ("Bitget",)),
    ("Gate.io", "Majors", ("Gate",)),
    ("Upbit", "Korean", ("Upbit",)),
    ("Bithumb", "Korean", ("Bithumb",)),
    ("Coinone", "Korean", ("Coinone",)),
]


def _token_chips(cfg: dict) -> list[str]:
    """Which fixed chips this token's listing events satisfy."""
    exchanges = [e["exchange"] for e in cfg.get("events", [])]
    out = []
    for label, _group, prefixes in CHIP_SPECS:
        if any(ex.startswith(p) for ex in exchanges for p in prefixes):
            out.append(label)
    return out


def _chart_rel(cfg: dict) -> str | None:
    chart = CHARTS / f"{_slug(cfg)}_listing_reaction.png"
    return f"../charts/{chart.name}" if chart.exists() else None


KLINES = HERE.parent / "cache"
_SPARK_W, _SPARK_H = 100.0, 32.0   # viewBox units; CSS stretches to tile width


def _sparkline(cfg: dict) -> str:
    """Inline SVG mini-chart of the close-price path from the token's own
    kline cache (same data the detail Plotly chart uses). Vector + tiny, so it
    replaces the ~250KB PNG thumbnail. A faint vertical line marks the Alpha
    listing. Returns '' if no kline data."""
    p = KLINES / f"{_slug(cfg)}_klines_5m_alpha.json"
    if not p.exists():
        return ""
    rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
    if len(rows) < 2:
        return ""

    # Downsample to ~120 points to keep the path string small.
    step = max(1, len(rows) // 120)
    pts = rows[::step]
    closes = [r[4] for r in pts]
    times = [r[0] for r in pts]
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    n = len(closes)

    def x(i): return round(i / (n - 1) * _SPARK_W, 2)
    def y(v): return round(_SPARK_H - (v - lo) / span * (_SPARK_H - 2) - 1, 2)

    line = " ".join(f"{x(i)},{y(c)}" for i, c in enumerate(closes))
    area = f"0,{_SPARK_H} {line} {_SPARK_W},{_SPARK_H}"

    # Alpha marker: x of the candle nearest the Alpha listing time.
    alpha_ms = int(_alpha_time(cfg).timestamp() * 1000)
    ai = min(range(n), key=lambda i: abs(times[i] - alpha_ms))
    marker = (f'<line x1="{x(ai)}" y1="0" x2="{x(ai)}" y2="{_SPARK_H}" '
              f'class="spark-alpha"/>') if 0 < ai < n - 1 else ""

    return (
        f'<svg class="thumb" viewBox="0 0 {_SPARK_W:g} {_SPARK_H:g}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polygon class="spark-fill" points="{area}"/>'
        f'<polyline class="spark-line" points="{line}"/>'
        f'{marker}</svg>'
    )


def _events_rows(cfg: dict) -> str:
    rows = []
    sym = cfg["token"]
    name = cfg.get("name", sym)
    chain, contract = cfg.get("chain", ""), cfg.get("token_contract", "")
    ann_map = ANN.get(_slug(cfg), {})
    for ev in sorted(cfg.get("events", []), key=lambda e: e["iso_time_utc"]):
        dt = parse_iso(ev["iso_time_utc"])
        # Daily-resolution sweep events are NOT verified listing times — they're the
        # earliest candle a venue's API still serves (e.g. Bitget retains only ~300
        # days, so 31 tokens share a 2025-08-04 floor). Render them date-only, muted,
        # and flagged so they never masquerade as a precise listing time (audit M-1).
        if _is_sweep(ev):
            t = (f'<span class="approx" title="Earliest candle the venue API still '
                 f'serves — not a verified listing time (data-limited).">'
                 f'≈ {dt.strftime("%Y-%m-%d")}</span>')
        else:
            t = dt.strftime("%Y-%m-%d %H:%M")
        cls = " class=\"alpha\"" if ev["exchange"] == "Binance Alpha" else ""
        dot = f"<span class=\"dot\" style=\"background:{venue_color(ev['exchange'])}\"></span>"
        ex = html.escape(ev["exchange"])
        url = venue_url(ev["exchange"], sym, chain, contract)
        label = (f"<a href=\"{html.escape(url)}\" target=\"_blank\" rel=\"noopener\">{ex} ↗</a>"
                 if url else ex)
        # The listing-announcement article (when the exchange said it would list)
        # is embedded as a clickable link in the NOTE column — direct article only,
        # no search fallback. Any hand-written note text precedes it.
        note_bits = []
        if ev.get("note"):
            note_bits.append(html.escape(ev["note"]))
        exact = ann_map.get(ev["exchange"])
        if exact and exact.get("url"):
            tip = exact.get("title", f"{ev['exchange']} listing announcement")
            adate = (exact.get("date") or "")[:10]
            txt = f"Announcement{(' ' + adate) if adate else ''} ↗"
            note_bits.append(
                f"<a class=\"ann-link\" href=\"{html.escape(exact['url'])}\" target=\"_blank\" "
                f"rel=\"noopener\" title=\"{html.escape(tip)}\">{txt}</a>")
        rows.append(
            f"<tr{cls}><td>{dot}{label}</td>"
            f"<td class=\"t\">{t}</td>"
            f"<td class=\"note\">{' · '.join(note_bits)}</td></tr>"
        )
    return "\n".join(rows)


def _annotations(cfg: dict) -> str:
    anns = cfg.get("annotations", [])
    if not anns:
        return ""
    items = []
    for a in anns:
        t = parse_iso(a["iso_time_utc"]).strftime("%Y-%m-%d %H:%M")
        items.append(
            f"<li><b>{html.escape(a['label'])}</b> — {t} UTC"
            f"<br><span class=\"note\">{html.escape(a.get('note', ''))}</span></li>"
        )
    return f"<div class=\"ann\"><h4>Annotations</h4><ul>{''.join(items)}</ul></div>"


def _page(title: str, body: str, extra_head: str = "") -> str:
    # CSP as a <meta> (GitHub Pages can't set response headers). Allows same-origin +
    # the inline scripts/styles Plotly emits, but blocks any external script load or
    # data exfil — defense-in-depth on top of output escaping (audit L-3).
    csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
           "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
           "connect-src 'self'; base-uri 'self'; frame-ancestors 'none'")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="style.css">{extra_head}</head>
<body>{body}</body></html>"""


def _tile(cfg: dict) -> str:
    fdv = fmt_usd_compact(cfg["fdv_usd"]) if cfg.get("fdv_usd") else "—"
    alpha = _alpha_time(cfg).strftime("%Y-%m-%d")
    n_venues = len({e["exchange"] for e in cfg.get("events", [])})
    thumb = _sparkline(cfg)
    venues = "|" + "|".join(_token_chips(cfg)) + "|"
    search = html.escape(f"{cfg.get('name', cfg['token'])} {cfg['token']}".lower())
    return f"""
    <a class="tile" href="{_slug(cfg)}.html" data-venues="{html.escape(venues)}" data-search="{search}">
      {thumb}
      <div class="tile-body">
        <div class="tile-head"><span class="name">{html.escape(cfg.get('name', cfg['token']))}</span>
          <span class="sym">{html.escape(cfg['token'])}</span></div>
        <div class="tile-meta">
          <span><b>FDV</b> {fdv}</span>
          <span><b>Alpha</b> {alpha}</span>
          <span><b>Venues</b> {n_venues}</span>
        </div>
      </div>
    </a>"""


def _pct(x: float) -> str:
    cls = "pos" if x >= 0 else "neg"
    return f"<span class=\"{cls}\">{x:+.1f}%</span>"


def _explorer_url(chain: str, contract: str) -> str | None:
    base = {"bsc": "https://bscscan.com/token/", "base": "https://basescan.org/token/",
            "ethereum": "https://etherscan.io/token/", "eth": "https://etherscan.io/token/",
            "solana": "https://solscan.io/token/"}.get(chain)
    return base + contract if base else None


def _cmc_url(cfg: dict) -> str | None:
    slug = cfg.get("cmc_slug")
    return f"https://coinmarketcap.com/currencies/{slug}/" if slug else None


def _cmc_tag(cfg: dict, label: str = "CoinMarketCap") -> str:
    """Small source attribution that links a metric back to its CMC page."""
    url = _cmc_url(cfg)
    if not url:
        return ""
    return (f"<a class=\"src\" href=\"{url}\" target=\"_blank\" rel=\"noopener\">"
            f"{html.escape(label)} ↗</a>")


def _links(cfg: dict) -> str:
    """Project + data-source links: project site & X first, then the data
    providers (CMC / GeckoTerminal / chain explorer)."""
    soc = SOCIALS.get(_slug(cfg), {})
    project, data = [], []

    web = soc.get("website")
    if web:
        project.append(f"<a href=\"{html.escape(web)}\" target=\"_blank\" rel=\"noopener\">Website ↗</a>")
    tw = soc.get("twitter")
    if tw:
        handle = tw.lstrip("@")
        project.append(f"<a href=\"https://x.com/{html.escape(handle)}\" target=\"_blank\" rel=\"noopener\">X (@{html.escape(handle)}) ↗</a>")

    cmc = _cmc_url(cfg)
    if cmc:
        data.append(f"<a href=\"{cmc}\" target=\"_blank\" rel=\"noopener\">CoinMarketCap</a>")
    chain, pool = cfg.get("chain"), cfg.get("gecko_pool")
    if chain and pool:
        data.append(f"<a href=\"https://www.geckoterminal.com/{chain}/pools/{pool}\" target=\"_blank\" rel=\"noopener\">GeckoTerminal</a>")
    exp = _explorer_url(chain, cfg.get("token_contract", "")) if chain else None
    if exp:
        data.append(f"<a href=\"{exp}\" target=\"_blank\" rel=\"noopener\">Contract ↗</a>")

    blocks = []
    if project:
        blocks.append(f"<div class=\"links project\">{' · '.join(project)}</div>")
    if data:
        blocks.append(f"<div class=\"links\">{' · '.join(data)}</div>")
    return "".join(blocks)


def _supply_lines(cfg: dict) -> str:
    out = []
    mcap = cfg.get("mcap_usd")
    if mcap:
        out.append(f"<dt>Market cap</dt><dd>{fmt_usd_compact(mcap)} "
                   f"<span class=\"src\">{html.escape(cfg.get('mcap_source', ''))}</span> "
                   f"{_cmc_tag(cfg)}</dd>")
    fdv, circ, total = cfg.get("fdv_usd"), cfg.get("circulating_supply"), cfg.get("total_supply")
    if mcap and fdv:
        out.append(f"<dt>FDV / MC</dt><dd>{fdv / mcap:.1f}×</dd>")
    if circ and total:
        out.append(f"<dt>Float</dt><dd>{circ / total * 100:.1f}% circulating</dd>")
    out.append(_oi_lines(cfg))
    return "".join(out)


def _oi_lines(cfg: dict) -> str:
    """Current open-interest snapshot (CMC, summed across perp venues). Always
    labelled 'current' with the fetch date so it is never read as at-listing."""
    rec = OI.get(cfg["token"].upper())
    if not rec or not rec.get("oi_usd"):
        return ""
    asof = (rec.get("fetched_utc") or "")[:10]
    n = rec.get("n_pairs") or 0
    pct = rec.get("oi_pct_mcap")
    pct_str = f" · {pct:.0f}% of mcap" if pct is not None else ""
    top = [t for t in (rec.get("top") or []) if t.get("oi_usd")][:3]
    venues = ", ".join(f"{html.escape(t['exchange'])} {fmt_usd_compact(t['oi_usd'])}"
                       for t in top)
    note = (f"<div class=\"note\">Top venues: {venues}</div>" if venues else "")
    return (f"<dt>Open interest</dt><dd>{fmt_usd_compact(rec['oi_usd'])}{pct_str} "
            f"<span class=\"src\">current — {n} perp venue{'s' if n != 1 else ''}, "
            f"as of {asof}</span> {_cmc_tag(cfg)}{note}</dd>")


# ---- funding (RootData amount + investors, Excel amount fallback) ----

def _investor_links(investors: list, limit: int | None = None) -> list[str]:
    out = []
    for i in (investors if limit is None else investors[:limit]):
        name = html.escape(i.get("name", ""))
        lead = " <span class=\"lead\">(lead)</span>" if i.get("lead") else ""
        if i.get("url"):
            out.append(f"<a href=\"{html.escape(i['url'])}\" target=\"_blank\" rel=\"noopener\">{name}</a>{lead}")
        else:
            out.append(f"{name}{lead}")
    return out


def _funding_lines(cfg: dict) -> str:
    """Funding dt/dd for the detail page: amount + who funded (linked)."""
    rec = FUNDING.get(cfg["token"].upper())
    if not rec or (not rec.get("amount") and not rec.get("investors")):
        return "<dt>Funding</dt><dd>—</dd>"
    amt = fmt_usd_compact(rec["amount"]) if rec.get("amount") else "—"
    src = rec.get("source")
    src_tag = f" <span class=\"src\">via {html.escape(src)}</span>" if src else ""
    invs = rec.get("investors") or []
    inv_html = ""
    if invs:
        links = _investor_links(invs, limit=8)
        more = f" +{len(invs) - 8} more" if len(invs) > 8 else ""
        inv_html = f"<div class=\"note\">Backed by: {', '.join(links)}{more}</div>"
    return f"<dt>Funding</dt><dd>{amt}{src_tag}{inv_html}</dd>"


# ---- performance + list view ----

def _perf(cfg: dict) -> dict | None:
    """Pull the checkpoint set for the list view / sorting, or None if no data."""
    r = metrics.reaction(cfg)
    if not r:
        return None
    chk = {lbl.lower().lstrip("+").strip(): v for lbl, v in r["checkpoints"]}
    return {"since": r["change_pct"], "p24": chk.get("24h"), "p7": chk.get("7d"),
            "p30": chk.get("30d"), "p90": chk.get("90d"), "dd": r["max_drawdown_pct"]}


_NEG_INF = "-1e18"  # sort key for missing numeric cells -> always sink to the bottom


def _num_cell(v: float | None, pct: bool = True, color: bool = True) -> str:
    if v is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    cls = (" pos" if v >= 0 else " neg") if color else ""
    txt = f"{v:+.1f}%" if pct else fmt_usd_compact(v)
    return f'<td class="n{cls}" data-s="{v:.4f}">{txt}</td>'


def _funding_cell(cfg: dict) -> str:
    rec = FUNDING.get(cfg["token"].upper()) or {}
    amt = rec.get("amount")
    invs = rec.get("investors") or []
    if not amt and not invs:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    amt_txt = fmt_usd_compact(amt) if amt else "—"
    lead = next((i["name"] for i in invs if i.get("lead")), invs[0]["name"] if invs else "")
    sub = f' <span class="sub">{html.escape(lead)}</span>' if lead else ""
    return f'<td class="n" data-s="{amt or 0:.0f}">{amt_txt}{sub}</td>'


def _tge_cell(cfg: dict) -> str:
    """TGE date cell: date on top, UTC time below, sorted by epoch seconds."""
    t = _tge_time(cfg)
    if t is None:
        return f'<td class="n" data-s="{_NEG_INF}">—</td>'
    epoch = t.timestamp()
    return (f'<td class="tge" data-s="{epoch:.0f}">{t.strftime("%Y-%m-%d")}'
            f'<span class="sub">{t.strftime("%H:%M")} UTC</span></td>')


LIST_COLS = ["#", "Token", "TGE", "Since", "24h", "7d", "30d", "90d", "FDV", "MC", "OI%", "Funding"]


def _list_row(cfg: dict) -> str:
    venues = "|" + "|".join(_token_chips(cfg)) + "|"
    search = html.escape(f"{cfg.get('name', cfg['token'])} {cfg['token']}".lower())
    name = html.escape(cfg.get("name", cfg["token"]))
    sym = html.escape(cfg["token"])
    p = _perf(cfg) or {}
    fdv, mcap = cfg.get("fdv_usd"), cfg.get("mcap_usd")
    oi = (OI.get(cfg["token"].upper()) or {}).get("oi_pct_mcap")
    new_badge = '<span class="newbadge">NEW</span>' if cfg.get("new") else ''
    tok_cell = (f'<td class="tok" data-s="{search}">'
                f'<a href="{_slug(cfg)}.html">{_sparkline(cfg)}'
                f'<span class="lname">{name} <span class="sym">{sym}</span>{new_badge}</span></a></td>')
    return (
        f'<tr class="lrow" data-venues="{html.escape(venues)}" data-search="{search}">'
        f'<td class="rank"></td>'
        f"{tok_cell}"
        f"{_tge_cell(cfg)}"
        f"{_num_cell(p.get('since'))}{_num_cell(p.get('p24'))}{_num_cell(p.get('p7'))}"
        f"{_num_cell(p.get('p30'))}{_num_cell(p.get('p90'))}"
        f"{_num_cell(fdv, pct=False, color=False)}{_num_cell(mcap, pct=False, color=False)}"
        f"{_num_cell(oi, pct=True, color=False)}"
        f"{_funding_cell(cfg)}</tr>")


def _list_table(cfgs: list[dict]) -> str:
    head = "".join(f'<th data-i="{i}">{c}</th>' for i, c in enumerate(LIST_COLS))
    rows = "\n".join(_list_row(c) for c in cfgs)
    return (f'<table class="list" id="ltab"><thead><tr>{head}</tr></thead>'
            f"<tbody>{rows}</tbody></table>")


def _reaction_block(cfg: dict) -> str:
    r = metrics.reaction(cfg)
    if not r:
        return ""
    asof = r["last_t"].strftime("%m-%d %H:%M")
    stats = f"""
    <div class="stat"><span class="k">Launch px</span><span class="v">{fmt_subscript_price(r['launch_px'])}</span></div>
    <div class="stat"><span class="k">All-time high</span><span class="v">{fmt_subscript_price(r['ath_px'])}</span></div>
    <div class="stat"><span class="k">All-time low</span><span class="v">{fmt_subscript_price(r['atl_px'])}</span></div>
    <div class="stat"><span class="k">Since launch</span><span class="v">{_pct(r['change_pct'])}</span></div>
    <div class="stat"><span class="k">Peak gain</span><span class="v">{_pct(r['peak_gain_pct'])} <small>@ +{metrics.fmt_duration(r['time_to_peak'])}</small></span></div>
    <div class="stat"><span class="k">Max drawdown</span><span class="v">{_pct(r['max_drawdown_pct'])}</span></div>"""
    checks = "".join(
        f"<div class=\"chk\"><span class=\"k\">{lbl}</span><span class=\"v\">{_pct(v)}</span></div>"
        for lbl, v in r["checkpoints"])
    checks_block = (f"<h4>Performance checkpoints</h4><div class=\"checks\">{checks}</div>"
                    if checks else "")
    return (f"<h4>Price reaction <span class=\"asof\">as of {asof} UTC</span></h4>"
            f"<div class=\"stats\">{stats}</div>{checks_block}")


def _detail(cfg: dict) -> str:
    token = cfg["token"]
    fdv = fmt_usd_compact(cfg["fdv_usd"]) if cfg.get("fdv_usd") else "—"
    win = (f"{parse_iso(cfg['window_start_utc']).strftime('%Y-%m-%d %H:%M')} → "
           f"{parse_iso(cfg['window_end_utc']).strftime('%Y-%m-%d %H:%M')} UTC")
    not_listed = ", ".join(cfg.get("not_listed", [])) or "—"
    interactive = chart_html(cfg, announcements=ANN.get(_slug(cfg)))
    if interactive:
        chart_block = interactive
        extra_head = "<script src=\"plotly.min.js\"></script>"
    else:
        chart = _chart_rel(cfg)
        chart_block = (f"<img src=\"{chart}\" alt=\"{token} chart\">"
                       if chart else "<div class=\"missing\">chart not rendered</div>")
        extra_head = ""
    body = f"""
<header><a class="back" href="index.html">← all tokens</a></header>
<main><section class="card">
  <div class="info">
    <h2>{html.escape(cfg.get('name', token))} <span class="sym">{html.escape(token)}</span></h2>
    {f'<div class="cat">{html.escape(cfg["category"])}</div>' if cfg.get('category') else ''}
    {_links(cfg)}
    <dl>
      <dt>Chain</dt><dd>{html.escape(cfg.get('chain', '—'))}</dd>
      <dt>Contract</dt><dd class="mono">{html.escape(cfg.get('token_contract', '—'))}</dd>
      <dt>FDV</dt><dd>{fdv} <span class="src">{html.escape(cfg.get('fdv_source', ''))}</span> {_cmc_tag(cfg)}</dd>
      {_supply_lines(cfg)}
      {_funding_lines(cfg)}
      <dt>Window</dt><dd>{win}</dd>
      <dt>Not listed</dt><dd class="note">{html.escape(not_listed)}</dd>
    </dl>
    {_reaction_block(cfg)}
    <h4>Listing events</h4>
    <table>
      <thead><tr><th>Exchange</th><th>Time (UTC)</th><th>Note</th></tr></thead>
      <tbody>{_events_rows(cfg)}</tbody>
    </table>
    {_annotations(cfg)}
  </div>
  <div class="chart">{chart_block}</div>
</section></main>"""
    return _page(f"{cfg.get('name', token)} ({token})", body, extra_head)


FILTER_SCRIPT = """
<script>
const boxes = [...document.querySelectorAll('.filters input[type=checkbox]')];
const tiles = [...document.querySelectorAll('.tile')];      // thumbnail view
const rows  = [...document.querySelectorAll('.lrow')];      // list view
const items = [...tiles, ...rows];                          // both carry data-venues/search
const count = document.getElementById('count');
const search = document.getElementById('search');
function apply() {
  const sel = boxes.filter(b => b.checked).map(b => b.value);
  const q = search.value.trim().toLowerCase();
  const ok = el => sel.every(s => el.dataset.venues.includes('|' + s + '|'))
                && (!q || el.dataset.search.includes(q));
  for (const el of items) el.style.display = ok(el) ? '' : 'none';
  count.textContent = tiles.filter(ok).length + ' / ' + tiles.length + ' tokens';
}
boxes.forEach(b => b.addEventListener('change', apply));
search.addEventListener('input', apply);
document.getElementById('clear').addEventListener('click', () => {
  boxes.forEach(b => b.checked = false); search.value = ''; apply();
});

// view toggle: thumbnails <-> list. The chosen view is remembered (per report)
// so returning from a token's detail page via the back link lands on the same
// view instead of resetting to thumbnails.
const VIEW_KEY = 'll-view-reactions';
const views = document.getElementById('views');
const bGrid = document.getElementById('view-grid');
const bList = document.getElementById('view-list');
function setView(v, persist) {
  views.className = 'view-' + v;
  bGrid.classList.toggle('active', v === 'grid');
  bList.classList.toggle('active', v === 'list');
  if (persist) { try { localStorage.setItem(VIEW_KEY, v); } catch (e) {} }
}
bGrid.addEventListener('click', () => setView('grid', true));
bList.addEventListener('click', () => setView('list', true));
// restore last-used view on load
let _v0 = 'grid';
try { _v0 = localStorage.getItem(VIEW_KEY) || 'grid'; } catch (e) {}
setView(_v0 === 'list' ? 'list' : 'grid', false);

// sortable list columns (click a header; toggles asc/desc). Highest performer
// rises to the top for any of the 24h/7d/30d/90d columns.
const ltab = document.getElementById('ltab');
if (ltab) {
  const tb = ltab.querySelector('tbody');
  ltab.querySelectorAll('th').forEach((th, i) => {
    let asc = false;  // first click on a column = descending (best on top)
    th.addEventListener('click', () => {
      const rs = [...tb.rows];
      rs.sort((a, b) => {
        const x = a.cells[i].dataset.s, y = b.cells[i].dataset.s;
        const nx = parseFloat(x), ny = parseFloat(y);
        const c = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : ('' + x).localeCompare(y);
        return asc ? c : -c;
      });
      asc = !asc;
      ltab.querySelectorAll('th').forEach(h => h.classList.remove('sorted'));
      th.classList.add('sorted');
      rs.forEach(r => tb.appendChild(r));
    });
  });
}
apply();
</script>"""


def _filter_bar(cfgs: list[dict]) -> str:
    counts = {label: 0 for label, _g, _p in CHIP_SPECS}
    for cfg in cfgs:
        for label in _token_chips(cfg):
            counts[label] += 1
    groups: list[str] = []
    last_group = None
    chips: list[str] = []
    for label, group, _p in CHIP_SPECS:
        if group != last_group:
            if chips:
                groups.append(f"<span class=\"group\">{''.join(chips)}</span>")
                chips = []
            last_group = group
        muted = " muted" if counts[label] == 0 else ""
        chips.append(
            f"<label class=\"chip{muted}\"><input type=\"checkbox\" value=\"{html.escape(label)}\"> "
            f"{html.escape(label)} <span class=\"c\">{counts[label]}</span></label>"
        )
    if chips:
        groups.append(f"<span class=\"group\">{''.join(chips)}</span>")
    return f"""
<div class="filters">
  <input id="search" type="search" placeholder="Search token name or symbol…" autocomplete="off">
  <span class="flabel">Listed on (all checked):</span>
  {''.join(groups)}
  <button id="clear" type="button">clear</button>
  <span class="viewtoggle">
    <button id="view-grid" type="button" class="active">▦ Thumbnails</button>
    <button id="view-list" type="button">☰ List</button>
  </span>
  <span id="count" class="count"></span>
</div>"""


def _news_strip(tracked_syms: set[str]) -> str:
    """Live listing-signal strip sourced from the BWEnews RSS poll.

    Built at build time (no browser fetch -> no CORS issue). Shows the most
    recent venue listing signals; tracked tokens link to their detail page,
    untracked symbols are flagged as candidates not yet in the report.
    """
    if not BWENEWS_SIGNALS.exists():
        return ""
    try:
        data = json.loads(BWENEWS_SIGNALS.read_text(encoding="utf-8"))
    except Exception:
        return ""
    sigs = data.get("signals", [])
    if not sigs:
        return ""
    fetched = (data.get("fetched_utc") or "")[:16].replace("T", " ")
    pills = []
    for s in sigs[:14]:
        sym = s.get("symbol")
        venue = html.escape(s.get("venue", "?"))
        link = s.get("link")
        # only allow http(s) hrefs — html.escape doesn't neutralize a javascript: scheme (audit L-4)
        if link and not str(link).startswith(("http://", "https://")):
            link = None
        if sym and s.get("tracked"):
            label = f'<a href="{sym.lower()}.html">{html.escape(sym)}</a>'
            cls = "sig"
        elif sym:
            label = f'{html.escape(sym)} <span class="new">new</span>'
            cls = "sig untracked"
        else:
            continue
        inner = f'{label} <span class="v">→ {venue}</span>'
        if link:
            inner = f'<a class="src" href="{html.escape(link)}" target="_blank" rel="noopener">{inner}</a>' \
                if not (sym and s.get("tracked")) else \
                f'{inner} <a class="src" href="{html.escape(link)}" target="_blank" rel="noopener">↗</a>'
        pills.append(f'<span class="{cls}">{inner}</span>')
    if not pills:
        return ""
    return (f'<div class="newsbar"><span class="nlabel">📡 Live listing signals'
            f'<small>via BWEnews · {html.escape(fetched)} UTC</small></span>'
            f'<div class="pills">{"".join(pills)}</div></div>')


def _index(cfgs: list[dict]) -> str:
    tiles = "\n".join(_tile(c) for c in cfgs)
    tracked = {c["token"].upper() for c in cfgs}
    body = f"""
<header><h1>Binance Alpha &amp; Perps</h1>
<nav class="topnav"><a class="active" href="index.html">Binance Alpha &amp; Perps ({len(cfgs)})</a><a href="../funnel/report/index.html">CEX → Korea (74)</a><a href="../scams/index.html">Scam Watchlist</a></nav>
<p>{len(cfgs)} tokens · click a token for its info + chart</p></header>
{_news_strip(tracked)}
{_filter_bar(cfgs)}
<div id="views" class="view-grid">
  <main class="grid">{tiles}</main>
  <div class="listwrap">{_list_table(cfgs)}</div>
</div>
{FILTER_SCRIPT}"""
    return _page("Binance Alpha listing reactions", body)


CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
       background: #f4f6f9; color: #1d2733; }
header { padding: 18px 32px; background: #1f4e79; color: #fff; }
header h1 { margin: 0; font-size: 20px; }
header p { margin: 4px 0 0; opacity: .8; font-size: 13px; }
/* live BWEnews listing-signal strip */
.newsbar { display: flex; align-items: center; gap: 12px; padding: 9px 32px;
           background: #102a43; color: #e5edf6; border-bottom: 1px solid #0b1f33;
           overflow-x: auto; white-space: nowrap; font-size: 12.5px; }
.newsbar .nlabel { font-weight: 700; display: inline-flex; flex-direction: column;
                   line-height: 1.2; flex: 0 0 auto; }
.newsbar .nlabel small { font-weight: 400; opacity: .6; font-size: 10.5px; }
.newsbar .pills { display: inline-flex; gap: 8px; }
.newsbar .sig { background: rgba(255,255,255,.07); border: 1px solid rgba(255,255,255,.14);
                border-radius: 999px; padding: 3px 10px; flex: 0 0 auto; }
.newsbar .sig a { color: #7fc4ff; font-weight: 600; }
.newsbar .sig .v { opacity: .7; }
.newsbar .sig.untracked { opacity: .8; }
.newsbar .sig .new { background: #c0392b; color: #fff; border-radius: 4px;
                     font-size: 9.5px; padding: 0 4px; font-weight: 700; text-transform: uppercase; }
.newsbar .sig .src { opacity: .6; }
.topnav { margin: 8px 0 2px; display: flex; gap: 8px; }
.topnav a { font-size: 13px; color: #fff; opacity: .8; padding: 4px 12px; border-radius: 999px;
            border: 1px solid rgba(255,255,255,.35); }
.topnav a:hover { opacity: 1; text-decoration: none; background: rgba(255,255,255,.1); }
.topnav a.active { opacity: 1; background: #fff; color: #1f4e79; border-color: #fff; font-weight: 600; }
a { color: inherit; text-decoration: none; }
.back { color: #fff; font-size: 13px; opacity: .9; }
.back:hover { opacity: 1; text-decoration: underline; }

/* filter bar */
.filters { padding: 14px 32px; background: #fff; border-bottom: 1px solid #e1e7ee;
           display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
.filters #search { font-size: 13px; padding: 6px 12px; border: 1px solid #cdd6e0;
                   border-radius: 999px; outline: none; min-width: 220px; flex: 0 1 260px; }
.filters #search:focus { border-color: #1f4e79; box-shadow: 0 0 0 2px rgba(31,78,121,.12); }
.filters .flabel { font-weight: 600; color: #6b7785; font-size: 13px; }
.filters .group { display: inline-flex; flex-wrap: wrap; gap: 6px; padding: 0 4px;
                  border-left: 1px solid #e1e7ee; }
.filters .group:first-of-type { border-left: 0; }
.filters label { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px;
                 background: #f4f6f9; border: 1px solid #e1e7ee; border-radius: 999px;
                 padding: 4px 11px 4px 9px; cursor: pointer; user-select: none; }
.filters label:has(input:checked) { background: #eaf2fb; border-color: #1f4e79; color: #1f4e79; }
.filters label.muted { opacity: .45; }
.filters label .c { font-size: 11px; color: #8a96a3; font-weight: 600; }
.filters label:has(input:checked) .c { color: #1f4e79; }
.filters input { accent-color: #1f4e79; margin: 0; }
.filters #clear { font-size: 12px; color: #6b7785; background: none; border: 0;
                  cursor: pointer; text-decoration: underline; }
.filters .count { margin-left: auto; font-size: 12px; color: #6b7785; }
.viewtoggle { display: inline-flex; border: 1px solid #cdd6e0; border-radius: 999px; overflow: hidden; }
.viewtoggle button { font-size: 12px; padding: 5px 12px; background: #fff; border: 0; cursor: pointer; color: #6b7785; }
.viewtoggle button.active { background: #1f4e79; color: #fff; font-weight: 600; }

/* view switching */
.view-grid .listwrap { display: none; }
.view-list .grid { display: none; }

/* list view table */
.listwrap { padding: 16px 32px 28px; overflow-x: auto; }
table.list { width: 100%; border-collapse: collapse; background: #fff; font-size: 13px;
             border: 1px solid #e1e7ee; border-radius: 10px; overflow: hidden; table-layout: fixed; }
/* deterministic column widths so the # column can't balloon (auto-layout dumps
   slack into the first columns). 12 cols: #, Token, TGE, Since, 24h, 7d, 30d,
   90d, FDV, MC, OI%, Funding. */
table.list th:nth-child(1) { width: 3%; }
table.list th:nth-child(2) { width: 19%; text-align: left; }
table.list th:nth-child(3) { width: 9%; }
table.list th:nth-child(4), table.list th:nth-child(5), table.list th:nth-child(6),
table.list th:nth-child(7), table.list th:nth-child(8) { width: 6.5%; }
table.list th:nth-child(9), table.list th:nth-child(10) { width: 9%; }
table.list th:nth-child(11) { width: 7%; }
table.list th:nth-child(12) { width: 13%; }
table.list td { overflow: hidden; }
table.list th { position: sticky; top: 0; background: #eef2f6; text-align: right; padding: 8px 10px;
                border-bottom: 2px solid #d6dee6; cursor: pointer; white-space: nowrap; font-size: 12px; }
table.list th:first-child { text-align: left; }
table.list th.sorted { color: #1f4e79; background: #e3edf7; }
table.list tbody { counter-reset: rank; }
table.list tbody tr { counter-increment: rank; }
table.list td.rank::before { content: counter(rank); color: #8a96a3; font-variant-numeric: tabular-nums; }
table.list td.rank { text-align: right; color: #8a96a3; width: 1%; white-space: nowrap; }
table.list td { padding: 7px 10px; border-bottom: 1px solid #eef2f6; }
table.list td.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
table.list td.tge { text-align: left; font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 12.5px; }
table.list tr:hover td { background: #f7faff; }
table.list td.tok a { display: flex; align-items: center; gap: 10px; }
table.list td.tok .thumb { width: 64px; height: 26px; border: 0; background: none; flex: 0 0 auto; }
table.list td.tok .lname { font-weight: 600; }
table.list td.tok .sym { color: #8a96a3; font-weight: 400; font-size: 12px; }
table.list td.tok .newbadge { display: inline-block; margin-left: 6px; padding: 1px 5px;
  font-size: 9px; font-weight: 700; line-height: 1.4; letter-spacing: .04em; color: #fff;
  background: #1a8f4c; border-radius: 4px; vertical-align: middle; }
table.list td .sub { display: block; font-size: 11px; color: #8a96a3; font-weight: 400; }
table.list .pos { color: #1a8f4c; } table.list .neg { color: #c0392b; }

/* funding */
.lead { color: #1f6fb2; font-size: 11px; font-weight: 600; }

/* index grid */
.grid { padding: 24px 32px; display: grid; gap: 18px;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
.tile { background: #fff; border: 1px solid #e1e7ee; border-radius: 10px;
        overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.05);
        transition: transform .12s, box-shadow .12s; display: block; }
.tile:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(31,78,121,.18); }
.thumb { display: block; width: 100%; height: 90px;
         border-bottom: 1px solid #eef2f6; background: #f7faff; }
.spark-fill { fill: rgba(31,111,178,.12); stroke: none; }
.spark-line { fill: none; stroke: #1f6fb2; stroke-width: 1; vector-effect: non-scaling-stroke; }
.spark-alpha { stroke: #c0392b; stroke-width: 1; stroke-dasharray: 2 2;
               vector-effect: non-scaling-stroke; opacity: .65; }
.tile-body { padding: 12px 14px; }
.tile-head { display: flex; align-items: baseline; gap: 8px; }
.tile-head .name { font-size: 16px; font-weight: 700; }
.tile-meta { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 8px;
             font-size: 12px; color: #6b7785; }
.tile-meta b { color: #1f4e79; font-weight: 600; }

/* detail card */
main:not(.grid) { padding: 24px 32px; }
.card { display: grid; grid-template-columns: minmax(320px, 420px) 1fr; gap: 20px;
        background: #fff; border: 1px solid #e1e7ee; border-radius: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,.05); overflow: hidden; }
.info { padding: 20px 22px; min-width: 0; }
.info h2 { margin: 0 0 14px; font-size: 18px; }
.sym { color: #1f4e79; font-weight: 600; font-size: 13px;
       background: #eaf2fb; padding: 2px 8px; border-radius: 6px; }
dl { display: grid; grid-template-columns: 88px 1fr; gap: 4px 12px; margin: 0 0 16px; }
dt { color: #6b7785; font-weight: 600; }
dd { margin: 0; word-break: break-word; }
.mono, .t { font-family: ui-monospace, Menlo, Consolas, monospace; }
.mono { font-size: 12px; }
.src { display: block; color: #8a96a3; font-size: 11px; }
a.src { display: inline; text-decoration: none; }
a.src:hover { text-decoration: underline; color: #1f6fb2; }
.note { color: #6b7785; font-size: 12px; }
h4 { margin: 16px 0 8px; font-size: 13px; text-transform: uppercase;
     letter-spacing: .04em; color: #6b7785; }
table { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 12.5px; }
th { text-align: left; color: #6b7785; border-bottom: 2px solid #e1e7ee; padding: 5px 6px; }
td { border-bottom: 1px solid #eef2f6; padding: 5px 6px; vertical-align: top; }
th, td { overflow-wrap: anywhere; word-break: break-word; }
th:nth-child(1), td:nth-child(1) { width: 24%; }
th:nth-child(2), td:nth-child(2) { width: 26%; }
td.t { color: #1f4e79; word-break: normal; overflow-wrap: normal; }
.approx { color: #8a96a3; font-style: italic; cursor: help; border-bottom: 1px dotted #c5ccd3; }
.ann-link { color: #1f4e79; text-decoration: none; }
.ann-link:hover { text-decoration: underline; }
tr.alpha td { background: #fff7e6; font-weight: 600; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
       margin-right: 6px; vertical-align: middle; }
.cat { display: inline-block; font-size: 12px; color: #1f4e79; background: #eaf2fb;
       border-radius: 6px; padding: 2px 9px; margin: 0 0 10px; font-weight: 600; }
.links { margin: 0 0 14px; font-size: 12.5px; }
.links a { color: #1f6fb2; text-decoration: none; }
.links a:hover { text-decoration: underline; }
.links.project { margin: 0 0 6px; font-weight: 600; }
.links.project a { color: #1f4e79; }
.pos { color: #1a8f4c; font-weight: 600; }
.neg { color: #c0392b; font-weight: 600; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
         gap: 8px; margin-bottom: 8px; }
.stat { background: #f7f9fc; border: 1px solid #e7edf3; border-radius: 8px; padding: 8px 10px; }
.stat .k, .chk .k { display: block; font-size: 10.5px; text-transform: uppercase;
                    letter-spacing: .03em; color: #8a96a3; margin-bottom: 2px; }
.stat .v { font-size: 15px; font-weight: 600; }
.stat .v small { font-size: 11px; font-weight: 400; color: #8a96a3; }
.checks { display: flex; flex-wrap: wrap; gap: 8px; }
.chk { background: #fff; border: 1px solid #e7edf3; border-radius: 8px;
       padding: 6px 12px; text-align: center; }
.chk .v { font-size: 14px; }
h4 .asof { text-transform: none; letter-spacing: 0; font-weight: 400;
           color: #aab3bd; font-size: 11px; }
.ann { margin-top: 14px; background: #f7f9fc; border: 1px solid #e7edf3;
       border-radius: 8px; padding: 8px 14px; }
.ann ul { margin: 6px 0 4px; padding-left: 18px; }
.chart { padding: 14px; display: flex; align-items: flex-start; justify-content: center;
         background: #fafbfc; }
.chart img { width: 100%; height: auto; border-radius: 6px; }
.chart > .plotly-graph-div, .chart > div { width: 100%; }
.missing { color: #b00; font-style: italic; }

/* ============================================================
   Responsive layer — fluid grids, flexible media, breakpoints
   ============================================================ */
/* flexible media: never let an image/chart/svg force horizontal overflow */
img, svg, canvas, video { max-width: 100%; height: auto; }
.thumb { height: auto; }              /* sparklines keep their viewBox aspect */
/* grid/flex children default to min-width:auto and refuse to shrink below their
   content — the #1 cause of charts/tables overflowing (and being clipped by an
   overflow:hidden card). Let them shrink. */
.card > *, .chart, .stats > *, .grid > * { min-width: 0; }
.chart > .plotly-graph-div, .chart > div { width: 100% !important; max-width: 100%; }

@media (max-width: 1024px) {
  header, .newsbar, .filters, .grid, .listwrap, main:not(.grid) {
    padding-left: 20px; padding-right: 20px; }
}
@media (max-width: 900px) {
  .card { grid-template-columns: 1fr; }   /* info + chart stack */
}
@media (max-width: 640px) {
  header, .newsbar, .filters, .grid, .listwrap, main:not(.grid) {
    padding-left: 14px; padding-right: 14px; }
  header { padding-top: 14px; padding-bottom: 14px; }
  header h1 { font-size: 18px; }
  .filters { gap: 8px 10px; }
  .filters #search { flex: 1 1 100%; min-width: 0; }   /* full-width search */
  .filters .count { margin-left: 0; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
  .info { padding: 16px; }
  dl { grid-template-columns: 80px 1fr; }
  /* the dense 12-col list scrolls (readable) instead of crushing to 0-width cols */
  table.list { min-width: 760px; }
}
@media (max-width: 380px) {
  .grid { grid-template-columns: 1fr; }
}
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if SOCIALS_CACHE.exists():
        SOCIALS.update(json.loads(SOCIALS_CACHE.read_text(encoding="utf-8")))
    if ANN_CACHE.exists():
        ANN.update(json.loads(ANN_CACHE.read_text(encoding="utf-8")))
    if OI_CACHE.exists():
        OI.update(json.loads(OI_CACHE.read_text(encoding="utf-8")).get("tokens", {}))
    if FUNDING_CACHE.exists():
        FUNDING.update(json.loads(FUNDING_CACHE.read_text(encoding="utf-8")))
    cfgs = [json.loads(p.read_text(encoding="utf-8")) for p in LISTINGS.glob("*.json")]
    cfgs.sort(key=_alpha_time, reverse=True)
    from plotly.offline import get_plotlyjs
    (OUT_DIR / "plotly.min.js").write_text(get_plotlyjs(), encoding="utf-8")
    (OUT_DIR / "style.css").write_text(CSS, encoding="utf-8")
    (OUT_DIR / "index.html").write_text(_index(cfgs), encoding="utf-8")
    for cfg in cfgs:
        (OUT_DIR / f"{_slug(cfg)}.html").write_text(_detail(cfg), encoding="utf-8")
    print(f"wrote {OUT_DIR / 'index.html'}  ({len(cfgs)} tokens + detail pages)")


if __name__ == "__main__":
    main()

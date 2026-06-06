"""Per-token TOKEN-UNLOCKS panel for the detail pages (both reports).

Reads `cache/token_unlocks.json` (built by `fetch_token_unlocks.py`) and returns
an HTML section, or "" when there's no unlock schedule for the token. Server-side
rendered (static site, no client API calls).

Coverage note: the only free source is DefiLlama's open dataset bucket
(`https://defillama-datasets.llama.fi/emissions/<slug>`), which covers ~hundreds
of mostly-major protocols — most small watchlist tokens will have NO data, so an
empty return is the common, expected case. Render a "no schedule" state only if
you want; "" (panel hidden) is fine.

Cache schema (keyed by UPPER symbol) — suggested, the agent owns the exact shape:
    { "AAVE": {
        "next_unlock": {"ts": int, "tokens": float, "usd": float, "pct_supply": float, "label": str},
        "events": [ {"ts": int, "tokens": float, "usd": float, "pct_supply": float, "label": str}, ... ],
        "source_url": "https://defillama.com/unlocks/aave"
      }, ... }

Render contract (called by build_scams.py + build_listing_report.py):
    render(ident: dict) -> str
  ident = {"symbol","name","cmc_slug","cg_id"} — "symbol" required; "cg_id"
  (CoinGecko id) is the most reliable key for matching DefiLlama's `gecko_id`.

Markup/CSS contract (the matching CSS already lives in build_listing_report.py's
CSS string, reused by build_scams.py as RCSS — so BOTH reports are styled. Use
exactly these classes; do NOT add a <style> block):
    <section class="extra-card unlockcard">
      <h3>Token unlocks <span class="asof">via DefiLlama</span></h3>
      <div class="unlock-next">Next unlock: <b>12 Jul 2026</b> · 1.2% of supply (~$3.4M)</div>
      <ul class="unlocklist">
        <li><span class="ud">12 Jul 2026</span><span class="ut">Team · 1.2% (~$3.4M)</span></li>
        …
      </ul>
    </section>
Empty/missing -> return "" (the common case for small tokens — panel hidden).
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(__file__).parent.parent / "cache" / "token_unlocks.json"

# Load once at import; guard a missing/broken cache (panel hidden everywhere).
try:
    _DATA = json.loads(CACHE.read_text(encoding="utf-8"))
    if not isinstance(_DATA, dict):
        _DATA = {}
except Exception:
    _DATA = {}


def _fmt_date(ts) -> str:
    """1780617600 -> '12 Jul 2026' (UTC)."""
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%d %b %Y")
    except Exception:
        return ""


def _fmt_usd(usd) -> str:
    """3400000 -> '$3.4M' (compact). Returns '' for null/zero."""
    try:
        v = float(usd)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    a = abs(v)
    if a >= 1e9:
        s = f"${v / 1e9:.1f}B"
    elif a >= 1e6:
        s = f"${v / 1e6:.1f}M"
    elif a >= 1e3:
        s = f"${v / 1e3:.1f}K"
    else:
        s = f"${v:.0f}"
    return s


def _fmt_pct(pct) -> str:
    """1.2 -> '1.2%'. Returns '' for null."""
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    return f"{v:.1f}%" if v >= 0.1 else f"{v:.2f}%"


def _head(source: str = "DefiLlama") -> str:
    return ('<section class="extra-card unlockcard">'
            f'<h3>Token unlocks <span class="asof">via {html.escape(source)}</span></h3>')


def _empty() -> str:
    """The panel is ALWAYS shown — when we have no schedule for the token, say so
    explicitly rather than hiding it, so an unlock is never silently missed."""
    return _head() + '<p class="empty">No unlock data available at the moment.</p></section>'


def render(ident: dict) -> str:
    sym = (ident or {}).get("symbol")
    if not sym:
        return _empty()
    rec = _DATA.get(str(sym).strip().upper())
    if not rec:
        return _empty()
    events = rec.get("events") or []
    if not events:
        return _empty()

    nxt = rec.get("next_unlock") or events[0]

    # --- "Next unlock" headline line ---
    parts = [f"Next unlock: <b>{html.escape(_fmt_date(nxt.get('ts')))}</b>"]
    pct = _fmt_pct(nxt.get("pct_supply"))
    if pct:
        parts.append(f"{pct} of supply")
    usd = _fmt_usd(nxt.get("usd"))
    if usd:
        parts.append(f"(~{usd})")
    next_line = " · ".join(parts[:2]) + ((" " + parts[2]) if len(parts) > 2 else "")

    # --- upcoming events list ---
    rows = []
    for e in events:
        date = _fmt_date(e.get("ts"))
        if not date:
            continue
        bits = []
        label = e.get("label")
        if label:
            bits.append(html.escape(str(label)))
        p = _fmt_pct(e.get("pct_supply"))
        u = _fmt_usd(e.get("usd"))
        tail = p
        if u:
            tail = (p + f" (~{u})") if p else f"~{u}"
        if tail:
            bits.append(tail)
        ut = " · ".join(bits)
        rows.append(
            f'<li><span class="ud">{html.escape(date)}</span>'
            f'<span class="ut">{ut}</span></li>'
        )

    if not rows:
        return _empty()

    return (
        _head(rec.get("source") or "DefiLlama")
        + f'<div class="unlock-next">{next_line}</div>'
        '<ul class="unlocklist">' + "".join(rows) + "</ul>"
        "</section>"
    )

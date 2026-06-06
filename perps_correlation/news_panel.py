"""Per-token NEWS panel for the detail pages (both reports).

Reads `cache/token_news.json` (built by `fetch_token_news.py`) and returns an
HTML section, or "" when there's no news for the token. Server-side rendered so
the static site needs no client-side API calls (CSP blocks those anyway).

Cache schema (keyed by UPPER symbol):
    { "AAVE": [ {"title": str, "url": str, "source": str, "ts": int}, ... ], ... }
  • newest item first
  • ts = unix seconds (UTC)

Render contract (called by build_scams.py + build_listing_report.py):
    render(ident: dict) -> str
  ident = {"symbol","name","cmc_slug","cg_id"} — only "symbol" is required.

Markup/CSS contract (the matching CSS already lives in build_listing_report.py's
CSS string, which build_scams.py reuses as RCSS — so BOTH reports are styled.
Use exactly these classes; do NOT add a <style> block):
    <section class="extra-card newscard">
      <h3>News <span class="asof">via CoinMarketCap</span></h3>
      <ul class="newsfeed">
        <li><a href="…" target="_blank" rel="noopener">Headline</a>
            <span class="src">SourceName · 3d ago</span></li>
        …
      </ul>
    </section>
The `.newsfeed` is a fixed-height vertical scroll box: newest first, scroll down
for older (that is the whole point of the feature). Use html.escape on titles/
source names. Return "" when the token has no news (panel hidden); if you prefer
a visible empty state, render <p class="empty">No recent news.</p> inside the
section — but "" is fine and is the default for tokens with no coverage.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

CACHE = Path(__file__).parent.parent / "cache" / "token_news.json"

MAX_ITEMS = 30


def _load() -> dict:
    """Load the symbol-keyed news cache once at import. {} if missing/bad."""
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_NEWS = _load()


def _rel_age(ts: int, now: int | None = None) -> str:
    """Compact relative age: 'Nm ago' / 'Nh ago' / 'Nd ago' / 'Nw ago' / 'Nmo ago'."""
    if not ts:
        return ""
    if now is None:
        now = int(time.time())
    secs = max(now - int(ts), 0)
    mins = secs // 60
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago"
    days = hrs // 24
    if days < 7:
        return f"{days}d ago"
    weeks = days // 7
    if days < 30:
        return f"{weeks}w ago"
    months = days // 30
    return f"{months}mo ago"


def render(ident: dict) -> str:
    sym = (ident.get("symbol") or "").strip().upper()
    if not sym:
        return ""
    items = _NEWS.get(sym)
    if not isinstance(items, list) or not items:
        return ""

    now = int(time.time())
    rows: list[str] = []
    for it in items[:MAX_ITEMS]:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title or not url:
            continue
        source = (it.get("source") or "").strip()
        age = _rel_age(it.get("ts") or 0, now)
        meta = " · ".join(p for p in (html.escape(source), age) if p)
        rows.append(
            f'<li><a href="{html.escape(url)}" target="_blank" rel="noopener">'
            f"{html.escape(title)}</a>"
            f'<span class="src">{meta}</span></li>'
        )

    if not rows:
        return ""

    return (
        '<section class="extra-card newscard">'
        '<h3>News <span class="asof">via CoinMarketCap</span></h3>'
        '<ul class="newsfeed">'
        + "".join(rows)
        + "</ul></section>"
    )

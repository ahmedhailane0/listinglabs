"""Canonical venue -> color map, shared site-wide so a given exchange/market is
always the same color on every chart, thumbnail, and events table. Hues are
spread so commonly co-occurring venues stay visually distinct.
"""
from __future__ import annotations

import hashlib

VENUE_COLORS = {
    "Binance Alpha":   "#e8b400",  # signature gold (on every chart)
    "Binance Spot":    "#b8860b",
    "Binance Perp":    "#8c6d1f",  # olive-gold
    "Coinbase Spot":   "#1652f0",  # blue
    "Coinbase INTX":   "#6fa8ff",
    "OKX Spot":        "#222222",
    "OKX Perp":        "#777777",
    "Bybit Spot":      "#ff7f0e",  # orange
    "Bybit Futures":   "#c65a00",  # burnt orange
    "Kraken Spot":     "#7e57c2",  # purple
    "Kraken Futures":  "#b39ddb",
    "KuCoin Spot":     "#2ca02c",  # green
    "KuCoin Futures":  "#1a7a1a",
    "Bitget Spot":     "#17becf",  # cyan
    "Bitget Perp":     "#0e7c8a",
    "Gate.io Spot":    "#e84142",  # red
    "Gate.io Perp":    "#a0282a",
    "Upbit":           "#00a1e9",  # sky blue
    "Bithumb":         "#f4623a",  # coral
    "Coinone":         "#00b386",
}
_FALLBACK = ["#9467bd", "#8c564b", "#e377c2", "#bcbd22", "#aec7e8",
             "#ffbb78", "#98df8a", "#c49c94", "#f7b6d2", "#dbdb8d"]


def venue_color(name: str) -> str:
    if name in VENUE_COLORS:
        return VENUE_COLORS[name]
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return _FALLBACK[h % len(_FALLBACK)]


# Trade-page URL templates per venue. `{S}` = uppercase base symbol (e.g. AERO),
# `{s}` = lowercase. Korean venues quote KRW; everyone else USDT/USD.
_VENUE_URL = {
    "Binance Spot":   "https://www.binance.com/en/trade/{S}_USDT",
    "Binance Perp":   "https://www.binance.com/en/futures/{S}USDT",
    "Coinbase Spot":  "https://www.coinbase.com/advanced-trade/spot/{S}-USD",
    "Coinbase INTX":  "https://www.coinbase.com/advanced-trade/perpetuals/{S}-PERP",
    "OKX Spot":       "https://www.okx.com/trade-spot/{s}-usdt",
    "OKX Perp":       "https://www.okx.com/trade-swap/{s}-usdt-swap",
    "Bybit Spot":     "https://www.bybit.com/en/trade/spot/{S}/USDT",
    "Bybit Futures":  "https://www.bybit.com/trade/usdt/{S}USDT",
    "Bybit Perp":     "https://www.bybit.com/trade/usdt/{S}USDT",
    "Kraken Spot":    "https://pro.kraken.com/app/trade/{s}-usd",
    "Kraken Futures": "https://futures.kraken.com/trade/futures/PF_{S}USD",
    "KuCoin Spot":    "https://www.kucoin.com/trade/{S}-USDT",
    "KuCoin Futures": "https://www.kucoin.com/futures/trade/{S}USDTM",
    "Bitget Spot":    "https://www.bitget.com/spot/{S}USDT",
    "Bitget Perp":    "https://www.bitget.com/futures/usdt/{S}USDT",
    "Gate.io Spot":   "https://www.gate.io/trade/{S}_USDT",
    "Gate.io Perp":   "https://www.gate.io/futures/USDT/{S}_USDT",
    "Upbit":          "https://upbit.com/exchange?code=CRIX.UPBIT.KRW-{S}",
    "Bithumb":        "https://www.bithumb.com/react/trade/order/{S}-KRW",
    "Coinone":        "https://coinone.co.kr/exchange/trade/{s}/krw",
}

# Binance Alpha token pages are keyed by chain + contract, not symbol.
_ALPHA_NET = {"bsc": "bsc", "base": "base", "ethereum": "eth", "eth": "eth",
              "solana": "sol", "arbitrum": "arbitrum", "linea": "linea"}


def venue_url(exchange: str, symbol: str, chain: str = "", contract: str = "") -> str | None:
    """Best-effort trade/listing page for an event's venue, or None."""
    sym = symbol.upper()
    if exchange == "Binance Alpha":
        net = _ALPHA_NET.get((chain or "").lower())
        if net and contract:
            return f"https://www.binance.com/en/alpha/{net}/{contract}"
        return f"https://www.binance.com/en/trade/{sym}_USDT"
    tmpl = _VENUE_URL.get(exchange)
    return tmpl.format(S=sym, s=sym.lower()) if tmpl else None


import urllib.parse

# Exchange FAMILY (prefix match on the event's exchange name) -> the site(s)
# its listing announcements live on. Native exchange "search" pages mostly don't
# deep-link reliably (SPA routing / bot walls), so the robust fallback is a
# site:-scoped web search: the exact announcement is the top hit and it never
# 404s. `announcement_exact()` overrides this with a direct article URL when one
# was resolved from the venue's API (see fetch_announcements.py).
_ANNOUNCE_SITE = [
    ("Binance",  "binance.com/en/support/announcement"),
    ("Coinbase", "x.com/CoinbaseAssets"),
    ("OKX",      "okx.com/help"),
    ("Bybit",    "announcements.bybit.com"),
    ("Kraken",   "blog.kraken.com"),
    ("KuCoin",   "kucoin.com/announcement"),
    ("Bitget",   "bitget.com"),
    ("Gate",     "gate.com/announcements"),
    ("Upbit",    "upbit.com/service_center"),
    ("Bithumb",  "bithumb.com"),
    ("Coinone",  "coinone.co.kr"),
]


def announcement_url(exchange: str, symbol: str, name: str = "") -> str | None:
    """Scoped web search that lands on this token's listing announcement for the
    venue. Works for every exchange (no bot-blocking, no SPA dead-ends)."""
    site = next((s for p, s in _ANNOUNCE_SITE if exchange.startswith(p)), None)
    if not site:
        return None
    terms = [f"site:{site}", symbol.upper()]
    if name and name.upper() != symbol.upper():
        terms.append(f'"{name}"')
    terms.append("listed")
    q = urllib.parse.quote_plus(" ".join(terms))
    return f"https://duckduckgo.com/?q={q}"

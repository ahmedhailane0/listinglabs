"""Repair price-candle caches that got contaminated with another token's data.

Some tokens' GeckoTerminal pools start returning a *different* price band partway
through (a reused/low-liquidity pool, a flipped base/quote, or a wholly different
token), which the refresh then merged in. The result is a chart that's flat then
jumps to a wrong, persistent level — e.g. LINEA pinned at ~$1.00 from 2026-05-06
while its real price is ~$0.0026. (User report: "it spikes for no reason / looks
like another token's chart.")

This finds the contamination and repairs it, using CoinMarketCap's current price as
ground truth:

  1. Detect the contamination jump: a >5x single-candle move, occurring AFTER the
     launch window, into a band whose median diverges from the CMC price by >3x.
  2. Truncate the cache at that jump (the launch + early real history is kept).
  3. Refill from the launch-window start (well, from the truncation point) to now
     using Binance spot klines (authoritative, keyless) when the token trades there,
     so the chart stays current with correct prices. Tokens with no Binance pair are
     left truncated (correct-but-shorter beats wrong).

    python clean_klines.py                 # scan all tokens, repair the contaminated
    python clean_klines.py linea zora      # only these
    python clean_klines.py --dry-run       # report, don't write

Run `python build_listing_report.py` afterwards to rebuild the charts.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from refresh_klines import _fetch_binance_spot, _merge, _cache_path

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
LISTINGS = HERE / "listings"
MARKET = CACHE / "token_market.json"

JUMP = 5.0            # single-candle ratio that flags a possible contamination edge
BAND = 3.0           # tail median must be within this factor of CMC price to be "real"
GRACE_DAYS = 14      # ignore jumps inside the launch window (real launch pumps)


def _truth_price() -> dict:
    if not MARKET.exists():
        return {}
    toks = json.loads(MARKET.read_text(encoding="utf-8")).get("tokens", {})
    return {k: v.get("price") for k, v in toks.items() if v.get("price")}


def _find_contamination(rows: list, p_truth: float) -> int | None:
    """Index of the first candle of the contaminated tail, or None if clean."""
    if not rows or not p_truth:
        return None
    t_lo = rows[0][0]
    grace_ms = GRACE_DAYS * 86400 * 1000
    for j in range(1, len(rows)):
        if rows[j][0] < t_lo + grace_ms:
            continue
        prev, cur = rows[j - 1][4], rows[j][4]
        if prev <= 0 or cur <= 0:
            continue
        ratio = cur / prev
        if ratio > JUMP or ratio < 1 / JUMP:
            tail = [r[4] for r in rows[j:] if r[4] > 0]
            if not tail:
                continue
            med = median(tail)
            if med / p_truth > BAND or p_truth / med > BAND:
                return j
    return None


def clean_one(token: str, truth: dict, dry: bool) -> str:
    path = _cache_path(token)
    if not path.exists():
        return f"{token}: no cache"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    p = truth.get(token.upper())
    if not p:
        return f"{token}: no CMC truth price — skipped"
    j = _find_contamination(rows, p)
    if j is None:
        return f"{token}: clean (last={rows[-1][4]:.6g}, CMC={p:.6g})"

    bad_from = datetime.fromtimestamp(rows[j][0] / 1000, tz=timezone.utc)
    kept = rows[:j]
    # Refill the dropped span from Binance spot (correct, keyless) if available.
    start = datetime.fromtimestamp(kept[-1][0] / 1000, tz=timezone.utc) if kept \
        else datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    bn = _fetch_binance_spot(token, start, now)
    # Only accept Binance candles that agree with the CMC truth (guards against a
    # same-named-but-different Binance token, e.g. a symbol collision).
    if bn:
        bn_last = bn[-1][4]
        if not (bn_last and (bn_last / p < BAND and p / bn_last < BAND)):
            bn = []
    merged = _merge(kept, bn) if bn else kept
    refill = f" + {len(bn)} Binance-spot" if bn else " (no Binance pair — truncated)"
    msg = (f"{token}: CONTAMINATED from {bad_from:%Y-%m-%d} "
           f"(band~{median([r[4] for r in rows[j:]]):.6g} vs CMC {p:.6g}); "
           f"kept {len(kept)} -> {len(merged)} candles{refill}")
    if not dry:
        label = (data.get("source", "")) + " [cleaned]"
        path.write_text(json.dumps({"source": label, "rows": merged}, ensure_ascii=False),
                        encoding="utf-8")
    return msg


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    only = [a.lower() for a in argv if not a.startswith("--")]
    truth = _truth_price()
    if not truth:
        print("token_market.json missing — run fetch_token_market.py first")
        return 1
    tokens = only or sorted(p.stem for p in LISTINGS.glob("*.json"))
    repaired = 0
    for t in tokens:
        msg = clean_one(t, truth, dry)
        print(f"  {msg}")
        if "CONTAMINATED" in msg:
            repaired += 1
    print(f"\n{'(dry-run) ' if dry else ''}repaired {repaired} contaminated cache(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

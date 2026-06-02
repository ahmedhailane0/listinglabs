"""Keep every token's price candles current so the charts update "forever".

For each `listings/<token>.json`, re-pull 5m OHLCV from its GeckoTerminal Alpha
pool up to *now* and MERGE it into the existing cache (union by timestamp). The
merge matters: GeckoTerminal's free endpoint only pages back ~12k candles
(~41 days of 5m), so for older tokens a from-now fetch would not reach the launch
window — keeping the already-cached launch candles preserves the listing reaction
(the whole point of the report) while new candles accumulate on the right.

The interactive chart still DEFAULTS its view to the launch window
(interactive_chart.py pins xaxis.range); the extended history is revealed by
panning/zooming out.

Run:
    python refresh_klines.py            # all tokens
    python refresh_klines.py slx nex    # just these

Designed to be safe in CI: any single token failing is logged and skipped; the
script never raises, so a flaky pool can't break the scheduled build.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from listing_chart import fetch_geckoterminal, parse_iso

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
LISTINGS = HERE / "listings"


def _cache_path(token: str) -> Path:
    return CACHE / f"{token.lower()}_klines_5m_alpha.json"


def _merge(old: list, new: list) -> list:
    """Union two [ts, o, h, l, c] lists by timestamp; newer data wins on overlap."""
    by_ts = {r[0]: r for r in old}
    for r in new:
        by_ts[r[0]] = r          # fresh candle overwrites a stale one at the same ts
    return [by_ts[k] for k in sorted(by_ts)]


def refresh_one(cfg: dict) -> str:
    token = cfg["token"]
    pool = cfg.get("gecko_pool")
    chain = cfg.get("chain")
    if not pool or not chain:
        return f"{token}: no pool/chain — skipped"

    w_start = parse_iso(cfg["window_start_utc"])
    now = datetime.now(timezone.utc)
    path = _cache_path(token)

    old = []
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
        except Exception:
            old = []

    try:
        fresh = fetch_geckoterminal(chain, pool, w_start, now)
    except SystemExit as e:           # fetch_geckoterminal raises SystemExit on empty
        return f"{token}: fetch failed ({e}) — kept {len(old)} cached"
    except Exception as e:
        return f"{token}: error {type(e).__name__}: {e} — kept {len(old)} cached"

    merged = _merge(old, fresh)
    if not merged:
        return f"{token}: no candles — skipped"

    label = f"Binance Alpha ({chain}, on-chain)"
    path.write_text(json.dumps({"source": label, "rows": merged}, ensure_ascii=False),
                    encoding="utf-8")
    last = datetime.fromtimestamp(merged[-1][0] / 1000, tz=timezone.utc)
    lag_h = (now - last).total_seconds() / 3600
    return (f"{token}: {len(old)}->{len(merged)} candles "
            f"(+{len(merged) - len(old)}); latest {lag_h:.1f}h old")


def main(argv: list[str]) -> int:
    if argv:
        cfgs = [LISTINGS / f"{t.lower()}.json" for t in argv]
    else:
        cfgs = sorted(LISTINGS.glob("*.json"))
    ok = 0
    for p in cfgs:
        if not p.exists():
            print(f"  {p.name}: not found")
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  {p.name}: bad JSON ({e})")
            continue
        msg = refresh_one(cfg)
        print(f"  {msg}", flush=True)
        ok += 1
    print(f"refresh_klines: processed {ok} token(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

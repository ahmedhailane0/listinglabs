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
import os
import sys
from datetime import datetime, timedelta, timezone
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


def _fetch_binance_spot(symbol: str, start: datetime, end: datetime) -> list:
    """Fallback OHLC for tokens whose on-chain GeckoTerminal pool is gone (audit L-1):
    fetch 5m spot klines for <SYMBOL>USDT from Binance. Returns [] if the pair doesn't
    exist there. Free, keyless, deep history."""
    import requests
    url = "https://api.binance.com/api/v3/klines"
    pair = f"{symbol.upper()}USDT"
    out, cur, end_ms = {}, int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    for _ in range(160):  # Binance isn't IP-throttled; allow deep backfill (~1.5y of 5m)
        try:
            r = requests.get(url, params={"symbol": pair, "interval": "5m",
                                          "startTime": cur, "limit": 1000}, timeout=20)
        except Exception:
            break
        if r.status_code != 200:
            break  # bad symbol / not listed on Binance spot
        rows = r.json()
        if not rows:
            break
        for k in rows:
            ts = int(k[0])
            if ts > end_ms:
                break
            out[ts] = [ts, float(k[1]), float(k[2]), float(k[3]), float(k[4])]
        nxt = int(rows[-1][0]) + 1
        if nxt <= cur or len(rows) < 1000:
            break
        cur = nxt
    return [out[k] for k in sorted(out)]


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

    # INCREMENTAL: only pull candles since the last cached one (with a 2h overlap so
    # we never miss any around the boundary). This is the difference between ~1 page
    # and ~12 pages per token — critical because GeckoTerminal rate-limits (429) the
    # shared CI IP, and every extra page stacks multi-second backoffs. First-ever pull
    # for a token (no cache) still fetches the full launch window -> now.
    if old:
        last_dt = datetime.fromtimestamp(old[-1][0] / 1000, tz=timezone.utc)
        fetch_start = max(w_start, last_dt - timedelta(hours=2))
    else:
        fetch_start = w_start

    src_label = f"Binance Alpha ({chain}, on-chain)"
    fresh, gt_failed = [], False
    try:
        fresh = fetch_geckoterminal(chain, pool, fetch_start, now)
    except SystemExit:                # fetch_geckoterminal raises SystemExit on empty
        gt_failed = True
    except Exception:
        gt_failed = True

    # Fallback: on-chain pool gone/empty (audit L-1) -> try Binance spot so the chart
    # still tracks current price. Merged by timestamp with the on-chain launch history.
    fb = ""
    if not fresh:
        # Cap the fallback backfill to recent history so caches stay bounded (the
        # on-chain launch candles are already preserved in `old`; we only need to keep
        # the chart's current tail alive). ~120d of 5m ≈ 35k candles.
        bn_start = max(fetch_start, now - timedelta(days=120))
        bn = _fetch_binance_spot(token, bn_start, now)
        if bn:
            fresh = bn
            fb = " [binance-spot fallback]"
        elif gt_failed:
            return f"{token}: on-chain gone + no Binance spot — kept {len(old)} cached"

    merged = _merge(old, fresh)
    if not merged:
        return f"{token}: no candles — skipped"

    label = src_label + (" + binance-spot" if fb else "")
    path.write_text(json.dumps({"source": label, "rows": merged}, ensure_ascii=False),
                    encoding="utf-8")
    last = datetime.fromtimestamp(merged[-1][0] / 1000, tz=timezone.utc)
    lag_h = (now - last).total_seconds() / 3600
    return (f"{token}: {len(old)}->{len(merged)} candles "
            f"(+{len(merged) - len(old)}); latest {lag_h:.1f}h old{fb}")


def _last_candle_ms(token: str) -> int:
    """Timestamp of the newest cached candle (0 if none) — the staleness key."""
    p = _cache_path(token)
    if not p.exists():
        return 0
    try:
        rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
        return rows[-1][0] if rows else 0
    except Exception:
        return 0


def _process(p: Path) -> str:
    if not p.exists():
        return f"{p.name}: not found"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return f"{p.name}: bad JSON ({e})"
    return refresh_one(cfg)


def main(argv: list[str]) -> int:
    # Pull "--limit N" / "--workers N" out of argv; the rest are token names.
    limit = int(os.environ.get("REFRESH_LIMIT") or 0)
    workers = int(os.environ.get("REFRESH_WORKERS") or 6)
    tokens = []
    it = iter(argv)
    for a in it:
        if a == "--limit":
            limit = int(next(it, "0"))
        elif a == "--workers":
            workers = int(next(it, "6"))
        else:
            tokens.append(a)

    if tokens:
        cfgs = [LISTINGS / f"{t.lower()}.json" for t in tokens]
    else:
        cfgs = sorted(LISTINGS.glob("*.json"))
        # Most-stale first: tokens whose newest cached candle is oldest (incl. those
        # still at only their launch window, or with no cache) go first. With a cap,
        # this makes successive runs round-robin fairly across all tokens.
        cfgs.sort(key=lambda p: _last_candle_ms(p.stem))

    # A cap keeps a rate-limited CI run bounded; 0/unset = all (use locally to seed).
    if limit > 0:
        cfgs = cfgs[:limit]
        print(f"refresh_klines: limited to {limit} most-stale token(s)")

    # Parallel across tokens: each hits a DIFFERENT GeckoTerminal pool endpoint and
    # writes its OWN cache file, so they're independent (no shared state, no write
    # races). Threads overlap the network/sleep waits. Note: GeckoTerminal still
    # rate-limits per IP, so keep `workers` modest in CI (the per-page 429 backoff in
    # fetch_geckoterminal absorbs bursts); locally the IP isn't throttled so more
    # workers = much faster seeding.
    workers = max(1, min(workers, len(cfgs) or 1))
    ok = 0
    print(f"refresh_klines: {len(cfgs)} token(s), {workers} worker(s)", flush=True)
    if workers == 1:
        for p in cfgs:
            print(f"  {_process(p)}", flush=True)
            ok += 1
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_process, p): p for p in cfgs}
            for fut in as_completed(futs):
                try:
                    print(f"  {fut.result()}", flush=True)
                except Exception as e:
                    print(f"  {futs[fut].name}: worker error {e}", flush=True)
                ok += 1
    print(f"refresh_klines: processed {ok} token(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

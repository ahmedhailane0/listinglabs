"""Stage 2: resolve per-venue listing dates for the funnel survivors and compute
lag columns. Dates are earliest-candle proxies (the listing day = first day the
market has price data). Cached to date_cache.json so reruns are cheap.

Outputs funnel_dated.json: each survivor + coinbase/upbit/bithumb/coinone dates
+ first_korean + lag-in-days columns.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/funnel-dates"}
HERE = Path(__file__).parent
CACHE_PATH = HERE / "date_cache.json"
CACHE: dict[str, str | None] = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}


def _save():
    CACHE_PATH.write_text(json.dumps(CACHE, indent=2), encoding="utf-8")


def get(url, **kw):
    return requests.get(url, headers=UA, timeout=25, **kw)


def _cache(key, fn):
    if key in CACHE:
        return CACHE[key]
    try:
        v = fn()
    except Exception:
        v = None
    CACHE[key] = v
    _save()
    return v


def coinbase_date(sym: str) -> str | None:
    def fn():
        for quote in ("USD", "USDC"):
            pid = f"{sym}-{quote}"
            earliest = None
            end = datetime.now(timezone.utc)
            for _ in range(12):
                start = end - timedelta(days=300)
                r = get(f"https://api.exchange.coinbase.com/products/{pid}/candles",
                        params={"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()})
                if r.status_code != 200:
                    break
                c = r.json() or []
                if not c:
                    break
                oldest = min(x[0] for x in c)
                od = datetime.fromtimestamp(oldest, tz=timezone.utc)
                earliest = od if earliest is None or od < earliest else earliest
                if od > start + timedelta(days=1):
                    break
                end = start
                time.sleep(0.12)
            if earliest:
                return earliest.date().isoformat()
        return None
    return _cache(f"cb:{sym}", fn)


def upbit_date(sym: str) -> str | None:
    def fn():
        for quote in ("KRW", "USDT"):
            market = f"{quote}-{sym}"
            to = None
            earliest = None
            for _ in range(15):
                params = {"market": market, "count": 200}
                if to:
                    params["to"] = to
                r = get("https://api.upbit.com/v1/candles/days", params=params)
                if r.status_code != 200:
                    break
                d = r.json() or []
                if not isinstance(d, list) or not d:
                    break
                times = [x["candle_date_time_utc"] for x in d]
                oldest = min(times)
                earliest = oldest if earliest is None or oldest < earliest else earliest
                if len(d) < 200:
                    break
                to = oldest.replace("T", " ")
                time.sleep(0.12)
            if earliest:
                return earliest[:10]
        return None
    return _cache(f"up:{sym}", fn)


def bithumb_date(sym: str) -> str | None:
    def fn():
        r = get(f"https://api.bithumb.com/public/candlestick/{sym}_KRW/24h")
        if r.status_code != 200:
            return None
        d = r.json().get("data") or []
        if not d:
            return None
        oldest_ms = min(int(row[0]) for row in d)
        return datetime.fromtimestamp(oldest_ms / 1000, tz=timezone.utc).date().isoformat()
    return _cache(f"bt:{sym}", fn)


def coinone_date(sym: str) -> str | None:
    def fn():
        r = get(f"https://api.coinone.co.kr/public/v2/chart/KRW/{sym}", params={"interval": "1d"})
        if r.status_code != 200:
            return None
        chart = r.json().get("chart") or []
        if not chart:
            return None
        oldest_ms = min(int(x["timestamp"]) for x in chart)
        return datetime.fromtimestamp(oldest_ms / 1000, tz=timezone.utc).date().isoformat()
    return _cache(f"co:{sym}", fn)


def days_between(a: str | None, b: str | None) -> int | None:
    """b - a in days (positive = b is later)."""
    if not a or not b:
        return None
    return (datetime.fromisoformat(b).date() - datetime.fromisoformat(a).date()).days


def main():
    survivors = json.loads((HERE / "funnel_survivors.json").read_text(encoding="utf-8"))
    out = []
    for i, r in enumerate(survivors, 1):
        sym = r["symbol"]
        cb = coinbase_date(sym)
        up = upbit_date(sym) if r["on_upbit"] else None
        bt = bithumb_date(sym) if r["on_bithumb"] else None
        co = coinone_date(sym) if r["on_coinone"] else None
        kr_dates = [d for d in (up, bt, co) if d]
        first_kr = min(kr_dates) if kr_dates else None
        alpha = r["alpha_iso"][:10]
        perp = r["perp_onboard_iso"][:10]
        rec = dict(r)
        rec.update({
            "coinbase_date": cb, "upbit_date": up, "bithumb_date": bt, "coinone_date": co,
            "first_korean_date": first_kr,
            "lag_alpha_to_perp": days_between(alpha, perp),
            "lag_coinbase_to_perp": days_between(cb, perp),
            "lag_alpha_to_coinbase": days_between(alpha, cb),
            "lag_coinbase_to_korean": days_between(cb, first_kr),
            "lag_perp_to_korean": days_between(perp, first_kr),
        })
        out.append(rec)
        print(f"[{i:2}/{len(survivors)}] {sym:10} cb={cb} up={up} bt={bt} co={co} firstKR={first_kr}")
    (HERE / "funnel_dated.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote funnel_dated.json ({len(out)} tokens)")


if __name__ == "__main__":
    main()

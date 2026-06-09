"""Extended sweep: futures/perps + remaining spot venues for CTR and SLX."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/probe2"}
CACHE = Path(__file__).resolve().parents[2] / "cache"

TARGETS = {
    "CTR": {"start": datetime(2026, 5, 25, 0, tzinfo=timezone.utc),
            "end":   datetime(2026, 5, 27, 0, tzinfo=timezone.utc)},
    "SLX": {"start": datetime(2026, 5, 24, 0, tzinfo=timezone.utc),
            "end":   datetime(2026, 5, 26, 23, tzinfo=timezone.utc)},
}


def ms(dt): return int(dt.timestamp() * 1000)
def s(dt): return int(dt.timestamp())


def _earliest_ms(values):
    return min(values) if values else None


# ---------- Binance main ----------
def binance_spot(sym, start, end):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": f"{sym}USDT", "interval": "5m", "startTime": ms(start), "limit": 1000},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json()
        return _earliest_ms([int(x[0]) for x in d]) if d else None
    except: return None


def binance_perp(sym, start, end):
    for variant in (f"{sym}USDT", f"1000{sym}USDT", f"10000{sym}USDT"):
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                             params={"symbol": variant, "interval": "5m", "startTime": ms(start), "limit": 1000},
                             headers=UA, timeout=15)
            if r.status_code != 200: continue
            d = r.json()
            if d: return (variant, _earliest_ms([int(x[0]) for x in d]))
        except: pass
    return None


# ---------- Bybit ----------
def bybit_linear(sym, start, end):
    for variant in (f"{sym}USDT", f"1000{sym}USDT", f"10000{sym}USDT"):
        try:
            r = requests.get("https://api.bybit.com/v5/market/kline",
                             params={"category": "linear", "symbol": variant, "interval": "5", "start": ms(start), "limit": 1000},
                             headers=UA, timeout=15)
            if r.status_code != 200: continue
            d = r.json().get("result", {}).get("list") or []
            if d: return (variant, _earliest_ms([int(x[0]) for x in d]))
        except: pass
    return None


# ---------- OKX ----------
def okx_swap(sym, start, end):
    for variant in (f"{sym}-USDT-SWAP", f"1000{sym}-USDT-SWAP"):
        try:
            r = requests.get("https://www.okx.com/api/v5/market/history-candles",
                             params={"instId": variant, "bar": "5m", "limit": 100, "before": ms(start)},
                             headers=UA, timeout=15)
            if r.status_code != 200: continue
            d = r.json().get("data") or []
            if d: return (variant, _earliest_ms([int(x[0]) for x in d]))
        except: pass
    return None


# ---------- Bitget ----------
def bitget_perp(sym, start, end):
    for variant in (f"{sym}USDT", f"1000{sym}USDT", f"10000{sym}USDT"):
        try:
            r = requests.get("https://api.bitget.com/api/v2/mix/market/candles",
                             params={"symbol": variant, "productType": "USDT-FUTURES",
                                     "granularity": "5m", "startTime": ms(start),
                                     "endTime": ms(end), "limit": 1000},
                             headers=UA, timeout=15)
            if r.status_code != 200: continue
            d = r.json().get("data") or []
            if d: return (variant, _earliest_ms([int(x[0]) for x in d]))
        except: pass
    return None


# ---------- Gate.io ----------
def gate_perp(sym, start, end):
    for variant in (f"{sym}_USDT", f"1000{sym}_USDT"):
        try:
            r = requests.get(f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                             params={"contract": variant, "interval": "5m",
                                     "from": s(start), "to": s(end)},
                             headers=UA, timeout=15)
            if r.status_code != 200: continue
            d = r.json() or []
            if d:
                return (variant, _earliest_ms([int(x["t"]) * 1000 for x in d if isinstance(x, dict) and "t" in x]))
        except: pass
    return None


# ---------- KuCoin futures ----------
def kucoin_perp(sym, start, end):
    for variant in (f"{sym}USDTM", f"1000{sym}USDTM"):
        try:
            r = requests.get("https://api-futures.kucoin.com/api/v1/kline/query",
                             params={"symbol": variant, "granularity": 5,
                                     "from": ms(start), "to": ms(end)},
                             headers=UA, timeout=15)
            if r.status_code != 200: continue
            d = r.json().get("data") or []
            if d: return (variant, _earliest_ms([int(x[0]) for x in d]))
        except: pass
    return None


# ---------- HTX futures ----------
def htx_perp(sym, start, end):
    try:
        r = requests.get("https://api.hbdm.com/linear-swap-ex/market/history/kline",
                         params={"contract_code": f"{sym}-USDT", "period": "5min", "size": 2000},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if not d: return None
        in_range = [int(x["id"]) * 1000 for x in d if s(start) <= int(x["id"]) <= s(end)]
        return _earliest_ms(in_range)
    except: return None


# ---------- BitMart futures ----------
def bitmart_perp(sym, start, end):
    try:
        r = requests.get("https://api-cloud-v2.bitmart.com/contract/public/kline",
                         params={"symbol": f"{sym}USDT", "step": 5, "start_time": s(start), "end_time": s(end)},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if not d: return None
        return _earliest_ms([int(x["timestamp"]) * 1000 for x in d if isinstance(x, dict) and "timestamp" in x])
    except: return None


# ---------- Coinbase Intl (perps) ----------
def coinbase_intx(sym, start, end):
    try:
        r = requests.get(f"https://api.international.coinbase.com/api/v1/instruments/{sym}-PERP/candles",
                         params={"granularity": "FIVE_MINUTE",
                                 "start": start.isoformat().replace("+00:00", "Z"),
                                 "end": end.isoformat().replace("+00:00", "Z")},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("aggregations") or r.json() or []
        if d and isinstance(d, list) and d:
            times = []
            for x in d:
                if isinstance(x, dict) and "start" in x:
                    times.append(int(datetime.fromisoformat(x["start"].replace("Z", "+00:00")).timestamp() * 1000))
            return _earliest_ms(times)
    except: pass
    return None


# ---------- Bithumb (KR spot) ----------
def bithumb(sym, start, end):
    try:
        r = requests.get(f"https://api.bithumb.com/public/candlestick/{sym}_KRW/1m",
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if not d: return None
        in_range = [int(x[0]) for x in d if ms(start) <= int(x[0]) <= ms(end)]
        return _earliest_ms(in_range)
    except: return None


# ---------- BingX, LBank, XT ----------
def bingx_spot(sym, start, end):
    try:
        r = requests.get("https://open-api.bingx.com/openApi/spot/v1/market/kline",
                         params={"symbol": f"{sym}-USDT", "interval": "5m",
                                 "startTime": ms(start), "endTime": ms(end), "limit": 1000},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if not d: return None
        return _earliest_ms([int(x[0]) if isinstance(x, list) else int(x["time"]) for x in d])
    except: return None


def bingx_perp(sym, start, end):
    try:
        r = requests.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines",
                         params={"symbol": f"{sym}-USDT", "interval": "5m",
                                 "startTime": ms(start), "endTime": ms(end), "limit": 1000},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if not d: return None
        return _earliest_ms([int(x["time"]) if isinstance(x, dict) else int(x[0]) for x in d])
    except: return None


def lbank_spot(sym, start, end):
    try:
        r = requests.get("https://api.lbkex.com/v2/kline.do",
                         params={"symbol": f"{sym.lower()}_usdt", "size": 2000,
                                 "type": "minute5", "time": s(start)},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if not d: return None
        in_range = [int(x[0]) * 1000 for x in d if isinstance(x, list) and s(start) <= int(x[0]) <= s(end)]
        return _earliest_ms(in_range)
    except: return None


def xt_spot(sym, start, end):
    try:
        r = requests.get("https://sapi.xt.com/v4/public/kline",
                         params={"symbol": f"{sym.lower()}_usdt", "interval": "5m",
                                 "startTime": ms(start), "endTime": ms(end), "limit": 1000},
                         headers=UA, timeout=15)
        if r.status_code != 200: return None
        d = r.json().get("result") or r.json().get("data") or []
        if not d: return None
        return _earliest_ms([int(x.get("t") or x.get("time") or x[0]) for x in d if x])
    except: return None


FETCHERS = [
    ("Binance Spot",     binance_spot),
    ("Binance Perp",     binance_perp),
    ("Bybit Perp",       bybit_linear),
    ("OKX Perp",         okx_swap),
    ("Bitget Perp",      bitget_perp),
    ("Gate.io Perp",     gate_perp),
    ("KuCoin Perp",      kucoin_perp),
    ("HTX Perp",         htx_perp),
    ("BitMart Perp",     bitmart_perp),
    ("Coinbase INTX",    coinbase_intx),
    ("Bithumb",          bithumb),
    ("BingX Spot",       bingx_spot),
    ("BingX Perp",       bingx_perp),
    ("LBank Spot",       lbank_spot),
    ("XT Spot",          xt_spot),
]


def main():
    out = {}
    for token, cfg in TARGETS.items():
        out[token] = {}
        print(f"\n=== {token} ===")
        for label, fn in FETCHERS:
            res = fn(token, cfg["start"], cfg["end"])
            if isinstance(res, tuple):
                variant, t_ms = res
            else:
                variant, t_ms = None, res
            if t_ms:
                t = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
                iso = t.isoformat().replace("+00:00", "Z")
                out[token][label] = {"iso": iso, "variant": variant}
                v = f" [{variant}]" if variant else ""
                print(f"  {label:18}  {iso}{v}")
            else:
                print(f"  {label:18}  -")
    (CACHE / "ctr_slx_probe2.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nsaved -> {CACHE / 'ctr_slx_probe2.json'}")


if __name__ == "__main__":
    main()

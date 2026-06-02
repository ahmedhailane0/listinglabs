"""Probe each CEX's earliest 1m candle for a token to derive listing time."""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/probe"}
CACHE = Path(__file__).parent.parent / "cache"
CACHE.mkdir(exist_ok=True)

# token -> per-venue config
TARGETS = {
    "QAIT": {
        "search_start": datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
        "search_end":   datetime(2026, 5, 29, 19, 0, tzinfo=timezone.utc),
        "venues": ["binance_spot", "coinbase", "okx_spot", "bybit_spot", "kraken_spot",
                   "kucoin", "bitget_spot", "gate_spot", "upbit",
                   "binance_perp", "okx_swap", "bybit_linear", "bitget_perp",
                   "gate_perp", "kucoin_futures", "kraken_futures"],
    },
    "NEX": {
        "search_start": datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        "search_end":   datetime(2026, 5, 27, 14, 0, tzinfo=timezone.utc),
        "venues": ["binance_spot", "coinbase", "okx_spot", "bybit_spot", "kraken_spot",
                   "kucoin", "bitget_spot", "gate_spot", "upbit",
                   "binance_perp", "okx_swap", "bybit_linear", "bitget_perp",
                   "gate_perp", "kucoin_futures", "kraken_futures"],
    },
    "CTR": {
        "search_start": datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
        "search_end":   datetime(2026, 5, 27, 0, 0, tzinfo=timezone.utc),
        "venues": ["coinbase", "kucoin", "mexc_spot", "bitmart", "htx", "bybit_spot", "gate_spot", "okx_spot", "upbit"],
    },
    "SLX": {
        "search_start": datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc),
        "search_end":   datetime(2026, 5, 26, 23, 0, tzinfo=timezone.utc),
        "venues": ["mexc_spot", "bitmart", "bitget_spot", "mexc_futures", "bybit_spot", "gate_spot", "okx_spot", "kucoin", "htx"],
    },
}


def ms(dt): return int(dt.timestamp() * 1000)
def s(dt): return int(dt.timestamp())


def fetch_binance_spot(sym, start, end):
    url = "https://api.binance.com/api/v3/klines"
    try:
        r = requests.get(url, params={"symbol": f"{sym}USDT", "interval": "1m", "startTime": ms(start), "endTime": ms(end), "limit": 1000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_kraken_spot(sym, start, end):
    # Kraken OHLC returns from `since`, interval in minutes; result keyed by pair name.
    url = "https://api.kraken.com/0/public/OHLC"
    try:
        r = requests.get(url, params={"pair": f"{sym}USD", "interval": 1, "since": s(start)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        result = r.json().get("result") or {}
        rows = next((v for k, v in result.items() if k != "last"), None)
        if not rows: return None
        in_range = [int(row[0]) * 1000 for row in rows if s(start) <= int(row[0]) <= s(end)]
        return min(in_range) if in_range else None
    except: return None


def fetch_coinbase(sym, start, end):
    # Coinbase Exchange candles, granularity 60 (1m), max 300 per call.
    out = []
    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(minutes=300), end)
        url = f"https://api.exchange.coinbase.com/products/{sym}-USD/candles"
        try:
            r = requests.get(url, params={"granularity": 60, "start": cur.isoformat(), "end": chunk_end.isoformat()}, headers=UA, timeout=15)
            if r.status_code != 200:
                # try USDC pair
                if sym == "CTR":
                    r = requests.get(f"https://api.exchange.coinbase.com/products/{sym}-USDC/candles", params={"granularity": 60, "start": cur.isoformat(), "end": chunk_end.isoformat()}, headers=UA, timeout=15)
            if r.status_code == 200:
                rows = r.json()
                for row in rows:
                    out.append(int(row[0]) * 1000)
        except Exception as e:
            pass
        cur = chunk_end
    return min(out) if out else None


def fetch_kucoin(sym, start, end):
    url = "https://api.kucoin.com/api/v1/market/candles"
    try:
        r = requests.get(url, params={"symbol": f"{sym}-USDT", "type": "1min", "startAt": s(start), "endAt": s(end)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) * 1000 for row in data)
    except: return None


def fetch_mexc_spot(sym, start, end):
    url = "https://api.mexc.com/api/v3/klines"
    try:
        r = requests.get(url, params={"symbol": f"{sym}USDT", "interval": "1m", "startTime": ms(start), "endTime": ms(end), "limit": 1000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_mexc_futures(sym, start, end):
    url = f"https://contract.mexc.com/api/v1/contract/kline/{sym}_USDT"
    try:
        r = requests.get(url, params={"interval": "Min1", "start": s(start), "end": s(end)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or {}
        times = data.get("time") or []
        if not times: return None
        return min(int(t) for t in times) * 1000
    except: return None


def fetch_bitmart(sym, start, end):
    url = "https://api-cloud.bitmart.com/spot/quotation/v3/klines"
    try:
        r = requests.get(url, params={"symbol": f"{sym}_USDT", "step": 1, "before": s(end), "limit": 200}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) * 1000 for row in data)
    except: return None


def fetch_htx(sym, start, end):
    # Huobi/HTX kline; uses 'size' (count from latest)
    url = "https://api.huobi.pro/market/history/kline"
    try:
        r = requests.get(url, params={"symbol": f"{sym.lower()}usdt", "period": "1min", "size": 2000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        in_range = [int(row["id"]) * 1000 for row in data if s(start) <= int(row["id"]) <= s(end)]
        return min(in_range) if in_range else None
    except: return None


def fetch_bitget_spot(sym, start, end):
    url = "https://api.bitget.com/api/v2/spot/market/candles"
    try:
        r = requests.get(url, params={"symbol": f"{sym}USDT", "granularity": "1min", "startTime": ms(start), "endTime": ms(end), "limit": 1000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_bybit_spot(sym, start, end):
    url = "https://api.bybit.com/v5/market/kline"
    try:
        r = requests.get(url, params={"category": "spot", "symbol": f"{sym}USDT", "interval": "1", "start": ms(start), "end": ms(end), "limit": 1000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("result", {}).get("list") or []
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_gate_spot(sym, start, end):
    url = "https://api.gateio.ws/api/v4/spot/candlesticks"
    try:
        r = requests.get(url, params={"currency_pair": f"{sym}_USDT", "interval": "1m", "from": s(start), "to": s(end)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json() or []
        if not data: return None
        return min(int(row[0]) * 1000 for row in data)
    except: return None


def fetch_okx_spot(sym, start, end):
    url = "https://www.okx.com/api/v5/market/history-candles"
    try:
        r = requests.get(url, params={"instId": f"{sym}-USDT", "bar": "1m", "after": ms(end), "before": ms(start), "limit": 100}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_upbit(sym, start, end):
    url = "https://api.upbit.com/v1/candles/minutes/1"
    try:
        # Upbit supports KRW & USDT base
        for market in (f"USDT-{sym}", f"KRW-{sym}"):
            r = requests.get(url, params={"market": market, "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "count": 200}, headers=UA, timeout=15)
            if r.status_code != 200: continue
            data = r.json() or []
            if not data: continue
            times = [datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=timezone.utc) for row in data]
            in_range = [t for t in times if start <= t <= end]
            if in_range:
                return ms(min(in_range))
    except: pass
    return None


# ---- Derivatives (perp/futures). Probe at 1h to dodge the ~1000-candle cap;
# returns the first candle in-range = approx listing hour. None = symbol absent. ----

def fetch_binance_perp(sym, start, end):
    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        r = requests.get(url, params={"symbol": f"{sym}USDT", "interval": "1h", "startTime": ms(start), "endTime": ms(end), "limit": 1000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_okx_swap(sym, start, end):
    url = "https://www.okx.com/api/v5/market/history-candles"
    try:
        r = requests.get(url, params={"instId": f"{sym}-USDT-SWAP", "bar": "1H", "after": ms(end), "before": ms(start), "limit": 100}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_bybit_linear(sym, start, end):
    url = "https://api.bybit.com/v5/market/kline"
    # NEX-style listings use a 10000-multiplied contract symbol; try both.
    for s_ in (f"{sym}USDT", f"10000{sym}USDT"):
        try:
            r = requests.get(url, params={"category": "linear", "symbol": s_, "interval": "60", "start": ms(start), "end": ms(end), "limit": 1000}, headers=UA, timeout=15)
            if r.status_code != 200: continue
            data = r.json().get("result", {}).get("list") or []
            if data:
                return min(int(row[0]) for row in data)
        except: continue
    return None


def fetch_bitget_perp(sym, start, end):
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    try:
        r = requests.get(url, params={"symbol": f"{sym}USDT", "productType": "usdt-futures", "granularity": "1H", "startTime": ms(start), "endTime": ms(end), "limit": 1000}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_gate_perp(sym, start, end):
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    try:
        r = requests.get(url, params={"contract": f"{sym}_USDT", "interval": "1h", "from": s(start), "to": s(end)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json() or []
        if not data: return None
        return min(int(row["t"]) for row in data) * 1000
    except: return None


def fetch_kucoin_futures(sym, start, end):
    url = "https://api-futures.kucoin.com/api/v1/kline/query"
    try:
        r = requests.get(url, params={"symbol": f"{sym}USDTM", "granularity": 60, "from": ms(start), "to": ms(end)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        data = r.json().get("data") or []
        if not data: return None
        return min(int(row[0]) for row in data)
    except: return None


def fetch_kraken_futures(sym, start, end):
    # Kraken Futures charts: perpetual symbol is PF_<base>USD.
    url = f"https://futures.kraken.com/api/charts/v1/trade/PF_{sym}USD/1h"
    try:
        r = requests.get(url, params={"from": s(start), "to": s(end)}, headers=UA, timeout=15)
        if r.status_code != 200: return None
        candles = r.json().get("candles") or []
        if not candles: return None
        return min(int(c["time"]) for c in candles)
    except: return None


FETCHERS = {
    "binance_spot": fetch_binance_spot,
    "binance_perp": fetch_binance_perp,
    "okx_swap": fetch_okx_swap,
    "bybit_linear": fetch_bybit_linear,
    "bitget_perp": fetch_bitget_perp,
    "gate_perp": fetch_gate_perp,
    "kucoin_futures": fetch_kucoin_futures,
    "kraken_futures": fetch_kraken_futures,
    "kraken_spot": fetch_kraken_spot,
    "coinbase": fetch_coinbase,
    "kucoin": fetch_kucoin,
    "mexc_spot": fetch_mexc_spot,
    "mexc_futures": fetch_mexc_futures,
    "bitmart": fetch_bitmart,
    "htx": fetch_htx,
    "bitget_spot": fetch_bitget_spot,
    "bybit_spot": fetch_bybit_spot,
    "gate_spot": fetch_gate_spot,
    "okx_spot": fetch_okx_spot,
    "upbit": fetch_upbit,
}

LABELS = {
    "binance_spot": "Binance Spot",
    "binance_perp": "Binance Perp",
    "okx_swap": "OKX Perp",
    "bybit_linear": "Bybit Perp",
    "bitget_perp": "Bitget Perp",
    "gate_perp": "Gate Perp",
    "kucoin_futures": "KuCoin Futures",
    "kraken_futures": "Kraken Futures",
    "kraken_spot": "Kraken Spot",
    "coinbase": "Coinbase Spot",
    "kucoin": "KuCoin Spot",
    "mexc_spot": "MEXC Spot",
    "mexc_futures": "MEXC Futures",
    "bitmart": "BitMart Spot",
    "htx": "HTX Spot",
    "bitget_spot": "Bitget Spot",
    "bybit_spot": "Bybit Spot",
    "gate_spot": "Gate.io Spot",
    "okx_spot": "OKX Spot",
    "upbit": "Upbit",
}


def main():
    only = {t.upper() for t in sys.argv[1:]} or None
    tokens = [t for t in TARGETS if only is None or t in only]
    out = {}
    for token in tokens:
        cfg = TARGETS[token]
        out[token] = {}
        for venue in cfg["venues"]:
            fn = FETCHERS[venue]
            t_ms = fn(token, cfg["search_start"], cfg["search_end"])
            if t_ms:
                t = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
                out[token][venue] = {
                    "label": LABELS[venue],
                    "iso": t.isoformat().replace("+00:00", "Z"),
                }
                print(f"{token} {venue}: {t.isoformat()}")
            else:
                print(f"{token} {venue}: -")
    fname = f"{'_'.join(t.lower() for t in tokens)}_probe.json"
    (CACHE / fname).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nsaved -> {CACHE / fname}")


if __name__ == "__main__":
    main()

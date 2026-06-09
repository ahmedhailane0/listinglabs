"""Fetch 1d klines + funding rate for each row's Binance perp.

For every row in parsed_rows.json we look up the USDT-quoted PERPETUAL symbol
in cache/binance_fapi_exchangeinfo.json, then call:
  - fapi/v1/klines        interval=1d, startTime=binance_perp_date, limit=60
  - fapi/v1/fundingRate   startTime=binance_perp_date, limit=1000 (~30d)

Outputs:
  cache/binance_perp_klines.json   { symbol -> [kline rows] }
  cache/binance_funding.json       { symbol -> [funding rows] }
  cache/perp_symbol_map.json       { ticker -> symbol }   (for reuse downstream)
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]   # verifysheet/ (repo root)
CACHE = ROOT / "cache"
KLINES = CACHE / "binance_perp_klines.json"
FUNDING = CACHE / "binance_funding.json"
SYMMAP = CACHE / "perp_symbol_map.json"

UA = {"User-Agent": "Mozilla/5.0 verifysheet/correlation"}
BASE = "https://fapi.binance.com"


def load_json(p, default):
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def save_json(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def build_symbol_map():
    info = json.loads((CACHE / "binance_fapi_exchangeinfo.json").read_text(encoding="utf-8"))
    rows = info if isinstance(info, list) else info.get("symbols", [])
    by_base = {}
    for s in rows:
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        base = s["baseAsset"]
        by_base.setdefault(base, []).append(s["symbol"])
    # prefer exact "{BASE}USDT", else first
    out = {}
    for base, syms in by_base.items():
        out[base] = next((x for x in syms if x == f"{base}USDT"), syms[0])
    return out


def to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_klines(symbol: str, start_ms: int) -> list:
    r = requests.get(
        f"{BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": "1d", "startTime": start_ms, "limit": 60},
        headers=UA, timeout=20,
    )
    if r.status_code == 400:
        return []  # symbol delisted
    r.raise_for_status()
    return r.json()


def fetch_funding(symbol: str, start_ms: int) -> list:
    end_ms = start_ms + 30 * 24 * 3600 * 1000
    r = requests.get(
        f"{BASE}/fapi/v1/fundingRate",
        params={"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000},
        headers=UA, timeout=20,
    )
    if r.status_code == 400:
        return []
    r.raise_for_status()
    return r.json()


def main():
    rows = json.loads((ROOT / "parsed_rows.json").read_text(encoding="utf-8"))
    sym_map = build_symbol_map()
    save_json(SYMMAP, sym_map)

    klines = load_json(KLINES, {})
    funding = load_json(FUNDING, {})

    missing_symbol = []
    done = 0
    for row in rows:
        ticker = row.get("symbol", "").upper()
        date = row.get("binance_perp")
        if not ticker or not date:
            continue
        symbol = sym_map.get(ticker)
        if not symbol:
            missing_symbol.append((row.get("name"), ticker))
            continue
        if symbol in klines and symbol in funding:
            done += 1
            continue
        try:
            start_ms = to_ms(date)
            if symbol not in klines:
                klines[symbol] = fetch_klines(symbol, start_ms)
                time.sleep(0.15)
            if symbol not in funding:
                funding[symbol] = fetch_funding(symbol, start_ms)
                time.sleep(0.15)
            done += 1
            if done % 20 == 0:
                save_json(KLINES, klines)
                save_json(FUNDING, funding)
                print(f"  ...{done} done, last={symbol}")
        except requests.HTTPError as e:
            print(f"  HTTP {e.response.status_code} on {symbol}")
            klines.setdefault(symbol, [])
            funding.setdefault(symbol, [])
        except Exception as e:
            print(f"  error on {symbol}: {e}")

    save_json(KLINES, klines)
    save_json(FUNDING, funding)
    print(f"done. {done} symbols fetched. {len(missing_symbol)} tickers had no fapi symbol.")
    if missing_symbol:
        print("missing:", missing_symbol[:10], "..." if len(missing_symbol) > 10 else "")


if __name__ == "__main__":
    main()

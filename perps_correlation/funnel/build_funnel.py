"""Stage 1: membership intersection for the Alpha -> Perp -> Coinbase -> Korea funnel.

Universe = Binance Alpha tokens listed 2025-01-01 .. now.
Gates    = also on Binance USDT-M perp, Coinbase spot (USD/USDC), and at least
           one Korean exchange (Upbit / Bithumb / Coinone).

Membership only (cheap bulk calls). Per-venue listing DATES + lags come in a
later stage, only for the survivors. Saves survivors -> funnel_survivors.json.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/funnel"}
HERE = Path(__file__).parent
WINDOW_START = datetime(2025, 1, 1, tzinfo=timezone.utc)


def get(url, **kw):
    return requests.get(url, headers=UA, timeout=30, **kw)


def alpha_universe() -> dict[str, dict]:
    d = get("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list").json()["data"]
    out: dict[str, dict] = {}
    for t in d:
        lt = t.get("listingTime")
        if not lt:
            continue
        dt = datetime.fromtimestamp(lt / 1000, tz=timezone.utc)
        if dt < WINDOW_START:
            continue
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        rec = {
            "symbol": sym, "name": t.get("name"), "chain": t.get("chainName"),
            "contract": t.get("contractAddress"), "alpha_iso": dt.isoformat().replace("+00:00", "Z"),
            "alpha_ms": lt, "fdv": t.get("fdv"), "mcap": t.get("marketCap"),
            "circ": t.get("circulatingSupply"), "total": t.get("totalSupply"),
            "fully_delisted": t.get("fullyDelisted"), "alphaId": t.get("alphaId"),
        }
        # keep earliest alpha listing if symbol repeats
        if sym not in out or lt < out[sym]["alpha_ms"]:
            out[sym] = rec
    return out


def perp_map() -> dict[str, dict]:
    """baseAsset -> {onboard_ms, symbol}. Includes 1000x / 10000x prefixed contracts
    mapped back to the bare ticker so e.g. 1000SATS-perp matches alpha SATS."""
    d = get("https://fapi.binance.com/fapi/v1/exchangeInfo").json()["symbols"]
    out: dict[str, dict] = {}
    for s in d:
        if s.get("quoteAsset") != "USDT":
            continue
        base = s.get("baseAsset") or ""
        ob = s.get("onboardDate")
        if not base or not ob:
            continue
        keys = {base}
        for pre in ("1000000", "10000", "1000"):
            if base.startswith(pre) and len(base) > len(pre):
                keys.add(base[len(pre):])
        for k in keys:
            if k not in out or ob < out[k]["onboard_ms"]:
                out[k] = {"onboard_ms": ob, "symbol": s["symbol"]}
    return out


def coinbase_bases() -> set[str]:
    d = get("https://api.exchange.coinbase.com/products").json()
    return {p["base_currency"].upper() for p in d if p.get("quote_currency") in ("USD", "USDC")}


def upbit_bases() -> set[str]:
    d = get("https://api.upbit.com/v1/market/all", params={"isDetails": "false"}).json()
    out = set()
    for m in d:
        mk = m.get("market", "")
        if mk.split("-")[0] in ("KRW", "USDT"):
            out.add(mk.split("-", 1)[1].upper())
    return out


def bithumb_bases() -> set[str]:
    out = set()
    for quote in ("KRW",):
        try:
            d = get(f"https://api.bithumb.com/public/ticker/ALL_{quote}").json().get("data", {})
            out |= {k.upper() for k in d if k != "date"}
        except Exception:
            pass
    return out


def coinone_bases() -> set[str]:
    try:
        d = get("https://api.coinone.co.kr/public/v2/markets/KRW").json()
        return {m.get("target_currency", "").upper() for m in d.get("markets", []) if m.get("target_currency")}
    except Exception:
        return set()


def main():
    alpha = alpha_universe()
    perp = perp_map()
    cb = coinbase_bases()
    up, bt, co = upbit_bases(), bithumb_bases(), coinone_bases()
    korean = up | bt | co
    print(f"alpha(2025+)={len(alpha)}  perp={len(perp)}  coinbase={len(cb)}  "
          f"upbit={len(up)} bithumb={len(bt)} coinone={len(co)}  korean_union={len(korean)}")

    survivors = []
    for sym, rec in alpha.items():
        if sym not in perp or sym not in cb or sym not in korean:
            continue
        rec = dict(rec)
        rec["perp_symbol"] = perp[sym]["symbol"]
        rec["perp_onboard_ms"] = perp[sym]["onboard_ms"]
        rec["perp_onboard_iso"] = datetime.fromtimestamp(perp[sym]["onboard_ms"] / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        rec["on_upbit"] = sym in up
        rec["on_bithumb"] = sym in bt
        rec["on_coinone"] = sym in co
        survivors.append(rec)

    survivors.sort(key=lambda r: r["alpha_ms"])
    (HERE / "funnel_survivors.json").write_text(json.dumps(survivors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSURVIVORS (Alpha n Perp n Coinbase n Korean): {len(survivors)}\n")
    print(f"{'SYM':12}{'chain':10}{'alpha':12}{'perp':12}{'FDV':>10}  KR")
    for r in survivors:
        kr = "".join(c for c, on in [("U", r["on_upbit"]), ("B", r["on_bithumb"]), ("C", r["on_coinone"])] if on)
        try:
            fdv = f"${float(r['fdv'])/1e6:.0f}M"
        except (TypeError, ValueError):
            fdv = "-"
        print(f"{r['symbol']:12}{(r['chain'] or '-'):10}{r['alpha_iso'][:10]:12}{r['perp_onboard_iso'][:10]:12}{fdv:>10}  {kr}")


if __name__ == "__main__":
    main()

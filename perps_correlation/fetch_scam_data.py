"""Gather full per-token data for the Scam Watchlist so it can be rendered like
the Listing Reactions report (chart + stats + funding + OI), for the tokens in
`scam list -   list.csv`.

Sources (all free):
  - CoinGecko: identity (symbol/name/chain/contract), current price/MC/FDV/vol,
    and the price history that feeds the sparkline + detail chart. The CSV's
    CHART LINK gives the exact CG id; tokens without a link are resolved by a
    symbol search validated against the CSV price.
  - cmc_map.json (repo root): symbol -> CMC slug, used to pull OI via fetch_oi_cmc.
  - RootData (fetch_rootdata): funding amount + investors.

Writes:
  cache/scam_data.json          { SYM: {id,name,symbol,chain,contract,price,mcap,
                                        fdv,vol,memo,memo_en,days,chart_link,
                                        cmc_slug,oi_usd,oi_pct_mcap,
                                        funding:{amount,investors[]}} }
  cache/scam_prices/<SYM>.json  [[ms, price], ...]  (price history)

    python fetch_scam_data.py
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
CSV_PATH = Path(r"C:\Users\PC\Downloads\scam list -   list.csv")
OUT = CACHE / "scam_data.json"
PRICES = CACHE / "scam_prices"
CMC_MAP = HERE.parent / "cmc_map.json"
CG = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PACE = 2.6  # CoinGecko free tier ~30/min

# The user's Chinese notes (exact CSV strings) -> English.
MEMO_EN = {
    "5/1起，价格>1 维持至今": "Since May 1, FDV >$1B, sustained to date",
    "4/18 首次到10亿fdv 短暂跌破，0.09左右维持收盘价，4/30至今收盘价均维持10亿以上":
        "Apr 18: first hit $1B FDV, briefly dipped below; held ~$0.09 close; since Apr 30 closing FDV stayed above $1B",
    "2025/10/01-2025/10/10；2026/04/11-至今": "Oct 1–10 2025; Apr 11 2026 – present",
    "5/5至今": "May 5 – present",
    "未突破过10亿，但一直是上升趋势": "Never broke $1B, but a steady uptrend",
    "3/21-3/31（28短暂跌破）；4/16-17短暂回到10亿":
        "Mar 21–31 (briefly below on the 28th); Apr 16–17 briefly back above $1B",
    "4/20突破10亿，前后两天维持10亿上下波动（upbit上币）":
        "Broke $1B on Apr 20; hovered around $1B for a couple of days (listed on Upbit)",
    "4/10-4/19，最高270亿": "Apr 10–19; peak $27B",
    "12/31-1/16 2/26-4/9，最高85亿": "Dec 31–Jan 16 & Feb 26–Apr 9; peak $8.5B",
    "未突破过10亿，4/28-5/6上升趋势": "Never broke $1B; uptrend Apr 28–May 6",
    "5/4 短暂突破10亿，4/24至今维持在5亿左右":
        "Briefly broke $1B on May 4; ~$500M from Apr 24 to date",
    "4/30以来一直成上升趋势": "Steady uptrend since Apr 30",
    "4/20以来一直成上升趋势": "Steady uptrend since Apr 20",
    "未突破过10亿": "Never broke $1B",
    "12/3-1/2 维持收盘fdv在10亿以上": "Dec 3–Jan 2: kept closing FDV above $1B",
}


def _get(url: str, tries: int = 4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == tries - 1:
                print(f"    GET failed: {e}")
                return None
            time.sleep(PACE * (i + 2))
    return None


def parse_usd(s: str):
    if not s:
        return None
    m = re.search(r"([\d,.]+)\s*([BMK]?)", s.replace("$", ""), re.I)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v * {"B": 1e9, "M": 1e6, "K": 1e3, "": 1}[m.group(2).upper()]


def cg_id_from_link(url: str):
    m = re.search(r"/coins/([^/?]+)", url or "")
    return m.group(1) if m else None


def resolve_id(name: str, csv_price, link: str):
    """CG id: from the chart link if present, else search by symbol and validate
    the candidate's price against the CSV price (guards ambiguous tickers)."""
    cid = cg_id_from_link(link)
    if cid:
        return cid, True
    s = _get(f"{CG}/search?query={urllib.parse.quote(name)}")
    time.sleep(PACE)
    coins = (s or {}).get("coins") or []
    if not coins:
        return None, False
    # validate top few by current price ~ CSV price
    for c in coins[:4]:
        d = _get(f"{CG}/coins/{c['id']}?localization=false&tickers=false&market_data=true"
                 "&community_data=false&developer_data=false")
        time.sleep(PACE)
        px = ((d or {}).get("market_data") or {}).get("current_price", {}).get("usd")
        if px and csv_price and 0.5 <= px / csv_price <= 2.0:
            return c["id"], True
    return coins[0]["id"], False  # fallback: top-ranked, unvalidated


def cmc_slug_for(symbol: str, cmc_list):
    cands = [t for t in cmc_list if (t.get("symbol") or "").upper() == symbol.upper()
             and t.get("is_active")]
    if not cands:
        return None
    cands.sort(key=lambda t: t.get("rank") or 10**9)
    return cands[0].get("slug")


def main():
    PRICES.mkdir(parents=True, exist_ok=True)
    rows = [r for r in csv.DictReader(CSV_PATH.open(encoding="utf-8-sig"))
            if (r.get("NAME") or "").strip()]
    cmc_list = json.loads(CMC_MAP.read_text(encoding="utf-8"))
    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}

    # RootData (funding) — optional; skip gracefully if no key.
    rd_hdrs = None
    try:
        import fetch_rootdata as frd
        rd_hdrs = frd.headers(frd.load_key())
    except Exception as e:
        print(f"(RootData unavailable: {e})")
    # OI helpers
    import fetch_oi_cmc as oimod

    for r in rows:
        sym = r["NAME"].strip().upper()
        csv_price = parse_usd(r.get("PRICE"))
        rec = {
            "symbol": sym,
            "memo": (r.get("MEMO") or "").strip(),
            "memo_en": MEMO_EN.get((r.get("MEMO") or "").strip(), (r.get("MEMO") or "").strip()),
            "days": (r.get("DAYS") or "").strip().replace("；", "; "),
            "chart_link": (r.get("CHART LINK") or "").strip(),
            "csv_price": csv_price,
            "csv_mc": parse_usd(r.get("MC")),
            "csv_vol": parse_usd(r.get("VOLUME")),
            "csv_fdv": parse_usd(r.get("FDV")),
            # listing references from the CSV (a "1" marks where it trades)
            "okx_spot": bool((r.get("OKX SPOT") or "").strip()),
            "coinbase_spot": bool((r.get("coinbase SPOT") or "").strip()),
        }
        cid, ok = resolve_id(r["NAME"].strip(), csv_price, rec["chart_link"])
        rec["cg_id"] = cid
        rec["resolved"] = ok
        if cid:
            d = _get(f"{CG}/coins/{cid}?localization=false&tickers=false&market_data=true"
                     "&community_data=false&developer_data=false")
            time.sleep(PACE)
            md = (d or {}).get("market_data") or {}
            plat = (d or {}).get("platforms") or {}
            chain, contract = next(iter(plat.items()), ("", ""))
            links = (d or {}).get("links") or {}
            homepage = next((u for u in (links.get("homepage") or []) if u), None)
            twitter = links.get("twitter_screen_name") or None
            rec.update({
                "website": homepage,
                "twitter": twitter,
                "name": (d or {}).get("name") or sym,
                "cg_symbol": ((d or {}).get("symbol") or "").upper(),
                "chain": chain, "contract": contract,
                "image": ((d or {}).get("image") or {}).get("small"),
                "price": md.get("current_price", {}).get("usd"),
                "mcap": md.get("market_cap", {}).get("usd"),
                "fdv": md.get("fully_diluted_valuation", {}).get("usd"),
                "vol": md.get("total_volume", {}).get("usd"),
                "circ_supply": md.get("circulating_supply"),
                "total_supply": md.get("total_supply"),
                "max_supply": md.get("max_supply"),
                "ath_price": (md.get("ath") or {}).get("usd"),
            })
            rec["supply_source"] = "coingecko"
            # circulation ratio = circulating / total (fall back to max supply)
            circ = md.get("circulating_supply")
            denom = md.get("total_supply") or md.get("max_supply")
            rec["circ_ratio"] = (circ / denom) if (circ and denom) else None
            # peak market cap ≈ all-time-high price × current circulating supply
            # (CoinGecko exposes no historical-supply series, so this is approximate)
            athp = (md.get("ath") or {}).get("usd")
            rec["peak_mcap"] = (athp * circ) if (athp and circ) else None
            mc = _get(f"{CG}/coins/{cid}/market_chart?vs_currency=usd&days=180&interval=daily")
            time.sleep(PACE)
            prices = (mc or {}).get("prices") or []
            # don't clobber a good prices file with an empty (throttled) result
            if prices or not (PRICES / f"{sym}.json").exists():
                (PRICES / f"{sym}.json").write_text(json.dumps(prices), encoding="utf-8")
            rec["n_prices"] = len(prices)
        else:
            rec["name"] = sym

        # CMC slug + OI + supply
        slug = cmc_slug_for(sym, cmc_list)
        rec["cmc_slug"] = slug
        if slug:
            oi = oimod.fetch_one(slug)
            mcm = oimod.fetch_mcap(slug)
            amt = oi.get("oi_usd")
            mcv = mcm.get("mcap_now_usd")
            rec["oi_usd"] = amt
            rec["oi_pct_mcap"] = (amt / mcv * 100) if (amt and mcv) else None
            # Prefer CMC supply — CoinGecko sometimes returns placeholder/wrong
            # values (total == circulating, or a bare "100"). When CMC has a
            # number, it wins; the supply-derived ratios are recomputed from it.
            cmc_circ = mcm.get("circulating_supply")
            cmc_tot = mcm.get("total_supply")
            cmc_max = mcm.get("max_supply")
            if cmc_circ is not None:
                rec["circ_supply"] = cmc_circ
            if cmc_tot is not None:
                rec["total_supply"] = cmc_tot
            if cmc_max is not None:
                rec["max_supply"] = cmc_max
            rec["supply_source"] = "coinmarketcap" if (cmc_circ or cmc_tot) else rec.get("supply_source")
            # recompute circulation ratio (circ / total, fall back to max) and
            # peak market cap (ATH price × circ supply) from the trusted supply
            circ2 = rec.get("circ_supply")
            denom2 = rec.get("total_supply") or rec.get("max_supply")
            rec["circ_ratio"] = (circ2 / denom2) if (circ2 and denom2) else rec.get("circ_ratio")
            athp2 = rec.get("ath_price")
            rec["peak_mcap"] = (athp2 * circ2) if (athp2 and circ2) else rec.get("peak_mcap")

        # funding (RootData)
        rec["funding"] = {"amount": None, "investors": []}
        if rd_hdrs is not None:
            try:
                import fetch_rootdata as frd
                hit = frd.search(rec.get("name", sym), sym, rd_hdrs)
                time.sleep(0.3)
                item = frd.get_item(hit.get("id"), rd_hdrs) if hit and hit.get("id") else None
                if item:
                    invs = [{"name": i.get("name"), "url": i.get("X") or i.get("rootdataurl"),
                             "lead": bool(i.get("lead_investor"))}
                            for i in (item.get("investors") or []) if i.get("name")]
                    invs.sort(key=lambda x: not x["lead"])
                    rec["funding"] = {"amount": item.get("total_funding"), "investors": invs}
            except Exception as e:
                print(f"    funding err {sym}: {e}")

        # merge-guard: never let a throttled/empty re-fetch overwrite a value we
        # already have. Keep the prior non-null for the volatile market fields
        # (and prices file) if this run came back empty for them.
        prev = out.get(sym) or {}
        for k in ("price", "mcap", "fdv", "vol", "chain", "contract", "cg_id",
                  "cmc_slug", "oi_usd", "oi_pct_mcap", "image", "name",
                  "website", "twitter", "circ_supply", "total_supply",
                  "max_supply", "ath_price", "circ_ratio", "peak_mcap",
                  "supply_source"):
            if rec.get(k) in (None, "") and prev.get(k) not in (None, ""):
                rec[k] = prev[k]
        # keep prior funding if this run found none
        if not (rec.get("funding") or {}).get("amount") and \
           not (rec.get("funding") or {}).get("investors") and prev.get("funding"):
            rec["funding"] = prev["funding"]
        out[sym] = rec
        flag = "" if rec.get("resolved", True) else "  (UNVALIDATED match)"
        print(f"{sym:9} id={cid} px={rec.get('price')} fdv={rec.get('fdv')} "
              f"oi={rec.get('oi_usd')} fund={rec['funding']['amount']}{flag}", flush=True)
        OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    res = sum(1 for v in out.values() if v.get("cg_id"))
    unval = [k for k, v in out.items() if v.get("cg_id") and not v.get("resolved")]
    print(f"\nwrote {OUT}: {res}/{len(rows)} resolved to a CoinGecko coin")
    if unval:
        print(f"  UNVALIDATED (ambiguous symbol, please eyeball): {', '.join(unval)}")


if __name__ == "__main__":
    main()

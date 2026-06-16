"""Seed + validate the manipulated-coin screener universe.

Reads the symbols transcribed from the owner's TradingView "MANIPULATED COINS"
watchlist PDF (grouped by the list's own sections) PLUS the 28 tokens already on
the Manipulated tab (cache/scam_data.json), then validates every base symbol
against two ground-truth sources:

  * cache/binance_fapi_exchangeinfo.json — the real Binance USDT-perp contracts
    (a base with no contract there can't be screened: spot-only / delisted /
    Bybit-only / mistranscribed).
  * cmc_map.json — CMC symbol->slug catalogue (30k coins), to confirm an FDV is
    resolvable for the page-level (BN+BYB OI)/FDV gate.

Writes cache/manip_watchlist.json:
  { "SYM": {"sections": [...], "sources": [...], "has_perp": bool,
            "cmc_slug": str|null} , ... }

and prints a PRE-FLIGHT report: how many coins are actually screenable, FDV
coverage, and — for any base that doesn't match a Binance contract — the closest
real tickers as correction suggestions (so a PDF misread like BROCCOLI ->
BROCCOLI714 is obvious and fixable). Transcription errors are therefore visible
and harmless: a wrong ticker simply fails to match and is reported, never faked.

    python tools/seed_manip_watchlist.py

Re-run any time after fixing a symbol below. Idempotent.
"""
from __future__ import annotations

import io
import json
from difflib import get_close_matches
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
CACHE = HERE.parent / "cache"
EXINFO = CACHE / "binance_fapi_exchangeinfo.json"
SCAM = CACHE / "scam_data.json"
CMCMAP = HERE.parent / "cmc_map.json"
OUT = CACHE / "manip_watchlist.json"

# ── Transcribed from the PDF, grouped by the watchlist's own section headers ───
# Sections kept verbatim (owner chose to include ALL of them). A symbol that
# appears in more than one section accumulates every section tag. Where the real
# ticker was unambiguous from validation it is already corrected inline
# (e.g. BROCCOLI -> BROCCOLI714); genuinely uncertain reads are left as-seen so
# the pre-flight flags them.
SECTIONS: dict[str, list[str]] = {
    "short_term_interest": [
        "CLO", "NAORIS", "VELVET", "LAB", "SKYAI", "M", "GRIFFAIN", "STABLE",
        "GUA", "ESPORTS", "ZEREBRO", "TAU", "UB", "US", "BROCCOLI714", "DEXE",
        "B", "BLUAI", "HANA", "IN", "BEAT", "PIEVERSE", "GENIUS", "AGT", "LYN",
        "TAC", "RECALL", "BR", "AIOT", "TRUTH", "BSB", "BAS", "UAI", "MAGHA",
        "JCT", "BANANAS31", "PLAY", "AIO",   # PLAY <- "PLAYSOUT" (OCR; PLAY perp confirmed live)
    ],
    "full_list": [
        "SPACE", "ZBT", "TST", "ENSO", "ARC", "SOPH", "ICNT", "TUT", "BARD",   # ICNT <- "ICHT" (OCR)
        "HUMA", "HEI", "AKE", "KITE", "EVAA", "TRUST", "BTRUST", "JELLYJELLY",
        "ACT", "SWARMS", "TRADOOR", "AVAAI", "XION", "PARTI", "MORPHO",
        "MUBARAK", "FF", "AWT", "AWE", "ARPA", "PTB", "ALCH", "XAN", "RIVER",
        "B2", "BAN", "XPIN", "RESOLV", "LA", "AERGO", "4", "GIGGLE", "BLESS",
        "Q", "APR", "IRYS", "KGEN", "ALLO", "SAHARA", "HEMI", "XNY", "BANK",
        "PIPPIN", "SIREN",
    ],
    "just_neg_funding_no_manip": [
        "MOODENG", "TOSHI", "DOOD", "ZORA", "AXS", "BLAST", "ANIME", "KAITO",
        "SUPER", "LSK", "MEU", "OG", "NMR", "MOVE", "BIO",
    ],
    "old": [
        "COAI", "H", "HIGH", "M", "MYX", "SOON", "BARD", "POWER", "FHE",   # H <- "HU" (HUSDT = token H's perp)
        "TAKE", "MASK", "TRB", "BTR", "JELLYJELLY", "AIA", "PUMPBTC", "BULLA",
        "TNSR", "TRADOOR", "AUCTION", "SAROS", "MERL", "LIGHT", "ZKJ", "RIVER",
        "BEAT", "ARIA", "RAVE", "IP", "LAYER", "WCT", "STO", "BAS", "FOLKS",
        "ID", "PIPPIN", "OM", "SIREN", "BANANAS31", "PLAY", "ALPACA",
    ],
}


def _load(p: Path):
    return json.loads(io.open(p, encoding="utf-8").read())


def main() -> None:
    # Ground truth: real Binance USDT perpetual base assets.
    ex = _load(EXINFO)
    syms = ex.get("symbols") if isinstance(ex, dict) else ex
    perp_bases = {x["baseAsset"].upper() for x in syms
                  if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT"
                  and x.get("status", "TRADING") == "TRADING"}

    # CMC symbol -> best slug for FDV resolvability. On ticker collisions, prefer
    # an active coin, then the lowest (best) rank.
    cmc = _load(CMCMAP)
    cmc_by_sym: dict[str, dict] = {}
    for c in cmc:
        s = (c.get("symbol") or "").upper()
        if not s:
            continue
        key = (1 if c.get("is_active") else 0, -(c.get("rank") or 10**9))
        cur = cmc_by_sym.get(s)
        if cur is None or key > (1 if cur.get("is_active") else 0, -(cur.get("rank") or 10**9)):
            cmc_by_sym[s] = c

    # Collect every symbol with its section tags.
    wl: dict[str, dict] = {}
    for section, bases in SECTIONS.items():
        for b in bases:
            rec = wl.setdefault(b.upper(), {"sections": [], "sources": []})
            if section not in rec["sections"]:
                rec["sections"].append(section)
            if "tradingview" not in rec["sources"]:
                rec["sources"].append("tradingview")

    # Merge the 28 existing Manipulated-tab tokens, and keep their KNOWN CMC slug
    # (scam_data already stores a verified slug) so the FDV gate covers them even
    # when the bare-symbol CMC lookup is ambiguous.
    scam = _load(SCAM)
    known_slug: dict[str, str] = {}
    for r in scam.values():
        s = (r.get("symbol") or "").upper()
        if not s:
            continue
        rec = wl.setdefault(s, {"sections": [], "sources": []})
        if "watchlist" not in rec["sources"]:
            rec["sources"].append("watchlist")
        if r.get("cmc_slug"):
            known_slug[s] = r["cmc_slug"]

    # Validate + enrich.
    for s, rec in wl.items():
        rec["has_perp"] = s in perp_bases
        c = cmc_by_sym.get(s)
        rec["cmc_slug"] = known_slug.get(s) or (c.get("slug") if c else None)

    OUT.write_text(json.dumps(wl, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── Pre-flight report ─────────────────────────────────────────────────────
    total = len(wl)
    screenable = [s for s, r in wl.items() if r["has_perp"]]
    no_perp = sorted(s for s, r in wl.items() if not r["has_perp"])
    no_fdv = sorted(s for s, r in wl.items() if r["has_perp"] and not r["cmc_slug"])
    print(f"\nwrote {OUT}")
    print(f"\n=== PRE-FLIGHT ===")
    print(f"total unique symbols (147 list + 28 watchlist, deduped): {total}")
    print(f"screenable (has a Binance USDT perp):                    {len(screenable)}")
    print(f"  of those, FDV resolvable via CMC:                      "
          f"{len(screenable) - len(no_fdv)}/{len(screenable)}")
    print(f"NOT screenable (no Binance perp — spot/delisted/misread/Bybit-only): {len(no_perp)}")

    if no_perp:
        print(f"\n--- {len(no_perp)} symbols with NO Binance USDT perp (check / correct) ---")
        for s in no_perp:
            sug = get_close_matches(s, perp_bases, n=3, cutoff=0.6)
            # also prefix matches (BROCCOLI -> BROCCOLI714)
            pre = sorted(p for p in perp_bases if p.startswith(s) and p != s)[:3]
            hint = ", ".join(dict.fromkeys(pre + sug)) or "(no close Binance ticker)"
            secs = "/".join(wl[s]["sections"]) or "/".join(wl[s]["sources"])
            print(f"  {s:14} [{secs}]  -> maybe: {hint}")
    if no_fdv:
        print(f"\n--- {len(no_fdv)} screenable but no CMC slug (FDV gate will skip) ---")
        print("  " + ", ".join(no_fdv))


if __name__ == "__main__":
    main()

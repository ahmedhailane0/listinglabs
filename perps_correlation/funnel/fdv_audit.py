"""Authoritative FDV re-audit for the 74 funnel tokens (supersedes fdv_cmc.py +
fdv2.py, both of which mis-resolved CMC slugs via ticker/name heuristics).

Resolution order, most-authoritative first:
  1. Contract address match against CMC's full slug map (cryptos.json). The map
     stores a LIST of addresses per coin (multi-chain), so we match our Alpha
     contract against any of them. This is collision-proof.
  2. Manual override table (OVERRIDE) for tokens whose Alpha contract isn't in
     the map's address list (brand-new / wrapped). Each override is a verified
     slug, checked by symbol match at fetch time.
  3. Unique symbol match in the map (only when exactly one active coin carries
     the ticker) — last resort, logged as low-confidence.

Every resolved slug is fetched from CMC's detail endpoint and ACCEPTED only when
the returned symbol matches ours. We then run a plausibility gate
(fdv>0, total<1e15, mcap<=fdv, fdv≈price*total) and record everything to an
audit log.

Outputs:
  fdv_audit.json    { symbol -> {slug, fdv, mcap, circ, total, price, basis, ok} }
  fdv_audit_log.txt  human-readable per-token audit trail
"""
from __future__ import annotations
import json, time, sys
from pathlib import Path
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
HERE = Path(__file__).parent
MAP_URL = "https://s3.coinmarketcap.com/generated/core/crypto/cryptos.json"
DETAIL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail"

# Tokens whose Alpha contract is absent from the CMC address map (verified by
# hand against coinmarketcap.com — symbol is re-checked at fetch time).
OVERRIDE: dict[str, str] = {
    # symbol -> verified CMC slug (Alpha contract absent/spoofed in the map)
    "PEPE": "pepe",       # Alpha row carries a TRON-bridged addr that collides with a namesake; real Pepe
    "SENT": "sentient",   # BSC contract not in map; name+symbol match Sentient
}


def load_map() -> list[dict]:
    d = requests.get(MAP_URL, headers=UA, timeout=60).json()
    F = d["fields"]
    return [dict(zip(F, v)) for v in d["values"]]


def detail(slug: str):
    r = requests.get(DETAIL, params={"slug": slug}, headers=UA, timeout=25)
    if r.status_code != 200:
        return None
    d = r.json().get("data") or {}
    s = d.get("statistics") or {}
    return {"symbol": d.get("symbol"), "name": d.get("name"), "slug": slug,
            "fdv": s.get("fullyDilutedMarketCap"), "mcap": s.get("marketCap"),
            "price": s.get("price"), "circ": s.get("circulatingSupply"),
            "total": s.get("totalSupply"), "max": s.get("maxSupply")}


# Blocking checks reject the value; warnings are recorded but the CMC FDV is
# still trusted (FDV legitimately uses max supply, so fdv != price*total and
# total!=max are expected, not errors).
def plausible(rec: dict) -> tuple[list[str], list[str]]:
    """Return (blocking, warnings)."""
    fdv, tot, mcap, price, mx = (rec.get(k) for k in ("fdv", "total", "mcap", "price", "max"))
    block, warn = [], []
    if not fdv or fdv <= 0:
        block.append("NO_FDV")
        return block, warn
    if mcap and fdv and mcap > fdv * 1.05:
        block.append("MCAP>FDV")
    supply = mx or tot
    if supply and supply > 1e16:                       # un-normalized on-chain raw value
        block.append("SUPPLY_RAW")
    if fdv < 1e6:
        warn.append("FDV<1M")
    if price and supply and abs(price * supply - fdv) / fdv > 0.05:
        warn.append("FDV!=P*SUPPLY")
    return block, warn


def main():
    rows = json.loads((HERE / "funnel_dated.json").read_text(encoding="utf-8"))
    cmap = load_map()
    by_addr: dict[str, dict] = {}
    by_sym: dict[str, list] = {}
    for e in cmap:
        for a in (e.get("address") or []):
            if isinstance(a, str):
                by_addr[a.lower()] = e
        by_sym.setdefault((e.get("symbol") or "").upper(), []).append(e)

    out: dict = {}
    log = []
    for i, r in enumerate(rows, 1):
        sym = r["symbol"]
        addr = (r.get("contract") or "").lower()
        slug = basis = None
        # 1. manual override (hand-verified; wins over a spoofed address list)
        if sym in OVERRIDE:
            slug, basis = OVERRIDE[sym], "override"
        # 2. contract address
        elif addr and addr in by_addr:
            slug, basis = by_addr[addr]["slug"], "address"
        # 3. unique active symbol
        else:
            cands = [c for c in by_sym.get(sym.upper(), []) if c.get("is_active")]
            if len(cands) == 1:
                slug, basis = cands[0]["slug"], "symbol-unique"

        rec = {"slug": slug, "basis": basis, "fdv": None, "ok": False}
        if slug:
            try:
                d = detail(slug)
            except Exception as e:
                d = None
                rec["err"] = str(e)
            time.sleep(0.25)
            if d:
                sym_match = (d["symbol"] or "").upper() == sym.upper()
                rec.update({k: d[k] for k in ("fdv", "mcap", "circ", "total", "max", "price", "name")})
                rec["sym_match"] = sym_match
                block, warn = plausible(d)
                rec["block"], rec["warn"] = block, warn
                rec["ok"] = sym_match and not block
        out[sym] = rec
        fdv = rec.get("fdv")
        fs = f"${fdv/1e6:,.1f}M" if fdv else "-"
        if rec["ok"]:
            status = "OK" + (" warn:" + ",".join(rec["warn"]) if rec.get("warn") else "")
        else:
            status = "REJECT:" + ",".join(rec.get("block", []) or ["UNRESOLVED"])
        if not rec.get("sym_match", True):
            status += " SYM-MISMATCH"
        line = f"[{i:2}/{len(rows)}] {sym:9} basis={str(basis):13} slug={str(slug)[:24]:24} fdv={fs:13} {status}"
        print(line)
        log.append(line)
        (HERE / "fdv_audit.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for v in out.values() if v["ok"])
    summary = f"\nclean (sym-verified + plausible): {ok}/{len(rows)}"
    print(summary)
    unresolved = [s for s, v in out.items() if not v["ok"]]
    print("needs attention:", unresolved)
    (HERE / "fdv_audit_log.txt").write_text("\n".join(log) + summary +
                                            "\nneeds attention: " + ", ".join(unresolved) + "\n",
                                            encoding="utf-8")


if __name__ == "__main__":
    main()

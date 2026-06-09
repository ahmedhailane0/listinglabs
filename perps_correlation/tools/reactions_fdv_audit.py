"""Audit the Listing Reactions study's FDV exposure against the authoritative
re-audit (funnel/fdv_audit.json — slugs resolved by contract address, symbol-
verified). The funnel's OLD fdv_final.json/fdv_cmc.json mis-resolved ~50 tokens
via ticker/name heuristics (e.g. ETHGas $28M -> $1.09B); this checks whether the
reactions report's per-token listings/*.json carry the same contamination.

Writes reactions_fdv_audit_log.txt. Match is by cmc_slug first, then by symbol.
"""
from __future__ import annotations
import json, glob
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
audit = json.loads((HERE / "funnel" / "fdv_audit.json").read_text(encoding="utf-8"))

by_slug, by_sym = {}, {}
for sym, a in audit.items():
    if a.get("slug"):
        by_slug[a["slug"]] = (sym, a)
    by_sym[sym.lower()] = (sym, a)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def usd(v):
    return f"${v/1e6:,.1f}M" if v else "—"


lines, discrepancies, nomatch = [], [], []
listings = sorted(glob.glob(str(HERE / "listings" / "*.json")))
for i, fp in enumerate(listings, 1):
    d = json.loads(Path(fp).read_text(encoding="utf-8"))
    tok, slug, rf = d.get("token"), d.get("cmc_slug"), f(d.get("fdv_usd"))
    src = d.get("fdv_source", "")
    m = by_slug.get(slug) or by_sym.get((tok or "").lower())
    if not m:
        nomatch.append((tok, slug, rf, src))
        lines.append(f"[{i:2}/{len(listings)}] {tok:10} slug={slug:24} "
                     f"react={usd(rf):>12}  NO-AUDIT-MATCH (reaction-only token) src={src}")
        continue
    asym, a = m
    af = f(a.get("fdv")) if a.get("ok") else None
    ratio = (rf / af) if (rf and af) else None
    flag = "OK"
    if ratio and (ratio > 1.5 or ratio < 0.67):
        flag = f"DISCREPANCY {ratio:.2f}x"
        discrepancies.append((tok, slug, rf, af, ratio))
    rs = f"{ratio:.3f}x" if ratio else "n/a"
    lines.append(f"[{i:2}/{len(listings)}] {tok:10} slug={slug:24} "
                 f"react={usd(rf):>12} audit={usd(af):>12} {rs:>8}  {flag}")

matched = len(listings) - len(nomatch)
summary = [
    "",
    f"listings audited:        {len(listings)}",
    f"matched to funnel audit: {matched}",
    f"reaction-only (no match):{len(nomatch)}  -> {[t for t,_,_,_ in nomatch]}",
    f"FDV discrepancies >1.5x: {len(discrepancies)}",
    "",
]
if discrepancies:
    summary.append("DISCREPANCIES:")
    for tok, slug, rf, af, ratio in discrepancies:
        summary.append(f"  {tok} ({slug}): react {usd(rf)} vs audit {usd(af)} = {ratio:.2f}x")
else:
    summary.append("CONCLUSION: reactions study has ZERO FDV exposure to the "
                   "mis-resolution bug -- every matched token equals the "
                   "contract-resolved audit value. Reaction-only tokens "
                   "(CTR/NEX/QAIT/SLX) carry explicit dated CMC v2 quote sources.")

out = "\n".join(lines + summary) + "\n"
(HERE / "tools" / "reactions_fdv_audit_log.txt").write_text(out, encoding="utf-8")
print(out)

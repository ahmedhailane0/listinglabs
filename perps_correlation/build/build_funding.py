"""Build the per-token funding map the report renders: amount + who funded.

Merges two sources into `cache/funding.json`:
  - RootData (`cache/rootdata.json`): item.total_funding + item.investors[].
  - Excel `CEX_Listings_Tracker_2026 (1).xlsx`: `Total Funding (USD)` as an
    amount-only fallback when RootData has no number.

Coverage: RootData gives both amount AND investors; the Excel only an amount.
Tokens with neither render as an em dash in the report.

    python build_funding.py            # offline: read caches only (used by build_all)
    python build_funding.py --fetch    # also hit RootData for tokens still missing
                                        # (needs ROOTDATA_API_KEY in the secrets.env
                                        #  read by fetch_rootdata.py)

Output: cache/funding.json
    { "TICKER": {"amount": float|None, "source": "rootdata"|"excel"|None,
                 "investors": [{"name", "url", "lead": bool}]} }
Prints the tokens it could NOT fill so they can be added manually.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
LISTINGS = HERE / "listings"
ROOTDATA = HERE.parent / "cache" / "rootdata.json"
OUT = HERE.parent / "cache" / "funding.json"
XLSX = Path(r"C:\Users\PC\Downloads\CEX_Listings_Tracker_2026 (1).xlsx")


def _report_tokens() -> dict[str, str]:
    """TICKER -> project name, for every token in the reactions report."""
    out = {}
    for p in sorted(LISTINGS.glob("*.json")):
        cfg = json.loads(p.read_text(encoding="utf-8"))
        out[cfg["token"].upper()] = cfg.get("name") or cfg["token"]
    return out


def _excel_amounts() -> dict[str, float]:
    """TICKER -> Total Funding (USD) parsed from any sheet (first numeric wins)."""
    try:
        import openpyxl
    except ImportError:
        print("  (openpyxl missing — skipping Excel fallback)")
        return {}
    if not XLSX.exists():
        print(f"  (Excel not found at {XLSX} — skipping fallback)")
        return {}
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    amounts: dict[str, float] = {}
    for ws in wb.worksheets:
        for r in ws.iter_rows(min_row=3, values_only=True):
            if not r or len(r) < 7 or not r[1]:
                continue
            sym = str(r[1]).upper().strip()
            val = r[6]
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue  # '-' or blank
            if f > 0 and sym not in amounts:
                amounts[sym] = f
    return amounts


def _investors_from_item(item: dict) -> list[dict]:
    out = []
    for inv in item.get("investors") or []:
        if not isinstance(inv, dict):
            continue
        name = inv.get("name")
        if not name:
            continue
        url = inv.get("X") or inv.get("rootdataurl") or ""
        out.append({"name": name, "url": url, "lead": bool(inv.get("lead_investor"))})
    # lead investors first, then preserve order
    out.sort(key=lambda x: not x["lead"])
    return out


def main():
    do_fetch = "--fetch" in sys.argv
    tokens = _report_tokens()
    rd = json.loads(ROOTDATA.read_text(encoding="utf-8")) if ROOTDATA.exists() else {}

    if do_fetch:
        missing = [t for t in tokens if not (rd.get(t, {}).get("item"))]
        if missing:
            print(f"--fetch: querying RootData for {len(missing)} missing tokens...")
            try:
                from fetch import fetch_rootdata as frd
                hdrs = frd.headers(frd.load_key())
                for t in missing:
                    hit = frd.search(tokens[t], t, hdrs)
                    if not hit:
                        rd[t] = {"project_id": None, "search_hit": None, "item": None, "name": tokens[t]}
                        continue
                    pid = hit.get("id")
                    item = frd.get_item(pid, hdrs) if pid else None
                    rd[t] = {"project_id": pid, "search_hit": hit, "item": item, "name": tokens[t]}
                    print(f"  {t}: pid={pid} funding={(item or {}).get('total_funding')}")
                ROOTDATA.write_text(json.dumps(rd, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"  RootData fetch failed ({e}); continuing offline.")

    excel = _excel_amounts()
    funding: dict[str, dict] = {}
    unfilled = []
    for t in sorted(tokens):
        item = (rd.get(t) or {}).get("item") or {}
        amount = item.get("total_funding")
        investors = _investors_from_item(item)
        source = "rootdata" if amount else None
        if not amount and t in excel:           # Excel amount-only fallback
            amount, source = excel[t], "excel"
        funding[t] = {"amount": amount, "source": source, "investors": investors}
        if not amount and not investors:
            unfilled.append(t)

    OUT.write_text(json.dumps(funding, indent=2, ensure_ascii=False), encoding="utf-8")
    have_amt = sum(1 for v in funding.values() if v["amount"])
    have_inv = sum(1 for v in funding.values() if v["investors"])
    print(f"\nwrote {OUT}")
    print(f"  {have_amt}/{len(tokens)} have a funding amount, {have_inv}/{len(tokens)} have investors")
    print(f"  {len(unfilled)} unfilled (no amount, no investors) — add manually if you have data:")
    print("   ", ", ".join(unfilled) or "none")


if __name__ == "__main__":
    main()

"""Clean enriched.csv into enriched_clean.csv.

Cleaning rules applied (each one is logged so you can audit):
  1. RootData total_funding == 0  ->  null  (RD often returns 0 for "unknown")
  2. CryptoRank slug mismatch     ->  null out all cr_* columns
     (compare CR coin.name vs input project name with difflib; threshold 0.55)
  3. RootData project mismatch    ->  null out all rd_* columns
     (compare RD project_name vs input project name; threshold 0.55)
  4. Drop rows with no kline data (ret_7d null) — perp never really traded
  5. Drop rows with day-1 USD volume < $100,000 — too thin to be a real launch
  6. establishment_date normalized to int year (drop month/day if present)
  7. Cap vol_decay_ratio outliers: values > 10 are recorded but flagged

Outputs:
  enriched_clean.csv   — cleaned data
  clean_log.txt        — what was changed, row by row
"""
import csv
import json
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
CACHE = ROOT / "cache"

INPUT = HERE / "enriched.csv"
OUTPUT = HERE / "enriched_clean.csv"
LOG = HERE / "clean_log.txt"

MIN_VOLUME_USD = 100_000
NAME_MATCH_THRESHOLD = 0.55


def name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def main():
    cryptorank = json.loads((CACHE / "cryptorank.json").read_text(encoding="utf-8"))
    rootdata = json.loads((CACHE / "rootdata.json").read_text(encoding="utf-8"))

    cr_columns = ["cr_slug", "cr_ico_raised_usd", "cr_has_funding_rounds",
                  "cr_investor_count", "cr_tier1_count", "cr_lead_count", "cr_fdv"]
    rd_columns = ["rd_project_id", "rd_total_funding_usd", "rd_investor_count",
                  "rd_lead_count", "rd_rt_score", "rd_establishment_date"]

    log_lines = []
    rows_in = list(csv.DictReader(INPUT.open(encoding="utf-8")))
    rows_out = []
    counts = {
        "rd_zero_to_null": 0,
        "cr_mismatch_nulled": 0,
        "rd_mismatch_nulled": 0,
        "dropped_no_klines": 0,
        "dropped_low_volume": 0,
        "estab_date_normalized": 0,
        "vol_decay_flagged": 0,
    }

    for row in rows_in:
        ticker = row["ticker"]
        project = row["project"]

        # 4. Drop rows with no kline data
        if not row.get("ret_7d"):
            counts["dropped_no_klines"] += 1
            log_lines.append(f"DROP no-klines: {project} ({ticker})")
            continue

        # 5. Drop rows with thin day-1 volume
        try:
            vol_d1 = float(row.get("vol_d1_usd") or 0)
        except ValueError:
            vol_d1 = 0
        if vol_d1 < MIN_VOLUME_USD:
            counts["dropped_low_volume"] += 1
            log_lines.append(f"DROP low-volume: {project} ({ticker}) vol_d1=${vol_d1:,.0f}")
            continue

        # 1. RootData 0 -> null
        if row.get("rd_total_funding_usd") in ("0", "0.0"):
            row["rd_total_funding_usd"] = ""
            counts["rd_zero_to_null"] += 1
            log_lines.append(f"  rd_total_funding 0->null: {project} ({ticker})")

        # 2. CryptoRank slug verification
        cr_entry = cryptorank.get(ticker) or {}
        cr_name = ((cr_entry.get("coin") or {}).get("name") or "")
        if row.get("cr_slug") and cr_name:
            sim = name_similarity(project, cr_name)
            if sim < NAME_MATCH_THRESHOLD:
                for c in cr_columns:
                    row[c] = ""
                counts["cr_mismatch_nulled"] += 1
                log_lines.append(
                    f"  CR mismatch nulled: {project} ({ticker}) -> CR said '{cr_name}' "
                    f"(sim={sim:.2f})")

        # 3. RootData project verification — token_symbol is the strong signal
        rd_entry = rootdata.get(ticker) or {}
        rd_item = rd_entry.get("item") or {}
        rd_name = rd_item.get("project_name") or ""
        rd_symbol = (rd_item.get("token_symbol") or "").upper()
        if row.get("rd_project_id") and rd_name:
            symbol_match = rd_symbol == ticker.upper()
            sim = name_similarity(project, rd_name)
            # accept if EITHER token_symbol matches OR names are similar
            if not symbol_match and sim < NAME_MATCH_THRESHOLD:
                for c in rd_columns:
                    row[c] = ""
                counts["rd_mismatch_nulled"] += 1
                log_lines.append(
                    f"  RD mismatch nulled: {project} ({ticker}) -> RD said "
                    f"'{rd_name}' (sym='{rd_symbol}', sim={sim:.2f})")

        # 6. establishment_date -> year only
        ed = row.get("rd_establishment_date") or ""
        if ed and len(ed) > 4:
            try:
                row["rd_establishment_date"] = str(int(float(ed[:4])))
                counts["estab_date_normalized"] += 1
            except ValueError:
                pass

        # 7. flag wild vol_decay
        try:
            vd = float(row.get("vol_decay_ratio") or 0)
            if vd > 10:
                counts["vol_decay_flagged"] += 1
                log_lines.append(f"  vol_decay > 10: {project} ({ticker}) = {vd:.1f}")
        except ValueError:
            pass

        rows_out.append(row)

    with OUTPUT.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=rows_in[0].keys())
        w.writeheader()
        w.writerows(rows_out)

    LOG.write_text(
        "Cleaning summary:\n"
        + "\n".join(f"  {k}: {v}" for k, v in counts.items())
        + f"\n\nrows in : {len(rows_in)}\nrows out: {len(rows_out)}\n\n"
        + "Details:\n" + "\n".join(log_lines),
        encoding="utf-8",
    )

    print(f"wrote {len(rows_out)} rows -> {OUTPUT}")
    print(f"log    -> {LOG}")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

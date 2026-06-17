"""Single source of truth: rebuild the entire Listinglabs site in one command.

Everything lives under ONE folder — `perps_correlation/Listinglabs/`:
    Listinglabs/index.html            landing page (two sections)
    Listinglabs/report/               Listing Reactions report
    Listinglabs/funnel/report/        Listing Funnel report (+ charts/)

There are no separate `report/` or `share/` copies anymore — the builders
write straight into Listinglabs/, so what you see is always the current build.

    python build_all.py            # rebuild reactions + funnel + landing, then zip
    python build_all.py --no-zip   # skip the deploy zip

The zip (`Listinglabs.zip`) is what you upload to Netlify.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SITE = HERE / "Listinglabs"
ZIP_BASE = HERE / "Listinglabs"            # -> Listinglabs.zip

REACTIONS_N = len(list((HERE / "listings").glob("*.json")))
FUNNEL_N = len(json.loads((HERE / "funnel" / "funnel_master.json").read_text(encoding="utf-8")))
try:
    SCAMS_N = len(json.loads((HERE.parent / "cache" / "scam_data.json").read_text(encoding="utf-8")))
except Exception:
    SCAMS_N = None
try:
    SCREENER_N = json.loads((HERE.parent / "cache" / "screener" / "screener.json")
                            .read_text(encoding="utf-8")).get("counts", {}).get("screenable")
except Exception:
    SCREENER_N = None

# One favicon for the whole site, written to Listinglabs/favicon.svg; every page
# references it relatively (report/: ../favicon.svg, funnel/report/: ../../…).
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#1f4e79"/>'
    '<polyline points="10,44 22,34 30,40 42,20 54,28" fill="none" stroke="#fff" '
    'stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="42" cy="20" r="4.5" fill="#e67e22"/></svg>')


def landing() -> str:
    from datetime import datetime, timezone
    from build.build_listing_report import page_meta, THEME_VARS, theme_toggle_button, THEME_JS
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    scam_n = f"{SCAMS_N} tokens" if SCAMS_N else "tracker"
    meta = page_meta(
        "ListingLabs — Binance Alpha listing studies",
        "How Binance-Alpha-listed tokens react to exchange listings: reaction "
        "charts, the CEX → Korea funnel, and a manipulated-token watchlist. "
        "Self-updating every ~20 minutes.",
        favicon_rel="favicon.svg")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}
<title>Binance Alpha — Listing Studies</title><style>{THEME_VARS}
*{{box-sizing:border-box}}body{{font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--text)}}
header{{padding:28px;background:var(--header-bg);color:var(--header-fg);display:flex;align-items:flex-start}}header h1{{margin:0;font-size:22px}}header p{{margin:6px 0 0;opacity:.85}}
.htext{{flex:1 1 auto}}
.wrap{{max-width:920px;margin:0 auto;padding:28px}}
.cards{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr))}}
.card{{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:22px;text-decoration:none;color:inherit;display:block;transition:.15s;min-width:0}}
.card:hover{{border-color:var(--primary);box-shadow:0 4px 18px rgba(31,78,121,.12)}}
.card h2{{margin:0 0 4px;font-size:18px;color:var(--primary)}}
.card .n{{font-size:13px;color:var(--text-3);font-weight:600}}
.card p{{margin:10px 0 0;font-size:13.5px;color:var(--text-2)}}
.go{{margin-top:14px;display:inline-block;font-size:13px;font-weight:600;color:var(--link)}}
footer{{max-width:920px;margin:0 auto;padding:0 28px 36px;color:var(--text-4);font-size:12.5px}}
img,svg{{max-width:100%;height:auto}}
.theme-toggle{{margin-left:auto;font-size:16px;line-height:1;cursor:pointer;background:rgba(255,255,255,.1);color:var(--header-fg);border:1px solid rgba(255,255,255,.35);border-radius:999px;width:32px;height:32px;padding:0;flex:0 0 auto}}
.theme-toggle:hover{{background:rgba(255,255,255,.2)}}
@media(max-width:640px){{header{{padding:18px 16px}}header h1{{font-size:19px}}.wrap{{padding:18px 16px}}footer{{padding:0 16px 28px}}}}
</style></head><body>
<header><div class="htext"><h1>Binance Alpha — Listing Studies</h1>
<p>Two views of the same Alpha-listed token set, under one roof.</p></div>{theme_toggle_button()}</header>
<div class="wrap"><div class="cards">
  <a class="card" href="report/index.html">
    <h2>Binance Alpha &amp; Perps</h2><span class="n">{REACTIONS_N} tokens</span>
    <p>Per-token price-reaction charts annotated with every venue listing —
       Alpha, Binance Perp, Coinbase, the Korean exchanges, plus full
       OKX / Bybit / Kraken / KuCoin / Bitget / Gate spot &amp; perp coverage.
       Filter the grid by which venues a token reached.</p>
    <span class="go">Open reactions →</span>
  </a>
  <a class="card" href="funnel/report/index.html">
    <h2>CEX → Korea</h2><span class="n">{FUNNEL_N} tokens</span>
    <p>Alpha → Binance Perp → Coinbase → Korea progression with timing gaps,
       FDV-at-listing, and open interest for the tokens that completed the
       funnel.</p>
    <span class="go">Open funnel →</span>
  </a>
  <a class="card" href="scams/index.html">
    <h2>Manipulated</h2><span class="n">{f"{SCREENER_N} coins" if SCREENER_N else scam_n}</span>
    <p>Manipulated-coin perp screener: combined Binance&nbsp;+&nbsp;Bybit open
       interest vs FDV, funding, and Buy v1/v2/v3 accumulation &amp; washout
       signals — filter by signal, OI, gate and FDV. Curated coins also carry
       price charts, holders &amp; memos.</p>
    <span class="go">Open screener →</span>
  </a>
</div></div>
<footer>Updated {stamp} UTC · rebuilds every ~20 min. Reactions filter backfilled with
daily-resolution earliest-candle listing dates across all major CEX venues; per-token
current open interest from CoinMarketCap.</footer>
{THEME_JS}
</body></html>"""


def _run(script: Path):
    """Run a builder script with this interpreter; abort the whole build on error."""
    print(f"  - {script.relative_to(HERE)}", flush=True)
    subprocess.run([sys.executable, str(script)], check=True)


def main():
    SITE.mkdir(parents=True, exist_ok=True)

    print("building reports into Listinglabs/ ...", flush=True)
    _run(HERE / "fetch" / "fetch_bwenews.py")                  # cache/bwenews_signals.json (RSS poll; never fails build)
    _run(HERE / "build" / "apply_signals.py")                  # fold new venue signals into listings/*.json
    _run(HERE / "build" / "build_funding.py")                  # cache/funding.json (offline merge)
    _run(HERE / "build" / "build_listing_report.py")          # -> Listinglabs/report
    _run(HERE / "funnel" / "funnel_report.py")       # -> Listinglabs/funnel/report
    _run(HERE / "build" / "build_scams.py")                    # -> Listinglabs/scams (now incl. the perp screener)

    (SITE / "index.html").write_text(landing(), encoding="utf-8")
    (SITE / "favicon.svg").write_text(FAVICON_SVG, encoding="utf-8")
    print(f"wrote landing: reactions={REACTIONS_N}, funnel={FUNNEL_N}", flush=True)

    if "--no-zip" not in sys.argv:
        zip_path = ZIP_BASE.with_suffix(".zip")
        if zip_path.exists():
            zip_path.unlink()
        shutil.make_archive(str(ZIP_BASE), "zip", SITE)
        print(f"wrote {zip_path}", flush=True)


if __name__ == "__main__":
    main()

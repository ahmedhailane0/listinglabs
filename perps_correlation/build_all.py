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


def landing() -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Binance Alpha — Listing Studies</title><style>
*{{box-sizing:border-box}}body{{font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f6f9;color:#1d2733}}
header{{padding:28px;background:#1f4e79;color:#fff}}header h1{{margin:0;font-size:22px}}header p{{margin:6px 0 0;opacity:.85}}
.wrap{{max-width:920px;margin:0 auto;padding:28px}}
.cards{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr))}}
.card{{background:#fff;border:1px solid #e1e7ee;border-radius:12px;padding:22px;text-decoration:none;color:inherit;display:block;transition:.15s;min-width:0}}
.card:hover{{border-color:#1f4e79;box-shadow:0 4px 18px rgba(31,78,121,.12)}}
.card h2{{margin:0 0 4px;font-size:18px;color:#1f4e79}}
.card .n{{font-size:13px;color:#6b7785;font-weight:600}}
.card p{{margin:10px 0 0;font-size:13.5px;color:#42505e}}
.go{{margin-top:14px;display:inline-block;font-size:13px;font-weight:600;color:#1f6fb2}}
footer{{max-width:920px;margin:0 auto;padding:0 28px 36px;color:#8a96a3;font-size:12.5px}}
img,svg{{max-width:100%;height:auto}}
@media(max-width:640px){{header{{padding:18px 16px}}header h1{{font-size:19px}}.wrap{{padding:18px 16px}}footer{{padding:0 16px 28px}}}}
</style></head><body>
<header><h1>Binance Alpha — Listing Studies</h1>
<p>Two views of the same Alpha-listed token set, under one roof.</p></header>
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
    <h2>Scam Watchlist</h2><span class="n">tracker</span>
    <p>Hand-maintained watchlist of tokens with notes on FDV behaviour
       (sustained &gt;$1B, brief spikes, uptrends). Sortable, with an
       FDV&nbsp;&gt;&nbsp;$1B filter.</p>
    <span class="go">Open watchlist →</span>
  </a>
</div></div>
<footer>Built by build_all.py — single-folder site. Reactions filter backfilled with
daily-resolution earliest-candle listing dates across all major CEX venues; per-token
current open interest from CoinMarketCap.</footer>
</body></html>"""


def _run(script: Path):
    """Run a builder script with this interpreter; abort the whole build on error."""
    print(f"  - {script.relative_to(HERE)}", flush=True)
    subprocess.run([sys.executable, str(script)], check=True)


def main():
    SITE.mkdir(parents=True, exist_ok=True)

    print("building reports into Listinglabs/ ...", flush=True)
    _run(HERE / "fetch_bwenews.py")                  # cache/bwenews_signals.json (RSS poll; never fails build)
    _run(HERE / "apply_signals.py")                  # fold new venue signals into listings/*.json
    _run(HERE / "build_funding.py")                  # cache/funding.json (offline merge)
    _run(HERE / "build_listing_report.py")          # -> Listinglabs/report
    _run(HERE / "funnel" / "funnel_report.py")       # -> Listinglabs/funnel/report
    _run(HERE / "build_scams.py")                    # -> Listinglabs/scams

    (SITE / "index.html").write_text(landing(), encoding="utf-8")
    print(f"wrote landing: reactions={REACTIONS_N}, funnel={FUNNEL_N}", flush=True)

    if "--no-zip" not in sys.argv:
        zip_path = ZIP_BASE.with_suffix(".zip")
        if zip_path.exists():
            zip_path.unlink()
        shutil.make_archive(str(ZIP_BASE), "zip", SITE)
        print(f"wrote {zip_path}", flush=True)


if __name__ == "__main__":
    main()

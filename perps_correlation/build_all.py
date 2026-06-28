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
*{{box-sizing:border-box}}body{{font:15px/1.6 var(--font-sans);margin:0;background:var(--bg);color:var(--text);
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}}
a{{color:inherit;text-decoration:none}}
:focus-visible{{outline:none;box-shadow:var(--ring);border-radius:var(--radius-sm)}}
@media (prefers-reduced-motion: reduce){{*,*::before,*::after{{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}}}}
/* hero */
.hero{{background:var(--header-grad);color:var(--header-fg);box-shadow:var(--shadow-md);
  position:relative;overflow:hidden}}
.hero::after{{content:"";position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(120% 140% at 85% -20%,rgba(127,196,255,.16),transparent 60%)}}
.hero-in{{max-width:1080px;margin:0 auto;padding:40px 28px 34px;position:relative;z-index:1}}
.hero-top{{display:flex;align-items:flex-start;gap:16px}}
.brand{{display:flex;align-items:center;gap:11px;flex:1 1 auto}}
.brand .mark{{width:38px;height:38px;flex:0 0 auto;border-radius:10px;
  background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);
  display:grid;place-items:center}}
.brand .mark svg{{width:22px;height:22px;display:block}}
.eyebrow{{font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;opacity:.72;font-weight:600}}
h1{{margin:2px 0 0;font-size:27px;font-weight:750;letter-spacing:-.02em;line-height:1.15}}
.lede{{margin:12px 0 0;max-width:640px;font-size:15.5px;opacity:.9;line-height:1.55}}
.live{{display:inline-flex;align-items:center;gap:7px;margin-top:16px;font-size:12.5px;
  font-weight:600;opacity:.92}}
.live .dot{{width:8px;height:8px;border-radius:50%;background:#3fc77a;
  box-shadow:0 0 0 0 rgba(63,199,122,.6);animation:pulse 2.4s var(--ease) infinite}}
@keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(63,199,122,.55)}}70%{{box-shadow:0 0 0 7px rgba(63,199,122,0)}}100%{{box-shadow:0 0 0 0 rgba(63,199,122,0)}}}}
/* body */
.wrap{{max-width:1080px;margin:0 auto;padding:30px 28px 8px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:0 0 26px}}
.stat{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px 16px;box-shadow:var(--shadow-sm)}}
.stat .v{{font-size:23px;font-weight:750;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.stat .k{{font-size:12px;color:var(--text-3);margin-top:2px}}
.cards{{display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(min(100%,288px),1fr))}}
.card{{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);
  padding:24px;color:inherit;display:flex;flex-direction:column;min-width:0;
  box-shadow:var(--shadow-sm);transition:transform .16s var(--ease),box-shadow .16s var(--ease),border-color .16s var(--ease)}}
.card:hover{{transform:translateY(-3px);border-color:var(--primary);box-shadow:var(--shadow-lg)}}
.card .ic{{width:42px;height:42px;border-radius:11px;display:grid;place-items:center;margin-bottom:14px;
  background:var(--bg-chip);color:var(--primary);border:1px solid var(--border)}}
.card .ic svg{{width:23px;height:23px;display:block}}
.card h2{{margin:0;font-size:18px;font-weight:700;letter-spacing:-.01em;
  display:flex;align-items:baseline;gap:9px}}
.card h2 .n{{font-size:12.5px;color:var(--text-3);font-weight:600;letter-spacing:0}}
.card p{{margin:11px 0 0;font-size:13.5px;color:var(--text-2);line-height:1.55}}
.go{{margin-top:auto;padding-top:18px;display:inline-flex;align-items:center;gap:6px;font-size:13.5px;font-weight:650;
  color:var(--link)}}
.go .arw{{transition:transform .16s var(--ease)}}
.card:hover .go .arw{{transform:translateX(4px)}}
footer{{max-width:1080px;margin:0 auto;padding:26px 28px 40px;color:var(--text-4);font-size:12.5px;line-height:1.6}}
img,svg{{max-width:100%;height:auto}}
.theme-toggle{{font-size:16px;line-height:1;cursor:pointer;background:rgba(255,255,255,.12);color:var(--header-fg);
  border:1px solid rgba(255,255,255,.3);border-radius:999px;width:34px;height:34px;padding:0;flex:0 0 auto}}
.theme-toggle:hover{{background:rgba(255,255,255,.22)}}
@media(max-width:760px){{.stats{{grid-template-columns:1fr 1fr}}}}
@media(max-width:560px){{.hero-in{{padding:30px 16px 26px}}h1{{font-size:23px}}.wrap{{padding:22px 16px 4px}}footer{{padding:22px 16px 30px}}}}
</style></head><body>
<header class="hero"><div class="hero-in">
  <div class="hero-top">
    <div class="brand">
      <span class="mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-5 4 3 8-9"/><path d="M3 21h18"/></svg></span>
      <div><div class="eyebrow">ListingLabs</div>
        <h1>Binance Alpha listing studies</h1></div>
    </div>{theme_toggle_button()}
  </div>
  <p class="lede">How Binance-Alpha-listed tokens react to exchange listings — annotated
     reaction charts, the CEX&nbsp;→&nbsp;Korea funnel, and a manipulated-coin perp screener,
     all under one roof.</p>
  <span class="live"><span class="dot" aria-hidden="true"></span>Self-updating · last refresh {stamp} UTC</span>
</div></header>
<div class="wrap">
  <div class="stats">
    <div class="stat"><div class="v">{REACTIONS_N}</div><div class="k">reaction-charted tokens</div></div>
    <div class="stat"><div class="v">{FUNNEL_N}</div><div class="k">funnel tokens</div></div>
    <div class="stat"><div class="v">{SCREENER_N or SCAMS_N or 0}</div><div class="k">coins screened</div></div>
    <div class="stat"><div class="v">~20<span style="font-size:14px;font-weight:600"> min</span></div><div class="k">rebuild cadence</div></div>
  </div>
  <div class="cards">
  <a class="card" href="report/index.html">
    <span class="ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l3-4 3 2 4-6"/></svg></span>
    <h2>Binance Alpha &amp; Perps <span class="n">{REACTIONS_N} tokens</span></h2>
    <p>Per-token price-reaction charts annotated with every venue listing —
       Alpha, Binance Perp, Coinbase, the Korean exchanges, plus full
       OKX / Bybit / Kraken / KuCoin / Bitget / Gate spot &amp; perp coverage.
       Filter the grid by which venues a token reached.</p>
    <span class="go">Open reactions <span class="arw">→</span></span>
  </a>
  <a class="card" href="funnel/report/index.html">
    <span class="ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h18l-7 8v6l-4 2v-8z"/></svg></span>
    <h2>CEX → Korea <span class="n">{FUNNEL_N} tokens</span></h2>
    <p>Alpha → Binance Perp → Coinbase → Korea progression with timing gaps,
       FDV-at-listing, and open interest for the tokens that completed the
       funnel.</p>
    <span class="go">Open funnel <span class="arw">→</span></span>
  </a>
  <a class="card" href="scams/index.html">
    <span class="ic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7z"/><path d="M12 8v4"/><path d="M12 15.5v.5"/></svg></span>
    <h2>Manipulated <span class="n">{f"{SCREENER_N} coins" if SCREENER_N else scam_n}</span></h2>
    <p>Manipulated-coin perp screener: combined Binance&nbsp;+&nbsp;Bybit open
       interest vs FDV, funding, and Buy v1/v2/v3 accumulation &amp; washout
       signals — filter by signal, OI, gate and FDV. Curated coins also carry
       price charts, holders &amp; memos.</p>
    <span class="go">Open screener <span class="arw">→</span></span>
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


def _run_soft(script: Path, *args: str):
    """Run a best-effort network step that must NEVER break the build — a flaky/
    blocked endpoint just leaves its work for the next run."""
    print(f"  - {script.relative_to(HERE)} (best-effort)", flush=True)
    try:
        subprocess.run([sys.executable, str(script), *args], check=False, timeout=180)
    except Exception as e:                                # pragma: no cover
        print(f"    (skipped: {e})", flush=True)


def main():
    SITE.mkdir(parents=True, exist_ok=True)

    print("building reports into Listinglabs/ ...", flush=True)
    _run(HERE / "fetch" / "fetch_bwenews.py")                  # cache/bwenews_signals.json (RSS poll; never fails build)
    _run(HERE / "build" / "apply_signals.py")                  # fold new venue signals into listings/*.json (auto-lists new Binance tokens)
    _run_soft(HERE / "fetch" / "enrich_autolisted.py")         # resolve chain/contract/pool/FDV for auto-listed tokens (best-effort)
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

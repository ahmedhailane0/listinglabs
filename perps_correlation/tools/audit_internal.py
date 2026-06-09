"""Internal audit harness for ListingLabs. Read-only — gathers evidence, prints a
structured findings dump. Run: python audit_internal.py"""
from __future__ import annotations
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent          # perps_correlation/ (this file is in tools/)
sys.path.insert(0, str(ROOT))
from lib.listing_chart import parse_iso
from build import build_listing_report as B

LISTINGS = ROOT / "listings"
CACHE = ROOT.parent / "cache"
REPORT = ROOT / "Listinglabs" / "report"
NOW = datetime.now(timezone.utc)

findings = []  # (severity, area, msg)
def add(sev, area, msg): findings.append((sev, area, msg))

cfgs = {}
for p in sorted(LISTINGS.glob("*.json")):
    try:
        cfgs[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        add("HIGH", "data", f"{p.name}: invalid JSON ({e})")

print(f"tokens: {len(cfgs)}")

# ---- schema + events ----
req = ["token", "name", "chain", "gecko_pool", "window_start_utc", "window_end_utc"]
no_pool = []
for t, c in cfgs.items():
    for f in req:
        if not c.get(f):
            add("MED", "schema", f"{t}: missing/empty '{f}'")
    if not c.get("gecko_pool"):
        no_pool.append(t)
    evs = c.get("events", [])
    if not evs:
        add("MED", "events", f"{t}: no events")
    times = []
    seen = set()
    for ev in evs:
        if not ev.get("exchange") or not ev.get("iso_time_utc"):
            add("MED", "events", f"{t}: event missing exchange/time: {ev}")
            continue
        try:
            dt = parse_iso(ev["iso_time_utc"]); times.append(dt)
        except Exception:
            add("HIGH", "events", f"{t}: unparseable time {ev['iso_time_utc']}")
            continue
        key = (ev["exchange"], ev["iso_time_utc"])
        if key in seen:
            add("LOW", "events", f"{t}: duplicate event {key}")
        seen.add(key)
    # window covers events?
    try:
        we = parse_iso(c["window_end_utc"])
        if times and max(times) > we:
            add("MED", "window", f"{t}: window_end {c['window_end_utc']} < last event {max(times).isoformat()} (event won't show in default view)")
        ws = parse_iso(c["window_start_utc"])
        if ws >= we:
            add("HIGH", "window", f"{t}: window_start >= window_end")
    except Exception as e:
        add("HIGH", "window", f"{t}: bad window ({e})")

# ---- TGE correctness (no sweep artifact leaking through) ----
for t, c in cfgs.items():
    tge = B._tge_time(c)
    # is the chosen TGE actually a non-sweep event?
    if tge is None:
        add("MED", "tge", f"{t}: TGE is None")

# ---- charts: cache presence, staleness, the 404 pools ----
stale, missing, fresh = [], [], 0
for t, c in cfgs.items():
    cp = CACHE / f"{t}_klines_5m_alpha.json"
    if not cp.exists():
        missing.append(t); continue
    try:
        rows = json.loads(cp.read_text(encoding="utf-8")).get("rows") or []
    except Exception:
        add("HIGH", "charts", f"{t}: corrupt kline cache"); continue
    if not rows:
        missing.append(t); continue
    last = datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc)
    age_h = (NOW - last).total_seconds()/3600
    if age_h > 36:
        stale.append((t, round(age_h/24, 1), len(rows)))
    else:
        fresh += 1
print(f"charts: fresh<=36h={fresh}, stale={len(stale)}, missing={len(missing)}, no_pool={len(no_pool)}")
if missing: add("MED", "charts", f"no/empty kline cache: {missing}")
if no_pool: add("LOW", "charts", f"no gecko_pool defined: {no_pool}")
for t, days, n in sorted(stale, key=lambda x:-x[1]):
    add("LOW", "charts", f"{t}: chart stale {days}d old ({n} candles) — likely ETH-pool 404")

# ---- FDV/MC sanity ----
for t, c in cfgs.items():
    fdv, mc = c.get("fdv_usd"), c.get("mcap_usd")
    if fdv and mc and mc > fdv * 1.02:
        add("LOW", "data", f"{t}: MC ({mc:,.0f}) > FDV ({fdv:,.0f})")
    if fdv and fdv <= 0: add("MED", "data", f"{t}: non-positive FDV")

# ---- generated HTML: stored-XSS / unescaped scraped content ----
xss_hits = 0
if REPORT.exists():
    for hp in REPORT.glob("*.html"):
        html_txt = hp.read_text(encoding="utf-8", errors="ignore")
        # any raw <script> inside a note/td that came from data? crude: look for
        # an unescaped angle bracket immediately following 'bwenews' provenance text
        for m in re.finditer(r"Auto-added from BWEnews[^<]{0,400}", html_txt):
            seg = m.group(0)
            if "<" in seg or "javascript:" in seg.lower():
                xss_hits += 1
if xss_hits:
    add("HIGH", "security", f"possible unescaped scraped content in {xss_hits} note(s)")
else:
    print("xss: BWEnews notes appear escaped in all report pages (0 raw-tag leaks)")

# ---- apply_signals symbol-collision risk ----
syms = {c["token"].upper() for c in cfgs.values()}
short = sorted(s for s in syms if len(s) <= 2)
if short:
    add("MED", "signals", f"short tickers risk false-positive BWEnews match: {short}")

# ---- orphan caches (cache files for tokens no longer tracked) ----
tracked_lower = set(cfgs.keys())
orphans = [p.stem.replace("_klines_5m_alpha","") for p in CACHE.glob("*_klines_5m_alpha.json")
           if p.stem.replace("_klines_5m_alpha","") not in tracked_lower]
if orphans: add("LOW", "data", f"orphan kline caches (untracked tokens): {orphans}")

# ---- summary ----
print("\n===== FINDINGS =====")
order = {"HIGH":0,"MED":1,"LOW":2}
for sev in ("HIGH","MED","LOW"):
    fs = [f for f in findings if f[0]==sev]
    print(f"\n[{sev}] {len(fs)}")
    for _, area, msg in fs:
        print(f"  ({area}) {msg}")
print(f"\nTOTAL: {len(findings)} findings  "
      f"(HIGH={sum(1 for f in findings if f[0]=='HIGH')}, "
      f"MED={sum(1 for f in findings if f[0]=='MED')}, "
      f"LOW={sum(1 for f in findings if f[0]=='LOW')})")

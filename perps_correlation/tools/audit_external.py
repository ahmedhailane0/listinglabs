"""External audit: crawl the live GitHub Pages site. Read-only.
Run: python audit_external.py"""
from __future__ import annotations
import json, time, urllib.request, urllib.error, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = "https://ahmedhailane0.github.io/listinglabs"
ROOT = Path(__file__).resolve().parent.parent          # perps_correlation/ (this file is in tools/)
LISTINGS = ROOT / "listings"
LOCAL = ROOT / "Listinglabs"
UA = {"User-Agent": "Mozilla/5.0 listinglabs-audit"}
findings = []
def add(sev, area, msg): findings.append((sev, area, msg))

def fetch(url, method="GET", timeout=30):
    req = urllib.request.Request(url, headers=UA, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read() if method == "GET" else b""
            return r.status, dict(r.headers), body, round((time.time()-t0)*1000)
    except urllib.error.HTTPError as e:
        return e.code, {}, b"", round((time.time()-t0)*1000)
    except Exception as e:
        return None, {"err": str(e)}, b"", round((time.time()-t0)*1000)

tokens = sorted(p.stem for p in LISTINGS.glob("*.json"))
pages = {
    "landing":      f"{BASE}/index.html",
    "report_index": f"{BASE}/report/index.html",
    "funnel_index": f"{BASE}/funnel/report/index.html",
    "scams_index":  f"{BASE}/scams/index.html",
    "style.css":    f"{BASE}/report/style.css",
    "plotly.min.js":f"{BASE}/report/plotly.min.js",
}

print("=== key pages ===")
for name, url in pages.items():
    st, hdr, body, ms = fetch(url)
    kb = len(body)/1024
    ct = hdr.get("Content-Type","?")
    print(f"  {name:14} {st} {kb:8.1f}KB {ms:5}ms  {ct}")
    if st != 200:
        add("HIGH", "availability", f"{name} returned HTTP {st} ({url})")

# --- parity: live report index vs local build ---
st, _, live_body, _ = fetch(pages["report_index"])
lp = LOCAL / "report" / "index.html"
if st == 200 and lp.exists():
    live_h = hashlib.sha256(live_body).hexdigest()[:12]
    loc_h = hashlib.sha256(lp.read_bytes()).hexdigest()[:12]
    print(f"\nparity report/index: live={live_h} local={loc_h} {'MATCH' if live_h==loc_h else 'DIFFER (live is older/newer build)'}")

# --- all 78 detail pages (parallel HEAD) ---
print(f"\n=== {len(tokens)} token detail pages (parallel) ===")
def check(t):
    st, hdr, _, ms = fetch(f"{BASE}/report/{t}.html", method="HEAD")
    return t, st, hdr.get("Content-Length"), ms
bad, sizes = [], []
with ThreadPoolExecutor(max_workers=10) as ex:
    for fut in as_completed([ex.submit(check, t) for t in tokens]):
        t, st, clen, ms = fut.result()
        if st != 200:
            bad.append((t, st)); add("HIGH", "availability", f"detail page {t}.html -> HTTP {st}")
        if clen: sizes.append((t, int(clen)))
print(f"  ok={len(tokens)-len(bad)}/{len(tokens)}  failures={bad if bad else 'none'}")
if sizes:
    sizes.sort(key=lambda x:-x[1])
    big = sizes[0]
    avg = sum(s for _,s in sizes)/len(sizes)/1024
    print(f"  page size: avg={avg:.0f}KB, largest={big[0]} {big[1]/1024:.0f}KB")
    for t, s in sizes:
        if s/1024 > 2000:
            add("MED", "perf", f"{t}.html is {s/1024:.0f}KB (heavy — inline Plotly data)")

# --- security headers on the live host (Pages sets a fixed set) ---
print("\n=== response headers (report index) ===")
_, hdr, _, _ = fetch(pages["report_index"])
for h in ("Content-Type","Cache-Control","X-Content-Type-Options","Strict-Transport-Security","Content-Security-Policy"):
    v = hdr.get(h)
    print(f"  {h}: {v or '(absent)'}")
    if h == "X-Content-Type-Options" and not v:
        add("LOW","headers","no X-Content-Type-Options (GitHub Pages limitation — not configurable)")
    if h == "Content-Security-Policy" and not v:
        add("LOW","headers","no Content-Security-Policy (static site; XSS already mitigated by escaping)")

# --- external dependency health ---
print("\n=== external dependencies ===")
for name, url in [("BWEnews RSS","https://rss-public.bwe-ws.com/"),
                  ("GeckoTerminal","https://api.geckoterminal.com/api/v2/networks"),
                  ("GitHub API","https://api.github.com/repos/ahmedhailane0/listinglabs")]:
    st, _, _, ms = fetch(url, timeout=20)
    print(f"  {name:14} {st} {ms}ms")
    if st not in (200, 304):
        add("MED","deps",f"{name} returned {st}")

print("\n===== EXTERNAL FINDINGS =====")
for sev in ("HIGH","MED","LOW"):
    for s,a,m in [f for f in findings if f[0]==sev]:
        print(f"  [{sev}] ({a}) {m}")
print(f"TOTAL external findings: {len(findings)}")

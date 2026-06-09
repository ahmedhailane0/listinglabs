"""Fetch RootData open-API project data per token.

The user-supplied API key authenticates against api.rootdata.com.
Workflow:
  POST /open/ser_inv    {"query": <name>}    -> list of candidates with id/name/symbol
  POST /open/get_item   {"project_id": <id>, "include_investors": true}
                                              -> total_funding, investors[], establishment_date, token_symbol, ...

Match heuristic: prefer candidate whose name matches and (if present) token_symbol == ticker.
Cache raw responses keyed by ticker.

Output: cache/rootdata.json   { ticker -> {"project_id": int|None, "search_hit": {...}|None, "item": {...}|None, "name": str} }
"""
import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]   # verifysheet/ (repo root)
CACHE = ROOT / "cache"
OUT = CACHE / "rootdata.json"
SECRETS = Path(r"C:\Users\PC\.config\verifysheet\secrets.env")

BASE = "https://api.rootdata.com/open"


def load_key():
    for line in SECRETS.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() in ("CRYPTORANK_API_KEY", "ROOTDATA_API_KEY", "API_KEY"):
                return v.strip()
    raise RuntimeError("API key not found in secrets.env")


def headers(key):
    return {"apikey": key, "language": "en", "Accept": "application/json"}


def load(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def search(name: str, ticker: str, hdrs) -> dict | None:
    try:
        r = requests.post(f"{BASE}/ser_inv", json={"query": name}, headers=hdrs, timeout=15)
        if r.status_code != 200:
            return None
        cands = r.json().get("data") or []
        # filter to type==1 (projects)
        cands = [c for c in cands if c.get("type") == 1]
        if not cands:
            r2 = requests.post(f"{BASE}/ser_inv", json={"query": ticker}, headers=hdrs, timeout=15)
            if r2.status_code == 200:
                cands = [c for c in r2.json().get("data") or [] if c.get("type") == 1]
        if not cands:
            return None
        # prefer exact name match
        for c in cands:
            if c.get("name", "").lower() == name.lower():
                return c
        return cands[0]
    except Exception as e:
        print(f"  search err {name}: {e}")
        return None


def get_item(project_id: int, hdrs) -> dict | None:
    try:
        r = requests.post(
            f"{BASE}/get_item",
            json={"project_id": project_id, "include_team": False, "include_investors": True},
            headers=hdrs, timeout=15,
        )
        if r.status_code != 200:
            return None
        return r.json().get("data")
    except Exception as e:
        print(f"  get_item err {project_id}: {e}")
        return None


def main():
    key = load_key()
    hdrs = headers(key)
    rows = json.loads((ROOT / "parsed_rows.json").read_text(encoding="utf-8"))
    out = load(OUT, {})
    n = 0
    for row in rows:
        ticker = row.get("symbol", "")
        name = row.get("name", "")
        if not ticker or not name:
            continue
        if ticker in out and out[ticker].get("item") is not None:
            continue
        hit = search(name, ticker, hdrs)
        time.sleep(0.2)
        if not hit:
            out[ticker] = {"project_id": None, "search_hit": None, "item": None, "name": name}
            continue
        pid = hit.get("id")
        item = get_item(pid, hdrs) if pid else None
        time.sleep(0.2)
        out[ticker] = {"project_id": pid, "search_hit": hit, "item": item, "name": name}
        n += 1
        if n % 25 == 0:
            save(OUT, out)
            print(f"  ...{n} fetched, last={name}({ticker}) pid={pid}")
    save(OUT, out)
    matched = sum(1 for v in out.values() if v.get("item"))
    print(f"done. {matched}/{len(out)} tickers matched a RootData project.")


if __name__ == "__main__":
    main()

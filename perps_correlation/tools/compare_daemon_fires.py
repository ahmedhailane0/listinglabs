"""One-off: validate fetch/screener_daemon.py's real-time fires against the
hourly cron's ledger before flipping its Telegram alerts on (see
docs/SCREENER_DAEMON_SETUP.md step 4).

Joins cache/screener/daemon/fires_live.jsonl (the daemon's not-yet-merged
real-time fires) AND any already-merged `source:"daemon"` rows already folded
into cache/screener/fires_log.json (fetch_screener._merge_daemon_fires runs
each hourly cycle and clears fires_live.jsonl, so after the first hourly run
the evidence lives there instead) against the hourly cron's OWN
independently-detected fires for the same (sym, strat) — a fire the hourly
loop finds at t within FIRE_REFRACTORY_H of a daemon fire at the same (sym,
strat) is the "same" trade catch, just noticed earlier.

Reports:
  - overlap: how many daemon fires the hourly ledger also confirms
  - timing delta: daemon `logged_utc` vs the hourly ledger's `logged_utc` for
    the same (sym, strat, hour) — the whole point of running the daemon
  - daemon-only: fires the hourly loop never independently confirmed within
    its own refractory window (worth a manual look — could be a real early
    catch the hourly loop's later data disagrees with, or a bug)

    python tools/compare_daemon_fires.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetch.fetch_screener import FIRES_LOG, DAEMON_FIRES_LIVE, FIRE_REFRACTORY_H


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def main() -> int:
    log = _load_json(FIRES_LOG, [])
    if not isinstance(log, list):
        print("fires_log.json missing/unreadable")
        return 1
    live = _load_jsonl(DAEMON_FIRES_LIVE)

    daemon_fires = [e for e in log if e.get("source") == "daemon"] + live
    if not daemon_fires:
        print("no daemon fires yet (not-yet-merged fires_live.jsonl is empty and "
              "no source:\"daemon\" rows in fires_log.json) — let it run longer")
        return 0

    # hourly-loop fires: everything NOT tagged source:"daemon"
    hourly = [e for e in log if e.get("source") != "daemon"]
    hourly_by_key: dict[tuple, list[dict]] = {}
    for e in hourly:
        hourly_by_key.setdefault((e.get("sym"), e.get("strat")), []).append(e)

    matched, unmatched = [], []
    for d in daemon_fires:
        key = (d.get("sym"), d.get("strat"))
        dt = d.get("t") or 0
        best = None
        for h in hourly_by_key.get(key, []):
            ht = h.get("t") or 0
            if abs(ht - dt) <= FIRE_REFRACTORY_H * 3600:
                best = h
                break
        (matched if best else unmatched).append((d, best))

    print(f"daemon fires: {len(daemon_fires)}  "
          f"matched-by-hourly: {len(matched)}  daemon-only: {len(unmatched)}\n")

    if matched:
        print("── overlap (daemon caught it, hourly loop later confirmed) ──────────")
        deltas = []
        for d, h in matched:
            dl, hl = d.get("logged_utc"), h.get("logged_utc")
            delta_min = (hl - dl) / 60 if (dl and hl) else None
            if delta_min is not None:
                deltas.append(delta_min)
            print(f"  {d.get('sym'):8s} {d.get('strat'):6s}  daemon+{d.get('t')}  "
                  f"lead={f'{delta_min:.1f}min' if delta_min is not None else 'n/a'}")
        if deltas:
            avg = sum(deltas) / len(deltas)
            print(f"\n  avg lead time: {avg:.1f} min over {len(deltas)} matched fires "
                  f"(how much sooner the daemon alerted vs the hourly cron)")

    if unmatched:
        print("\n── daemon-only (no hourly confirmation within "
              f"{FIRE_REFRACTORY_H}h) — review these ─────────")
        for d, _ in unmatched:
            print(f"  {d.get('sym'):8s} {d.get('strat'):6s}  t={d.get('t')}  "
                  f"alerted={d.get('alerted')}  market_wide={d.get('market_wide', False)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Backfill research CSV rows for days this PC was OFF.

The cloud GitHub Actions cron commits cache/ to GitHub every ~20 min, 24/7 — so the
repo holds a timestamped snapshot of the data for EVERY day, even days this machine
was asleep. The local research/ archive is just a projection of that; when the PC is
off it pauses, but the cloud recording never stops.

This script closes the gap: it finds the days missing from
research/<tab>/daily.csv and, for each, materializes the cache as it was on that day
(extracted from that day's last git commit) and runs build_research_archive.py with
ARCHIVE_ASOF=<day> so the missing rows are reconstructed and appended in
chronological order. Idempotent — days already present are skipped, and latest.csv
(the CURRENT snapshot) is never touched.

    python backfill_missing_days.py                 # fill every gap up to today
    python backfill_missing_days.py --since 2026-06-01
    python backfill_missing_days.py --no-fetch      # skip git fetch (offline)

Wired into run_research_archive.cmd so the daily task auto-fills gaps the moment the
PC comes back online.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
ROOT = HERE.parent                       # verifysheet/  (repo root)
RESEARCH = ROOT / "research"
REF = "origin/main"                      # cloud history (always-on recorder)
# binance_alpha_perps is written on every capture, so its dates are the canonical
# index of "days we have a record for".
INDEX_CSV = RESEARCH / "binance_alpha_perps" / "daily.csv"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _git(*args, check=True, capture=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check,
                          capture_output=capture, text=True, encoding="utf-8")


def _recorded_dates() -> set[str]:
    if not INDEX_CSV.exists():
        return set()
    with INDEX_CSV.open(encoding="utf-8", newline="") as f:
        return {row["date"] for row in csv.DictReader(f) if row.get("date")}


def _commit_for_day(day: str) -> str | None:
    """Last cloud commit on or before 23:59:59Z of `day`, but only if a commit
    actually landed ON that day (else the cron produced no data that day → skip)."""
    out = _git("rev-list", "-1", f"--before={day}T23:59:59Z", REF,
               check=False).stdout.strip()
    if not out:
        return None
    cdate = _git("show", "-s", "--format=%cI", out).stdout.strip()[:10]
    return out if cdate == day else None


def _extract_day(commit: str, dest: Path) -> bool:
    """Extract just cache/ + listings/ from `commit` into `dest` (no working-tree
    churn). Uses git archive -> in-memory tar so it's cross-platform."""
    res = subprocess.run(
        ["git", "archive", "--format=tar", commit,
         "cache", "perps_correlation/listings"],
        cwd=ROOT, check=False, capture_output=True)
    if res.returncode != 0 or not res.stdout:
        return False
    with tarfile.open(fileobj=io.BytesIO(res.stdout)) as tar:
        tar.extractall(dest, filter="data")   # 3.14-safe; these are our own commits
    return (dest / "cache").is_dir()


def _rebuild_day(day: str, commit: str) -> bool:
    with tempfile.TemporaryDirectory(prefix=f"bf_{day}_") as tmp:
        tmpp = Path(tmp)
        if not _extract_day(commit, tmpp):
            print(f"  {day}: could not extract cache from {commit[:9]} — skipped")
            return False
        env = dict(os.environ,
                   ARCHIVE_CACHE=str(tmpp / "cache"),
                   ARCHIVE_LISTINGS=str(tmpp / "perps_correlation" / "listings"),
                   ARCHIVE_OUT=str(RESEARCH),
                   ARCHIVE_ASOF=day)
        r = subprocess.run([sys.executable, str(HERE / "build" / "build_research_archive.py")],
                           cwd=HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8")
        if r.returncode != 0:
            print(f"  {day}: archive failed\n{(r.stderr or r.stdout)[-600:]}")
            return False
        print(f"  {day}: rebuilt from {commit[:9]}")
        return True


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="earliest day to fill (YYYY-MM-DD); "
                    "default = earliest already-recorded day, else 14 days back")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip 'git fetch' (use local history only)")
    args = ap.parse_args(argv)

    if not args.no_fetch:
        print("fetching cloud history …")
        _git("fetch", "origin", "main", check=False)

    recorded = _recorded_dates()
    today = dt.datetime.now(dt.timezone.utc).date()
    if args.since:
        start = dt.date.fromisoformat(args.since)
    elif recorded:
        start = min(dt.date.fromisoformat(d) for d in recorded)
    else:
        start = today - dt.timedelta(days=14)

    # Every day in [start, today]; today itself is handled by the live capture, so
    # backfill only days strictly before today that we don't already have.
    want = []
    d = start
    while d < today:
        s = d.isoformat()
        if s not in recorded:
            want.append(s)
        d += dt.timedelta(days=1)

    if not want:
        print(f"no gaps to backfill (recorded through {max(recorded) if recorded else 'n/a'}).")
        return 0

    print(f"missing {len(want)} day(s): {want[0]} … {want[-1]}")
    filled = skipped = 0
    for day in want:
        commit = _commit_for_day(day)
        if not commit:
            print(f"  {day}: no cloud commit that day — nothing to recover")
            skipped += 1
            continue
        if _rebuild_day(day, commit):
            filled += 1
        else:
            skipped += 1
    print(f"backfill done: {filled} filled, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

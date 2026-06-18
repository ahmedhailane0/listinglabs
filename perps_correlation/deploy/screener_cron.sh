#!/usr/bin/env bash
# Hourly Screener fetch — runs on the always-on box (NOT GitHub CI, which Binance
# 451-blocks). Pulls latest, fetches Binance hourly OI + signals for the
# manipulated-coin list, and commits the two small result files back. The cloud
# loop (update-site.yml) renders them into the Screener tab on its next ~20-min run.
#
# One-time setup is in perps_correlation/docs/SCREENER_BOX_SETUP.md.
# Install (on the box):  crontab -e  ->  17 * * * * /path/to/verifysheet/perps_correlation/deploy/screener_cron.sh >> ~/screener_cron.log 2>&1
set -uo pipefail

# Repo root = two levels up from this script (…/verifysheet).
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO/perps_correlation" || exit 1
PY="${PYTHON:-python3}"

echo "=== $(date -u +%FT%TZ) screener_cron ==="

# 1) Get the latest cache (best-effort; keep going if the pull hiccups).
git -C "$REPO" pull --rebase --autostash origin main || echo "warn: pull failed, continuing"

# 2) Fetch + compute (reaches Binance from this box).
if ! "$PY" fetch/fetch_screener.py; then
  echo "error: fetch_screener.py failed"; exit 1
fi

# 3) Commit the compact outputs + any new holder files.
git -C "$REPO" add cache/screener/screener.json cache/screener/market.json
git -C "$REPO" add cache/scam_holders/ 2>/dev/null || true
if git -C "$REPO" diff --cached --quiet; then
  echo "no change to commit"; exit 0
fi
# NO [skip ci]: this push must trigger the cloud rebuild so fresh hourly signals
# actually reach the live site. GitHub's schedule cron is best-effort (fires every
# 3-5h, not every 20min), so the box push is the reliable deploy trigger. The cloud
# loop's own commit-back keeps [skip ci], so there is no rebuild loop. Public repo =
# unlimited Actions minutes, so hourly builds cost nothing.
git -C "$REPO" commit -m "chore: screener hourly signals" || exit 0

# 4) Push with rebase-retry (the cloud loop commits every ~20 min — races happen).
for i in 1 2 3 4 5; do
  if git -C "$REPO" push; then echo "pushed on attempt $i"; exit 0; fi
  echo "push rejected — rebasing (attempt $i)"
  git -C "$REPO" pull --rebase --autostash origin main || true
  sleep $(( (RANDOM % 5) + 2 ))
done
echo "warn: could not push after retries; next run will catch up"

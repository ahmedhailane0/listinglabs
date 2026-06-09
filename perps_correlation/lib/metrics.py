"""Derive price-reaction metrics for a listing from its cached 5m candles.

All values come from `cache/<token>_klines_5m_alpha.json` (the on-chain Alpha
price) relative to the Binance Alpha listing time — no external calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from lib.listing_chart import parse_iso
from lib.interactive_chart import _load_rows

CHECKPOINTS = [("+1h", timedelta(hours=1)), ("+24h", timedelta(hours=24)),
               ("+7d", timedelta(days=7)), ("+30d", timedelta(days=30)),
               ("+90d", timedelta(days=90))]

# Max drawdown ignores this opening settling window. A brand-new on-chain Alpha
# pool's first few 5m candles wick violently (wide open spread, bots, thin
# liquidity): e.g. QAIT's very first candle swung high $0.0119 -> low $0.00353
# in a single bar, a fake -70% no one actually traded through. Starting the
# running peak after the settle window makes drawdown reflect real post-launch
# price action instead of launch-candle noise.
DD_SETTLE = timedelta(hours=1)


def alpha_time(cfg: dict) -> datetime:
    for ev in cfg.get("events", []):
        if ev["exchange"] == "Binance Alpha":
            return parse_iso(ev["iso_time_utc"])
    return parse_iso(cfg["window_start_utc"])


def _price_at(rows: list, t: datetime) -> float | None:
    """Close of the first candle at/after t; falls back to the last before it."""
    tms = int(t.timestamp() * 1000)
    prev = None
    for ts, _o, _h, _l, c in rows:
        if ts >= tms:
            return c
        prev = c
    return prev


def reaction(cfg: dict) -> dict | None:
    rows = _load_rows(cfg["token"])
    if not rows:
        return None
    launch = alpha_time(cfg)
    # If the listing predates the available price data (e.g. funnel tokens whose
    # Binance perp opened months after their Alpha listing), anchor "launch" at
    # the first candle so reaction stats measure real data, not a void. No-op
    # when the launch falls inside the window (the curated tokens).
    first_t = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    if launch < first_t:
        launch = first_t
    launch_ms = int(launch.timestamp() * 1000)
    launch_px = _price_at(rows, launch)
    if not launch_px:
        return None
    last_ts, last_px = rows[-1][0], rows[-1][4]
    last_t = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)

    post = [r for r in rows if r[0] >= launch_ms] or rows
    peak_row = max(post, key=lambda r: r[2])          # highest high
    peak_px, peak_t = peak_row[2], datetime.fromtimestamp(peak_row[0] / 1000, tz=timezone.utc)
    # Drawdown over the settled window (skip launch-candle wicks; see DD_SETTLE).
    settle_ms = launch_ms + int(DD_SETTLE.total_seconds() * 1000)
    dd_rows = [r for r in rows if r[0] >= settle_ms] or post
    run_max, max_dd = dd_rows[0][2], 0.0
    for _ts, _o, h, l, _c in dd_rows:
        run_max = max(run_max, h)
        max_dd = min(max_dd, l / run_max - 1)

    checks = []
    for label, delta in CHECKPOINTS:
        target = launch + delta
        if target <= last_t:
            px = _price_at(rows, target)
            if px:
                checks.append((label, (px / launch_px - 1) * 100))

    return {
        "launch_px": launch_px,
        "last_px": last_px,
        "last_t": last_t,
        "change_pct": (last_px / launch_px - 1) * 100,
        "ath_px": peak_px,                       # highest traded price (= peak high)
        "atl_px": min(r[3] for r in post),       # lowest traded price (low wick)
        "peak_px": peak_px,
        "peak_gain_pct": (peak_px / launch_px - 1) * 100,
        "time_to_peak": peak_t - launch,
        "max_drawdown_pct": max_dd * 100,
        "checkpoints": checks,
    }


def fmt_duration(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"

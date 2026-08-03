"""Regression tests for lib/metrics.py — the since-listing numbers the site prints.

Companion to test_build_scams.py. This module turns cached 5m candles into the
Since / peak / drawdown / checkpoint figures on every token page, and two of its
rules exist because of specific bugs that shipped:

  * DD_SETTLE — a brand-new on-chain pool's first candles wick violently on a
    wide spread and thin liquidity. QAIT's very first bar swung $0.0119 -> $0.00353,
    a fake -70% nobody traded through. Drawdown must start its running peak AFTER
    the settle window or it reports launch-candle noise as a real drawdown.
  * CHECKPOINT_TOL — cached histories have multi-month holes. Without a cap,
    "+30d" silently returned a candle from months later and labelled it the
    30-day mark (the VELVET bug). A checkpoint inside a data gap must be OMITTED,
    never bridged — the project's standing rule is to never invent a number to
    fill a hole.

Both are exactly the kind of rule that a refactor quietly breaks while every page
keeps rendering plausible-looking figures, so they are pinned here.

Candle row format is (ts_ms, open, high, low, close), ascending by ts.

DEPENDENCIES: lib/metrics.py imports lib.listing_chart, which pulls matplotlib.
The screener box has no chart stack, so this module skips cleanly there (same as
test_build_scams.py). tests/test_signals.py stays dependency-free for the box.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("matplotlib", reason="lib.metrics needs the chart stack; the box has none")

from lib import metrics as M          # noqa: E402

LAUNCH = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
MIN5 = 5 * 60 * 1000


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _row(dt, o=100.0, h=None, l=None, c=None):
    """One candle. Defaults make a flat bar at `o`."""
    return [_ms(dt), o, o if h is None else h, o if l is None else l, o if c is None else c]


def _flat(start, n, px=100.0, step_ms=MIN5):
    """n flat candles at `px`, 5m apart, beginning at `start`."""
    return [[_ms(start) + i * step_ms, px, px, px, px] for i in range(n)]


def _cfg(token="tok", launch=LAUNCH):
    return {"token": token,
            "events": [{"exchange": "Binance Alpha",
                        "iso_time_utc": launch.strftime("%Y-%m-%dT%H:%M:%SZ")}],
            "window_start_utc": launch.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _reaction(monkeypatch, rows, cfg=None):
    monkeypatch.setattr(M, "_load_rows", lambda _t: rows)
    return M.reaction(cfg or _cfg())


# ─────────────────────────── alpha_time ──────────────────────────────────────

def test_alpha_time_prefers_the_binance_alpha_event():
    cfg = {"events": [{"exchange": "Bybit", "iso_time_utc": "2026-01-01T00:00:00Z"},
                      {"exchange": "Binance Alpha", "iso_time_utc": "2026-03-01T12:00:00Z"}],
           "window_start_utc": "2020-01-01T00:00:00Z"}
    assert M.alpha_time(cfg) == LAUNCH


def test_alpha_time_falls_back_to_window_start():
    cfg = {"events": [{"exchange": "Bybit", "iso_time_utc": "2026-01-01T00:00:00Z"}],
           "window_start_utc": "2026-03-01T12:00:00Z"}
    assert M.alpha_time(cfg) == LAUNCH


# ─────────────────────────── _price_at ───────────────────────────────────────

def test_price_at_takes_the_first_candle_at_or_after_t():
    rows = [_row(LAUNCH - timedelta(minutes=5), c=90.0), _row(LAUNCH, c=100.0)]
    assert M._price_at(rows, LAUNCH) == 100.0


def test_price_at_falls_back_to_the_last_candle_before_t():
    """A launch after the cache ends must still price off the last known candle."""
    rows = [_row(LAUNCH - timedelta(minutes=10), c=90.0),
            _row(LAUNCH - timedelta(minutes=5), c=95.0)]
    assert M._price_at(rows, LAUNCH) == 95.0


# ─────────────────────────── _price_near: the data-gap rule ──────────────────

def test_price_near_returns_the_nearest_candle_inside_tolerance():
    target = LAUNCH + timedelta(days=30)
    rows = [_row(target - timedelta(hours=1), c=250.0)]
    assert M._price_near(rows, target) == 250.0


def test_price_near_returns_none_when_the_target_sits_in_a_data_gap():
    """THE VELVET BUG: the nearest candle is months away, so "+30d" must be
    OMITTED rather than quoting a much later price as the 30-day mark."""
    target = LAUNCH + timedelta(days=30)
    rows = [_row(LAUNCH, c=100.0), _row(target + timedelta(days=90), c=999.0)]
    assert M._price_near(rows, target) is None


def test_price_near_tolerance_boundary_is_inclusive():
    target = LAUNCH + timedelta(days=30)
    inside = [_row(target - M.CHECKPOINT_TOL, c=123.0)]
    assert M._price_near(inside, target) == 123.0

    outside = [_row(target - M.CHECKPOINT_TOL - timedelta(minutes=5), c=123.0)]
    assert M._price_near(outside, target) is None


def test_price_near_respects_an_explicit_tolerance():
    target = LAUNCH + timedelta(days=30)
    rows = [_row(target - timedelta(days=1), c=150.0)]
    assert M._price_near(rows, target, tol=timedelta(days=2)) == 150.0
    assert M._price_near(rows, target, tol=timedelta(hours=1)) is None


# ─────────────────────────── reaction: drawdown settle window ────────────────

def test_drawdown_ignores_the_opening_settle_window(monkeypatch):
    """THE QAIT BUG: the launch candle's violent wick is not a real drawdown.
    Here the first bar craters to 30 (-70%) inside DD_SETTLE, then the settled
    market only ever dips to 90 (-10%)."""
    rows = [_row(LAUNCH, o=100.0, h=100.0, l=30.0, c=100.0)]
    rows += _flat(LAUNCH + timedelta(minutes=5), 11)                  # rest of the settle hour
    after = LAUNCH + M.DD_SETTLE
    rows += [_row(after, o=100.0, h=100.0, l=100.0, c=100.0),
             _row(after + timedelta(minutes=5), o=100.0, h=100.0, l=90.0, c=95.0)]

    r = _reaction(monkeypatch, rows)
    assert r["max_drawdown_pct"] == pytest.approx(-10.0), (
        "launch-candle wick leaked into drawdown — DD_SETTLE not applied")


def test_drawdown_is_zero_when_price_only_rises(monkeypatch):
    rows = _flat(LAUNCH, 12)
    after = LAUNCH + M.DD_SETTLE
    rows += [_row(after + timedelta(minutes=5 * i), o=100.0 + i, h=100.0 + i,
                  l=100.0 + i, c=100.0 + i) for i in range(5)]
    assert _reaction(monkeypatch, rows)["max_drawdown_pct"] == pytest.approx(0.0)


# ─────────────────────────── reaction: core figures ──────────────────────────

def test_reaction_returns_none_without_candles(monkeypatch):
    assert _reaction(monkeypatch, []) is None


def test_change_and_peak_are_measured_from_the_launch_price(monkeypatch):
    rows = [_row(LAUNCH, o=100.0, h=100.0, l=100.0, c=100.0)]
    rows += _flat(LAUNCH + timedelta(minutes=5), 11)
    after = LAUNCH + M.DD_SETTLE
    rows += [_row(after, o=100.0, h=200.0, l=100.0, c=150.0)]

    r = _reaction(monkeypatch, rows)
    assert r["launch_px"] == 100.0
    assert r["last_px"] == 150.0
    assert r["change_pct"] == pytest.approx(50.0)
    assert r["peak_gain_pct"] == pytest.approx(100.0)      # peak high 200 vs launch 100
    assert r["ath_px"] == 200.0


def test_launch_anchors_to_the_first_candle_when_the_listing_predates_the_data(monkeypatch):
    """Funnel tokens whose perp opened months after their Alpha listing must
    measure real data, not a void before the cache starts."""
    data_start = LAUNCH + timedelta(days=60)
    rows = _flat(data_start, 24, px=50.0)
    r = _reaction(monkeypatch, rows, _cfg(launch=LAUNCH))
    assert r["launch_px"] == 50.0, "stats should anchor at the first available candle"


def test_atl_uses_post_launch_lows_only(monkeypatch):
    """A pre-launch low is not part of the listing reaction."""
    rows = [_row(LAUNCH - timedelta(hours=2), o=100.0, h=100.0, l=1.0, c=100.0)]
    rows += [_row(LAUNCH, o=100.0, h=100.0, l=100.0, c=100.0)]
    rows += _flat(LAUNCH + timedelta(minutes=5), 11)
    after = LAUNCH + M.DD_SETTLE
    rows += [_row(after, o=100.0, h=100.0, l=80.0, c=90.0)]

    assert _reaction(monkeypatch, rows)["atl_px"] == 80.0


# ─────────────────────────── reaction: checkpoints ───────────────────────────

def test_checkpoint_is_omitted_when_it_falls_in_a_data_gap(monkeypatch):
    """THE VELVET BUG at the reaction() level: data stops ~2h after launch and
    resumes 40 days later, so "+30d" has no candle within CHECKPOINT_TOL on
    either side and must be DROPPED — not filled from the far-side data.

    Note "+24h" IS legitimately kept here: the last real candle is only ~22h from
    that target, inside the 2-day tolerance. The rule caps how far a checkpoint
    may reach, it does not require an exact hit."""
    rows = [_row(LAUNCH, o=100.0, h=100.0, l=100.0, c=100.0)]
    rows += _flat(LAUNCH + timedelta(minutes=5), 24)            # covers ~2h
    rows += _flat(LAUNCH + timedelta(days=40), 3, px=500.0)     # long gap, then data

    labels = [lbl for lbl, _ in _reaction(monkeypatch, rows)["checkpoints"]]
    assert "+1h" in labels
    assert "+30d" not in labels, "a checkpoint was bridged across a data gap"
    assert "+7d" not in labels, "a checkpoint was bridged across a data gap"


def test_checkpoints_beyond_the_last_candle_are_not_reported(monkeypatch):
    rows = [_row(LAUNCH, o=100.0, h=100.0, l=100.0, c=100.0)]
    rows += _flat(LAUNCH + timedelta(minutes=5), 24)
    labels = [lbl for lbl, _ in _reaction(monkeypatch, rows)["checkpoints"]]
    assert labels == ["+1h"], "only checkpoints the data actually reaches may appear"


def test_checkpoint_pct_is_relative_to_launch(monkeypatch):
    rows = [_row(LAUNCH, o=100.0, h=100.0, l=100.0, c=100.0)]
    rows += _flat(LAUNCH + timedelta(minutes=5), 11)
    rows += [_row(LAUNCH + timedelta(hours=1), o=100.0, h=100.0, l=100.0, c=175.0)]

    checks = dict(_reaction(monkeypatch, rows)["checkpoints"])
    assert checks["+1h"] == pytest.approx(75.0)


# ─────────────────────────── fmt_duration ────────────────────────────────────

def test_fmt_duration_formats_by_largest_unit():
    assert M.fmt_duration(timedelta(minutes=7)) == "7m"
    assert M.fmt_duration(timedelta(hours=3, minutes=9)) == "3h 9m"
    assert M.fmt_duration(timedelta(days=2, hours=5, minutes=30)) == "2d 5h"


def test_fmt_duration_handles_zero():
    assert M.fmt_duration(timedelta(0)) == "0m"

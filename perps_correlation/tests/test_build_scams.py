"""Regression tests for the MONEY + VISIBILITY code in build/build_scams.py.

Why this file exists (2026-08-03): every numeric bug found in this project so far
was found by a human reading code, never by a test. `tests/test_signals.py` covers
the detector engine, but nothing covered the layer that decides WHICH setups the
site shows and WHAT $ P&L it prints for them. Four real bugs in one session came
from that gap:

  * build_scams re-declared TP1/TP2/DUMP_TARGET instead of importing them, so a
    promoted exit change would have silently desynced the printed P&L from the
    grader that produced it;
  * _winrate_chart hardcoded its setup list, fell behind the visibility gate, and
    silently dropped bear_trap's trades from the chart while the ledger below it
    still showed them;
  * card copy quoted backtest lifts that no longer matched lib/signals.py;
  * a losing CORE setup stayed on the site because nothing flagged it.

Each test below pins one of those invariants. They are deliberately about RULES
(a hidden setup needs N graded AND positive expectancy) rather than today's
membership (dump is hidden), so promoting/demoting a setup does not fail the
suite — only breaking the mechanism does.

NOTE ON DEPENDENCIES: build_scams pulls matplotlib/numpy/requests through
build_listing_report. lib/signals.py is deliberately dependency-free so the
screener box can run test_signals.py without a chart stack, and the box has NO
matplotlib — so this module SKIPS cleanly there instead of erroring. Run locally
or in CI where the full stack exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The box has no matplotlib; skip the whole module there rather than fail.
pytest.importorskip("matplotlib", reason="build_scams needs the chart stack; the box has none")

from build import build_scams as B          # noqa: E402
from lib import signals as S                # noqa: E402

HOUR = 3600
# A realistic bar time. NOT 0: `_episodes`' staleness guard tests `e.get("t")` for
# truthiness, so a t=0 fire short-circuits the check and bypasses the rule. Real
# timestamps are ~1.7e9 so this never bites in production, but fixtures must not
# sit on the falsy edge or they test the wrong branch.
BASE = 1_780_000_000


def _fire(sym="AAA", strat="v2", t=0, lag=0, pnl=None, pump=False):
    """One fires_log-shaped entry. `t` is an offset from BASE; `lag` is how long
    after its own bar the fire was journaled (0 = immediately tradeable)."""
    ts = BASE + t
    e = {"sym": sym, "strat": strat, "t": ts, "logged_utc": ts + lag}
    if pnl is not None:
        e["outcome"] = {"pnl_real": pnl, "pump": pump}
    return e


# ─────────────────────────── exit plan: one source of truth ──────────────────

def test_exit_constants_are_imported_not_redeclared():
    """The $ P&L on the AI Trades page must be computed with the SAME exit plan
    the grader scored the trade on. These used to be a second hardcoded copy in
    build_scams; the optimizer searches the exit space nightly, so the first
    promoted exit change would have desynced them with nothing failing."""
    for name in ("TP1", "TP1_FRAC", "TP2", "DUMP_TARGET"):
        assert getattr(B, name) == getattr(S, name), (
            f"{name} disagrees with lib.signals — re-declared instead of imported?")


def test_dump_target_is_used_for_shorts_not_tp2():
    """dump is the only SHORT: it covers at -DUMP_TARGET, it does not sell at +TP2."""
    assert B.DUMP_TARGET > 0
    assert B.DUMP_TARGET != B.TP2, "a short's target must not silently equal the long's"


# ─────────────────────────── _episodes: honest fire counting ─────────────────

def test_episodes_collapse_refires_within_72h():
    """One row per (sym, setup) per 72h — hourly re-fires inside the refractory
    window are the same trade, not three."""
    fires = [_fire(t=0), _fire(t=1 * HOUR), _fire(t=2 * HOUR)]
    assert len(B._episodes(fires)) == 1


def test_episodes_keep_refires_after_72h():
    fires = [_fire(t=0), _fire(t=73 * HOUR)]
    assert len(B._episodes(fires)) == 2


def test_episodes_separate_different_symbols_and_setups():
    """The dedup key is (sym, setup) — two coins firing at once are two trades."""
    fires = [_fire(sym="AAA", t=0), _fire(sym="BBB", t=0), _fire(sym="AAA", strat="v4", t=0)]
    assert len(B._episodes(fires)) == 3


def test_episodes_drop_fires_logged_after_the_tradeability_window():
    """A fire journaled more than LOG_LAG_MAX_S after its own bar was never
    tradeable (the 2026-07 audit found 104 such hindsight rows). It must be
    excluded EVERYWHERE, not just in the grader."""
    assert B._episodes([_fire(lag=B.LOG_LAG_MAX_S + 1)]) == []
    assert len(B._episodes([_fire(lag=B.LOG_LAG_MAX_S)])) == 1


def test_episodes_without_logged_utc_are_kept():
    """Older rows predate the logged_utc field; the staleness rule must not
    silently delete the entire early history."""
    e = {"sym": "AAA", "strat": "v2", "t": BASE}
    assert len(B._episodes([e])) == 1


def test_episodes_collapse_market_wide_clusters():
    """More than breadth_max coins firing the SAME setup the SAME hour is one
    market event, not N independent signals — it keeps a single sample."""
    n = 7
    fires = [_fire(sym=f"C{i}", t=0) for i in range(n + 3)]
    assert len(B._episodes(fires, breadth_max=n)) == 1

    # at/below the threshold every coin still counts on its own
    fires = [_fire(sym=f"C{i}", t=0) for i in range(n)]
    assert len(B._episodes(fires, breadth_max=n)) == n


# ─────────────────────────── _real_pnl: which number is the money ────────────

def test_real_pnl_prefers_pnl_real_over_ownstop():
    e = {"outcome": {"pnl_real": 0.11, "pnl_ownstop": 0.99}}
    assert B._real_pnl(e) == 0.11


def test_real_pnl_falls_back_for_trades_graded_before_pnl_real_existed():
    assert B._real_pnl({"outcome": {"pnl_ownstop": 0.42}}) == 0.42


def test_real_pnl_is_none_when_ungraded():
    assert B._real_pnl({}) is None
    assert B._real_pnl({"outcome": {}}) is None


def test_real_pnl_keeps_a_legitimate_zero():
    """0.0 is a real breakeven result, not a missing value — `or` would eat it."""
    assert B._real_pnl({"outcome": {"pnl_real": 0.0}}) == 0.0


# ─────────────────────────── the visibility gate ─────────────────────────────

def test_core_setups_always_visible():
    assert set(B.CORE_SETUPS).issubset(set(B._visible_setups([])))


def test_core_and_hidden_are_disjoint():
    """A setup in both lists would make the gate's meaning undefined."""
    assert not (set(B.CORE_SETUPS) & set(B.HIDDEN_SETUPS))


def test_retired_setups_are_in_neither_list():
    """v1 was RETIRED (tested and dead), not hidden. Putting it in HIDDEN_SETUPS
    would let a couple of lucky trades flicker it back onto the site."""
    assert "v1" not in B.CORE_SETUPS and "v1" not in B.HIDDEN_SETUPS


def test_hidden_setup_stays_hidden_below_the_sample_floor():
    """Positive expectancy on a tiny sample is not evidence."""
    sid = B.HIDDEN_SETUPS[0]
    eps = [_fire(sym=f"C{i}", strat=sid, t=i * 100 * HOUR, pnl=0.5)
           for i in range(B.EARN_MIN_N - 1)]
    assert sid not in B._visible_setups(eps)


def test_hidden_setup_stays_hidden_when_losing_at_a_real_sample_size():
    sid = B.HIDDEN_SETUPS[0]
    eps = [_fire(sym=f"C{i}", strat=sid, t=i * 100 * HOUR, pnl=-0.10)
           for i in range(B.EARN_MIN_N + 5)]
    assert sid not in B._visible_setups(eps)


def test_hidden_setup_earns_back_when_positive_at_a_real_sample_size():
    """The auto-return path: enough graded episodes AND positive expectancy.
    This is what lets a demoted setup come back with no code change."""
    sid = B.HIDDEN_SETUPS[0]
    eps = [_fire(sym=f"C{i}", strat=sid, t=i * 100 * HOUR, pnl=0.10)
           for i in range(B.EARN_MIN_N + 5)]
    assert sid in B._visible_setups(eps)


def test_ungraded_episodes_do_not_count_toward_earning_back():
    """Pending trades are not results — 100 open positions must not unhide."""
    sid = B.HIDDEN_SETUPS[0]
    eps = [_fire(sym=f"C{i}", strat=sid, t=i * 100 * HOUR)
           for i in range(B.EARN_MIN_N * 3)]
    assert sid not in B._visible_setups(eps)


# ─────────────────────────── the chart must follow the gate ──────────────────

def test_winrate_chart_renders_exactly_the_visible_setups(monkeypatch):
    """THE REGRESSION: _winrate_chart used to hardcode its setup list. It fell
    behind the gate, so bear_trap — which had earned its way back and appeared in
    the ledger table — was silently missing from the chart directly above it."""
    # `bear_trap` is the real case: it earned its way back through the gate while
    # the old hardcoded list still read ("v2","v4","dump"), so it vanished from the
    # chart. Pick a setup that a stale hardcoded list would NOT contain, or this
    # test passes by coincidence against the very bug it exists to catch.
    visible, hidden = "bear_trap", "probe"
    monkeypatch.setattr(B, "_visible_setups_cached", lambda: (visible,))

    rows = ([_fire(sym=f"V{i}", strat=visible, t=i * 100 * HOUR, pnl=0.2, pump=True)
             for i in range(3)]
            + [_fire(sym=f"H{i}", strat=hidden, t=i * 100 * HOUR, pnl=0.2, pump=True)
               for i in range(3)])

    html = B._winrate_chart(rows)
    assert f">{visible} " in html, "a visible setup is missing from the chart legend"
    assert f">{hidden} " not in html, "a hidden setup leaked into the chart legend"


def test_winrate_chart_survives_a_setup_with_no_color():
    """A newly added setup must not crash the chart before it gets a color."""
    monkeypatch_target = "brand_new_setup"
    import unittest.mock as mock
    with mock.patch.object(B, "_visible_setups_cached", lambda: (monkeypatch_target,)):
        rows = [_fire(sym="A", strat=monkeypatch_target, t=0, pnl=0.1, pump=True)]
        assert B._winrate_chart(rows)          # must not raise


# ─────────────────────────── displayed credentials come from the engine ──────

def test_lift_labels_come_from_signals_not_hardcoded_copy():
    """Card copy quoted "2.9x" while the engine documented 2.26x for weeks.
    The label is now generated from lib.signals.BACKTEST_LIFT."""
    for strat, (mult, target) in S.BACKTEST_LIFT.items():
        label = S.lift_label(strat)
        assert f"{mult:g}" in label and target in label


def test_lift_label_is_empty_for_a_setup_with_no_studied_lift():
    assert S.lift_label("v4") == ""
    assert S.lift_label("does_not_exist") == ""

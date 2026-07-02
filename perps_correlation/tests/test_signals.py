"""Unit tests for the Buy v1/v2/v3 signal engine (lib/signals.py).

Synthetic hourly series engineered to (a) fire each setup, (b) just miss it, and
(c) degrade to `insufficient` on short/gappy data — proving the engine never
fabricates a Buy from missing numbers.

    python tests/test_signals.py      # or: pytest tests/test_signals.py
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # perps_correlation/
from lib.signals import evaluate, _dominance_ratio, HOUR

BASE = (1_700_000_000 // HOUR) * HOUR


def build(oi_vals, closes, highs=None, lows=None, vols=None):
    n = len(oi_vals)
    assert len(closes) == n
    ts = [BASE + i * HOUR for i in range(n)]
    H = highs or [c * 1.001 for c in closes]
    L = lows or [c * 0.999 for c in closes]
    V = vols or [1.0] * n
    oi = [(ts[i], oi_vals[i]) for i in range(n)]
    kl = [(ts[i], closes[i], H[i], L[i], closes[i], V[i]) for i in range(n)]
    return oi, kl, ts


# ── helper / div-by-zero ──────────────────────────────────────────────────────
def test_dominance_ratio():
    assert _dominance_ratio(0.10, 0.0) == float("inf")     # price flat, OI up
    assert _dominance_ratio(0.10, -0.05) == float("inf")   # price down, OI up
    assert _dominance_ratio(0.10, 0.05) == 2.0
    assert _dominance_ratio(0.0, 0.05) == 0.0
    assert _dominance_ratio(-0.10, 0.0) == 0.0             # OI not rising


# ── Buy v1 ────────────────────────────────────────────────────────────────────
def _v1_series():
    oi = [80, 80, 80, 80, 80, 80, 90, 95, 100, 109]   # last 4: 109>100>95>90, +21%/3h
    closes = [10, 10, 10, 10, 10, 10, 10, 10, 10, 10.5]  # breaks 6h high; +5%/3h
    return build(oi, closes)


def test_v1_fires():
    oi, kl, ts = _v1_series()
    out = evaluate(oi, kl, current_funding=0.0)
    assert out["v1"]["fired"] is True, out["v1"]["conditions"]
    assert out["v1"]["t"] == ts[-1]
    assert abs(out["v1"]["stop"] - max(h for _t, _o, h, _l, _c, _v in kl[3:9])) < 1e-9


def test_v1_blocked_by_high_funding():
    oi, kl, _ = _v1_series()
    out = evaluate(oi, kl, current_funding=0.002)          # 0.2% > 0.05% cap
    assert out["v1"]["fired"] is False
    assert out["v1"]["conditions"]["funding<0.05%"] is False


def test_v1_blocked_when_oi_not_3up():
    oi = [80, 80, 80, 80, 80, 80, 95, 90, 100, 109]        # dip at k2 breaks the run
    closes = [10, 10, 10, 10, 10, 10, 10, 10, 10, 10.5]
    o, kl, _ = build(oi, closes)
    out = evaluate(o, kl, current_funding=0.0)
    assert out["v1"]["fired"] is False
    assert out["v1"]["conditions"]["oi_3up"] is False


# ── Buy v3 (washout reversal) ─────────────────────────────────────────────────
def _v3_series(oi_now=78.0):
    #            idx: 0     1     2     3      4(peak) 5(low) 6     7     8(now)
    oi = [60, 60, 60, 100, 70, 74, 76, oi_now]
    closes = [9.9, 9.9, 9.9, 10.0, 9.5, 9.6, 9.7, 10.1]
    highs = [9.95, 9.95, 9.95, 10.05, 9.55, 9.65, 9.75, 10.15]
    lows = [9.85, 9.85, 9.85, 9.95, 9.45, 9.55, 9.65, 9.95]
    return build(oi, closes, highs, lows)


def test_v3_fires():
    oi, kl, ts = _v3_series()
    out = evaluate(oi, kl, current_funding=0.00005, funding_interval_h=8.0)
    assert out["v3"]["fired"] is True, out["v3"]["conditions"]
    assert out["v3"]["t"] == ts[-1]


def test_v3_blocked_when_no_rebuild():
    # OI stays pinned near the washout low (70) at EVERY recent hour, so no hour
    # ever rebuilds >=8% -> v3 never fires (the 72h scan finds nothing).
    oi = [60, 60, 60, 100, 70, 71, 71, 72]
    closes = [9.9, 9.9, 9.9, 10.0, 9.5, 9.6, 9.7, 10.1]
    highs = [9.95, 9.95, 9.95, 10.05, 9.55, 9.65, 9.75, 10.15]
    lows = [9.85, 9.85, 9.85, 9.95, 9.45, 9.55, 9.65, 9.95]
    o, kl, _ = build(oi, closes, highs, lows)
    out = evaluate(o, kl, current_funding=0.00005, funding_interval_h=8.0)
    assert out["v3"]["fired"] is False
    assert out["v3"]["conditions"]["oi_rebuild>=8%"] is False


def test_v3_blocked_by_funding():
    oi, kl, _ = _v3_series()
    # 0.05% per-8h funding -> above the 0.02% washout cap.
    out = evaluate(oi, kl, current_funding=0.0005, funding_interval_h=8.0)
    assert out["v3"]["fired"] is False
    assert out["v3"]["conditions"]["|funding_8h|<=0.02%"] is False


# ── Buy v2 (IGNITION: OI-confirmed coil-break) ────────────────────────────────
def _v2_series():
    # 48h tight flat coil at 10.0, then the last 4 bars break out +10% above the
    # coil top on a 6x volume burst WHILE OI surges +20% over 3h — funding cold.
    n = 60
    closes = [10.0] * 56 + [11.0] * 4               # break > coil_hi(10.01)*1.08
    vols = [1.0] * 56 + [6.0] * 4                   # >=5x the coil's median (1.0)
    oi = [100.0] * 57 + [110.0, 115.0, 120.0]       # oi(t-3h)=100 -> oi(t)=120 = +20%
    return build(oi, closes, vols=vols)


def test_v2_fires():
    oi, kl, ts = _v2_series()
    out = evaluate(oi, kl, current_funding=0.0)
    assert out["v2"]["fired"] is True, out["v2"]["conditions"]
    assert out["v2"]["t"] == ts[-1]
    assert abs(out["v2"]["stop"] - 10.0 * 1.001) < 1e-6     # stop = coil high


def test_v2_blocked_without_oi_surge():
    # Same price/volume breakout but OI stays FLAT -> the conjunction fails: a
    # spot-only fakeout, exactly what Ignition's OI-surge condition filters out.
    n = 60
    closes = [10.0] * 56 + [11.0] * 4
    vols = [1.0] * 56 + [6.0] * 4
    oi = [100.0] * n                                # no OI surge
    o, kl, _ = build(oi, closes, vols=vols)
    out = evaluate(o, kl, current_funding=0.0)
    assert out["v2"]["fired"] is False
    assert out["v2"]["conditions"]["oi_surge>=15%"] is False


# ── insufficient / gappy data must never fire ─────────────────────────────────
def test_short_history_insufficient():
    oi, kl, _ = build([80, 90, 100], [10, 10, 10.5])       # 3 hours: too short for all
    out = evaluate(oi, kl, current_funding=0.0)
    for s in ("v1", "v2", "v3"):
        assert out[s]["fired"] is False
        assert out[s]["insufficient"] is True


def test_gap_in_window_insufficient_not_fire():
    # Take the firing v3 series, punch a hole in its OI window -> insufficient,
    # NOT a (false) fire.
    oi, kl, _ = _v3_series()
    oi_gap = [pt for i, pt in enumerate(oi) if i != 5]     # drop one OI hour
    out = evaluate(oi_gap, kl, current_funding=0.00005, funding_interval_h=8.0)
    assert out["v3"]["fired"] is False
    assert out["v3"]["insufficient"] is True


def test_empty_series():
    out = evaluate([], [])
    assert out["as_of"] is None
    assert out["v1"]["insufficient"] is True


# ── Probe day (#7) ────────────────────────────────────────────────────────────
def test_probe_fires():
    n = 61
    oi = [100.0] * n
    closes = [10.0] * (n - 1) + [11.0]               # last hour breaks the coil top
    highs = [10.01] * (n - 1) + [11.01]
    lows = [9.99] * (n - 1) + [10.5]
    vols = [1.0] * (n - 12) + [10.0] * 12            # 12h volume burst (>=3x)
    o, kl, ts = build(oi, closes, highs, lows, vols)
    out = evaluate(o, kl, current_funding=0.0)
    assert out["probe"]["fired"] is True, out["probe"]["conditions"]
    assert out["probe"]["t"] == ts[-1]


def test_probe_blocked_without_volume():
    n = 61
    oi = [100.0] * n
    closes = [10.0] * (n - 1) + [11.0]
    highs = [10.01] * (n - 1) + [11.01]
    lows = [9.99] * n
    o, kl, _ = build(oi, closes, highs, lows, vols=[1.0] * n)   # no spike
    out = evaluate(o, kl, current_funding=0.0)
    assert out["probe"]["fired"] is False
    assert out["probe"]["conditions"]["vol_spike>=3x"] is False


def test_probe_insufficient_short():
    o, kl, _ = build([100.0] * 10, [10.0] * 10)
    out = evaluate(o, kl, current_funding=0.0)
    assert out["probe"]["insufficient"] is True


# ── Distribution / dump (#6, short) ───────────────────────────────────────────
def _dump_series():
    n = 49
    closes = [10.0 + min(i, 20) * 0.25 for i in range(n)]   # ramp 10->15, then flat 15
    oi = [100.0 + i for i in range(n)]                      # OI rising (still elevated)
    return build(oi, closes)


def test_dump_fires():
    oi, kl, ts = _dump_series()
    out = evaluate(oi, kl, current_funding=0.0006)          # hot funding (crowded longs)
    assert out["dump"]["fired"] is True, out["dump"]["conditions"]
    assert out["dump"]["t"] == ts[-1]


def test_dump_blocked_by_cold_funding():
    oi, kl, _ = _dump_series()
    out = evaluate(oi, kl, current_funding=0.0)              # funding not hot
    assert out["dump"]["fired"] is False
    assert out["dump"]["conditions"]["funding_hot>=0.05%"] is False


# ── Bear trap (price-action factor) ──────────────────────────────────────────
def _trap_series(flush_low=8.0):
    n = 50                          # t-48 = index 1
    oi = [100.0] * n
    closes = [10.0] * n
    lows = [9.95] * n
    for i in range(19, 30):         # the flush, well inside [t-48 .. t-12]
        lows[i] = flush_low
    highs = [10.05] * n
    return build(oi, closes, highs, lows)


def test_bear_trap_fires():
    oi, kl, ts = _trap_series(flush_low=8.0)      # -20% flush, fully reclaimed
    out = evaluate(oi, kl, current_funding=0.0)
    assert out["bear_trap"]["fired"] is True, out["bear_trap"]["conditions"]
    assert out["bear_trap"]["t"] == ts[-1]
    assert abs(out["bear_trap"]["stop"] - 8.0) < 1e-9     # stop = the flush low


def test_bear_trap_blocked_shallow_flush():
    oi, kl, _ = _trap_series(flush_low=9.0)       # only -10% — not a real flush
    out = evaluate(oi, kl, current_funding=0.0)
    assert out["bear_trap"]["fired"] is False
    assert out["bear_trap"]["conditions"]["flushed>=15%"] is False


def test_bear_trap_insufficient_short():
    oi, kl, _ = build([100.0] * 10, [10.0] * 10)
    out = evaluate(oi, kl, current_funding=0.0)
    assert out["bear_trap"]["insufficient"] is True


# ── Spike & retrace (price-action factor) ─────────────────────────────────────
def _spike_series():
    n = 30                          # t-24 = index 5, close 10
    oi = [100.0] * n
    closes = [10.0] * (n - 1) + [11.0]            # holds above pre-spike, retraced
    highs = [10.01] * n
    highs[20] = 14.0                              # the +40% spike, inside last 24h
    highs[-1] = 11.01
    lows = [9.99] * n
    return build(oi, closes, highs, lows)


def test_spike_retrace_fires():
    oi, kl, ts = _spike_series()
    out = evaluate(oi, kl, current_funding=-0.0001)      # shorts crowding
    assert out["spike_retrace"]["fired"] is True, out["spike_retrace"]["conditions"]
    assert out["spike_retrace"]["t"] == ts[-1]


def test_spike_retrace_blocked_positive_funding():
    oi, kl, _ = _spike_series()
    out = evaluate(oi, kl, current_funding=0.0001)       # no short crowd = no fuel
    assert out["spike_retrace"]["fired"] is False
    assert out["spike_retrace"]["conditions"]["funding_negative"] is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)

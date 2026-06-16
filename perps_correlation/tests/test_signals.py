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
    out = evaluate(oi, kl, current_funding=0.002)          # 0.2% > 0.1% cap
    assert out["v1"]["fired"] is False
    assert out["v1"]["conditions"]["funding<0.1%"] is False


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


# ── Buy v2 (OI + EMA golden cross) ────────────────────────────────────────────
def _v2_series():
    n = 80
    closes = [10.0] * n
    # gentle rise that triggers an EMA20-over-EMA60 cross inside the last 10 bars,
    # then a flat shelf so price sits on EMA20 (the "near EMA20" condition).
    for i in range(72, 75):
        closes[i] = 10.0 + (i - 71) * 0.1          # 10.1, 10.2, 10.3
    for i in range(75, n):
        closes[i] = 10.3                            # flat shelf at 10.3
    oi = [100.0] * n
    oi[-4:] = [100, 103, 106, 110]                  # 3 consecutive up, +10%/3h
    vols = [1.0] * n
    return build(oi, closes, vols=vols)


def test_v2_fires():
    oi, kl, ts = _v2_series()
    out = evaluate(oi, kl, current_funding=0.0)
    assert out["v2"]["fired"] is True, out["v2"]["conditions"]
    assert out["v2"]["t"] == ts[-1]


def test_v2_blocked_without_cross():
    # Perfectly flat price -> EMA20 never crosses EMA60 -> no v2.
    n = 80
    oi = [100.0] * n
    oi[-4:] = [100, 103, 106, 110]
    o, kl, _ = build(oi, [10.0] * n)
    out = evaluate(o, kl, current_funding=0.0)
    assert out["v2"]["fired"] is False
    assert out["v2"]["conditions"]["ema20>ema60+cross"] is False


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

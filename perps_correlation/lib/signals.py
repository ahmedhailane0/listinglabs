"""Buy v1 / v2 / v3 strategy signals for the manipulated-coin perp screener.

PURE engine — no network, no file IO. Given a token's hourly open-interest series,
hourly OHLCV klines, and funding, it decides whether each Buy setup fires at the
latest CLOSED 1H candle, and scans the trailing `lookback_h` hours to report the
most-recent fire (so the page can show "Buy vN — fired <when>"). Every threshold
is a named constant so build/build_screener.py renders the exact same numbers.

Data contracts (timestamps in whole seconds; the engine snaps them to the hour):
  oi      : list[(t, oi_usd)]                     ascending, hourly
  klines  : list[(t, open, high, low, close, vol)] ascending, hourly
  funding : list[(t, rate)] settlement points  OR  a single current rate via
            `current_funding=` ; `rate` is the per-interval rate (0.0001 = 0.01%).

Correctness-first: if a strategy's required hours are missing (new contract or a
data gap), that strategy returns `insufficient` and NEVER a fire — we don't guess
a Buy from absent data. A wrong/short series can only suppress a signal, not
fabricate one.

The rules (evaluated at hour t; OI[k]/C[k]/H[k]/L[k] = value k hours before t):

  Buy v1 — accumulation breakout
    1 OI[0]>OI[1]>OI[2]>OI[3]      2 OI up >=8% over 3h      3 price up <=8% over 3h
    4 C[0] > max(H[1..6])          5 funding < 0.1%          6 oi%3h / price%3h >= 1.5
    -> size 5-10%, stop = the 6h breakout level max(H[1..6]).

  Buy v2 — OI + EMA golden cross
    1 OI[0]>OI[1]>OI[2]>OI[3]      2 OI up >=5% over 3h
    3 EMA20>EMA60 with a cross-up in the last 10 candles
    4 |C[0]-EMA20|/EMA20 <= 5%     5 oi%3h / price%3h >= 1.0     6 funding < 0.1%
    -> size 10% (15% if oi%3h>=10% and vol>1.5x avg); trend stop = EMA60.

  Buy v3 — washout reversal (the owner's original idea)
    1 OI dropped >=15% within 4h   2 price dropped <=10% within 4h
    3 price-drawdown / OI-drawdown <= 0.5
    4 |funding_8h| <= 0.02%        5 OI rebuilt >=8% off the washout low
    6 C[0] > the high of the 3h right after the washout low
    -> size 5-10%, stop = the washout low.
"""
from __future__ import annotations

HOUR = 3600


def ema(values: list[float], n: int) -> list[float]:
    """Exponential moving average aligned to `values` (same length), seeded on the
    first value. Kept INLINE (pure stdlib) so this engine has zero project import
    deps — the box that runs it needs no matplotlib/chart stack, just Python."""
    if not values:
        return []
    k = 2.0 / (n + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

# ── thresholds (named so the page can quote them) ─────────────────────────────
V1_OI_3H = 0.08          # >= +8% OI over 3h
V1_PRICE_3H_MAX = 0.08   # <= +8% price over 3h
V1_RATIO = 1.5           # oi%3h / price%3h
V2_OI_3H = 0.05          # >= +5% OI over 3h
V2_EMA_FAST, V2_EMA_SLOW = 20, 60
V2_CROSS_LOOKBACK = 10   # golden cross within last N candles
V2_NEAR_EMA = 0.05       # |C - EMA20| / EMA20 <= 5%
V2_RATIO = 1.0
V2_OI_BUMP = 0.10        # oi%3h >= 10% -> bigger size...
V2_VOL_BUMP = 1.5        # ...if vol > 1.5x the trailing average
FUNDING_MAX = 0.001      # v1/v2: per-interval funding < 0.1%
V3_OI_DD = 0.15          # OI drawdown >= 15% in 4h
V3_PRICE_DD_MAX = 0.10   # price drawdown <= 10% in 4h
V3_DD_RATIO_MAX = 0.5    # price_dd / oi_dd <= 0.5
V3_FUNDING_8H = 0.0002   # |funding normalized to 8h| <= 0.02%
V3_REBUILD = 0.08        # OI back >= 8% off the low
V3_WINDOW = 4            # hours
V3_POST_HIGH = 3         # post-washout high window (hours)


def _dominance_ratio(oi_pct: float, price_pct: float) -> float:
    """How many times as fast OI rose vs price (fractional). Price flat/down with
    OI rising = maximal dominance -> +inf (passes any >= threshold), avoiding a
    divide-by-zero. OI not rising -> 0."""
    if price_pct <= 0:
        return float("inf") if oi_pct > 0 else 0.0
    return oi_pct / price_pct


def _maps(oi, klines):
    """Snap each series to whole-hour keys for O(1) aligned lookup."""
    def snap(t):
        return (int(t) // HOUR) * HOUR
    oi_m = {snap(t): v for t, v in oi if v is not None}
    c_m, h_m, l_m, v_m = {}, {}, {}, {}
    for row in klines:
        t = snap(row[0])
        c_m[t] = row[4]
        h_m[t] = row[2]
        l_m[t] = row[3]
        v_m[t] = row[5] if len(row) > 5 else None
    return oi_m, c_m, h_m, l_m, v_m


def _funding_at(funding, current_funding, t):
    """Prevailing per-interval funding at hour t: the most recent settlement <= t,
    else the supplied current rate."""
    if funding:
        best = None
        for ft, fr in funding:
            if ft <= t and (best is None or ft > best[0]):
                best = (ft, fr)
        if best is not None:
            return best[1]
    return current_funding


def _consec_oi_up(oi_m, t):
    """OI[0]>OI[1]>OI[2]>OI[3] and the four values (or None if any hour missing)."""
    vals = [oi_m.get(t - k * HOUR) for k in range(4)]
    if any(v is None for v in vals):
        return None, vals
    return (vals[0] > vals[1] > vals[2] > vals[3]), vals


def _eval_v1(oi_m, c_m, h_m, t, funding):
    up, oiv = _consec_oi_up(oi_m, t)
    c0, c3 = c_m.get(t), c_m.get(t - 3 * HOUR)
    highs = [h_m.get(t - k * HOUR) for k in range(1, 7)]
    if up is None or c0 is None or c3 is None or any(h is None for h in highs) or funding is None:
        return {"insufficient": True}
    oi_pct = (oiv[0] - oiv[3]) / oiv[3] if oiv[3] else 0.0
    price_pct = (c0 - c3) / c3 if c3 else 0.0
    six_high = max(highs)
    cond = {
        "oi_3up": up,
        "oi_3h>=8%": oi_pct >= V1_OI_3H,
        "price_3h<=8%": price_pct <= V1_PRICE_3H_MAX,
        "breaks_6h_high": c0 > six_high,
        "funding<0.1%": funding < FUNDING_MAX,
        "oi/price>=1.5": _dominance_ratio(oi_pct, price_pct) >= V1_RATIO,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": six_high, "position": "5-10%",
            "metrics": {"oi_pct_3h": oi_pct, "price_pct_3h": price_pct, "funding": funding}}


def _eval_v2(oi_m, c_m, v_m, t, funding):
    up, oiv = _consec_oi_up(oi_m, t)
    # contiguous closes ending at t, oldest..newest, for EMA20/60 + cross lookback.
    need = V2_EMA_SLOW + V2_CROSS_LOOKBACK + 1
    closes = []
    for k in range(need - 1, -1, -1):
        c = c_m.get(t - k * HOUR)
        if c is None:
            return {"insufficient": True}
        closes.append(c)
    c0, c3 = c_m.get(t), c_m.get(t - 3 * HOUR)
    if up is None or c3 is None or funding is None:
        return {"insufficient": True}
    e20, e60 = ema(closes, V2_EMA_FAST), ema(closes, V2_EMA_SLOW)
    cur20, cur60 = e20[-1], e60[-1]
    # cross-up (EMA20 crosses above EMA60) within the last N candle transitions
    crossed = any(e20[j - 1] <= e60[j - 1] and e20[j] > e60[j]
                  for j in range(len(closes) - V2_CROSS_LOOKBACK, len(closes)))
    oi_pct = (oiv[0] - oiv[3]) / oiv[3] if oiv[3] else 0.0
    price_pct = (c0 - c3) / c3 if c3 else 0.0
    cond = {
        "oi_3up": up,
        "oi_3h>=5%": oi_pct >= V2_OI_3H,
        "ema20>ema60+cross": (cur20 > cur60) and crossed,
        "near_ema20<=5%": abs(c0 - cur20) / cur20 <= V2_NEAR_EMA if cur20 else False,
        "oi/price>=1.0": _dominance_ratio(oi_pct, price_pct) >= V2_RATIO,
        "funding<0.1%": funding < FUNDING_MAX,
    }
    # position bump if OI surged on heavy volume
    vols = [v_m.get(t - k * HOUR) for k in range(1, 21)]
    vols = [x for x in vols if x is not None]
    v0 = v_m.get(t)
    big = (oi_pct >= V2_OI_BUMP and v0 is not None and vols
           and v0 > V2_VOL_BUMP * (sum(vols) / len(vols)))
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": cur60, "position": "15%" if big else "10%",
            "metrics": {"oi_pct_3h": oi_pct, "price_pct_3h": price_pct,
                        "ema20": cur20, "ema60": cur60, "funding": funding}}


def _eval_v3(oi_m, c_m, h_m, l_m, t, funding, interval_h):
    W = V3_WINDOW
    ois = [oi_m.get(t - k * HOUR) for k in range(W + 1)]      # k=0..4
    highs = [h_m.get(t - k * HOUR) for k in range(W + 1)]
    lows = [l_m.get(t - k * HOUR) for k in range(W + 1)]
    c0 = c_m.get(t)
    if any(x is None for x in ois + highs + lows) or c0 is None or funding is None or not interval_h:
        return {"insufficient": True}
    # OI peak (oldest on ties) then the washout low after it (oldest on ties).
    k_high = max(range(W + 1), key=lambda k: (ois[k], k))
    after_peak = [k for k in range(W + 1) if k < k_high]
    if not after_peak:
        return {"fired": False, "conditions": {"washout": False}, "stop": None, "position": "5-10%"}
    k_low = min(after_peak, key=lambda k: (ois[k], -k))
    oi_high, oi_low = ois[k_high], ois[k_low]
    oi_dd = (oi_high - oi_low) / oi_high if oi_high else 0.0
    rebuild = (ois[0] - oi_low) / oi_low if oi_low else 0.0
    price_high, price_low = max(highs), min(lows)
    price_dd = (price_high - price_low) / price_high if price_high else 0.0
    dd_ratio = (price_dd / oi_dd) if oi_dd else float("inf")
    funding_8h = funding * (8.0 / interval_h)
    # post-washout high: the up-to-3 hours right after the low, before now.
    post_ks = list(range(max(1, k_low - V3_POST_HIGH), k_low))
    post_high = max((highs[k] for k in post_ks), default=None)
    cond = {
        "oi_dd>=15%": oi_dd >= V3_OI_DD,
        "price_dd<=10%": price_dd <= V3_PRICE_DD_MAX,
        "price/oi_dd<=0.5": dd_ratio <= V3_DD_RATIO_MAX,
        "|funding_8h|<=0.02%": abs(funding_8h) <= V3_FUNDING_8H,
        "oi_rebuild>=8%": rebuild >= V3_REBUILD,
        "breaks_postwashout_high": post_high is not None and c0 > post_high,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": price_low, "position": "5-10%",   # stop = the washout (price) low
            "metrics": {"oi_dd": oi_dd, "price_dd": price_dd, "rebuild": rebuild,
                        "funding_8h": funding_8h}}


def evaluate(oi, klines, funding=None, current_funding=None,
             funding_interval_h=8.0, lookback_h=72):
    """Run v1/v2/v3 over the trailing `lookback_h` hours; return, per strategy, the
    most-recent fire (with time, position, stop, per-condition breakdown) or the
    latest-hour evaluation when it never fired / lacks data.

    Returns: {"as_of": t0|None, "v1": {...}, "v2": {...}, "v3": {...}} where each
    strategy dict has {"fired": bool, "t": ts|None, "insufficient": bool,
    "conditions": {...}, "stop": float|None, "position": str|None}."""
    oi_m, c_m, h_m, l_m, v_m = _maps(oi, klines)
    if not oi_m or not c_m:
        return {"as_of": None, "v1": _empty(True), "v2": _empty(True), "v3": _empty(True)}
    t0 = min(max(oi_m), max(c_m))                 # latest hour present in BOTH series
    t0 = (t0 // HOUR) * HOUR

    out = {"as_of": t0}
    runners = {
        "v1": lambda tt, f: _eval_v1(oi_m, c_m, h_m, tt, f),
        "v2": lambda tt, f: _eval_v2(oi_m, c_m, v_m, tt, f),
        "v3": lambda tt, f: _eval_v3(oi_m, c_m, h_m, l_m, tt, f, funding_interval_h),
    }
    for name, run in runners.items():
        latest = None                              # latest-hour result, for display when no fire
        hit = None
        for i in range(lookback_h):
            tt = t0 - i * HOUR
            f = _funding_at(funding, current_funding, tt)
            r = run(tt, f)
            if i == 0:
                latest = r
            if not r.get("insufficient") and r.get("fired"):
                hit = dict(r, t=tt)
                break
        if hit is not None:
            out[name] = {"fired": True, "t": hit["t"], "insufficient": False,
                         "conditions": hit["conditions"], "stop": hit.get("stop"),
                         "position": hit.get("position")}
        elif latest.get("insufficient"):
            out[name] = _empty(insufficient=True)
        else:
            out[name] = {"fired": False, "t": None, "insufficient": False,
                         "conditions": latest.get("conditions", {}),
                         "stop": latest.get("stop"), "position": latest.get("position")}
    return out


def _empty(insufficient: bool = False):
    return {"fired": False, "t": None, "insufficient": insufficient,
            "conditions": {}, "stop": None, "position": None}

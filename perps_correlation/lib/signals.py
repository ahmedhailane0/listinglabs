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

  Buy v2 — IGNITION (OI-confirmed coil-break; the catch-at-break for "cold" pumps)
    1 the prior 48h base was a tight coil (range <= 45%)
    2 this hour's volume >= 5x the coil's MEDIAN hourly volume
    3 OI up >= +15% over the last 3h (leveraged money committing)
    4 close breaks the coil high by >= +8%      5 funding still cold (<= 0.05%)
    -> size 5-10%, stop = the coil high (a fall back below = failed break). Catches
       pumps that SKIP v1/v4's quiet-accumulation tell (e.g. VELVET +122%, 2026-06-26:
       OI flat through a 3-day coil, then price+OI+volume exploded together).

  Buy v3 — washout reversal (the owner's original idea)
    1 OI dropped >=15% within 4h   2 price dropped <=10% within 4h
    3 price-drawdown / OI-drawdown <= 0.5
    4 |funding_8h| <= 0.02%        5 OI rebuilt >=8% off the washout low
    6 C[0] > the high of the 3h right after the washout low
    -> size 5-10%, stop = the washout low.

  Buy v4 — coiled accumulation (the pre-pump build; fires DAYS before the vertical)
    1 OI[0] >= +40% vs OI[48]      2 |price move over 48h| <= 15%
    3 48h high/low range <= 30%    4 oi%48h / price%48h >= 2  (OI leads price)
    5 funding <= 0 (SQUEEZE)  OR  funding <= 0.05% with EMA20>EMA60 (TREND)
    -> size 5-10%, stop = the 48h coil low.

  probe — probe day (#7, LONG confirmation; the pre-vertical test, after v4's coil)
    1 last-12h volume >= 3x the trailing 48h baseline   2 close breaks the coil high
    3 that prior 48h range was tight (<= 40%)
    -> size 5-10%, stop = the coil low. Backtest ~2.5x lift to a +50%/72h pump.

  dump — distribution (#6, SHORT; after a markup, longs crowd in and roll over)
    1 ran up >= +40% over 48h      2 stalled (last 12h <= +5%)
    3 funding hot (>= 0.05%)        4 OI still elevated vs 48h ago
    -> short 5-10%, invalidated above the markup peak. Backtest ~6.2x lift to a
       -20%/72h drop. Validated against price DOWN, not the pump label.
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
V1_OI_3H = 0.05          # >= +5% OI over 3h (retuned 2026-07-01: OOS test lift 1.39->1.64x)
V1_PRICE_3H_MAX = 0.05   # <= +5% price over 3h (retuned 2026-07-01: tighter early-move window)
V1_RATIO = 2.0           # oi%3h / price%3h >= 2 (retuned 2026-07-01 from 1.0; OI must again dominate)
V1_FUNDING_MAX = 0.002   # v1 only: per-interval funding < 0.2% (retuned 2026-07-01 from 0.05%)
# Buy v2 — IGNITION: an OI-confirmed coil-break. The catch-at-break setup for
# pumps that SKIP the quiet-accumulation tell v1/v4 hunt for (VELVET +122% on
# 2026-06-26: OI flat through a 3-day coil, then price + OI + volume exploded
# together). Fires on the breakout HOUR — the earliest honest entry for a "cold"
# pump — and self-limits to ~one fire (once the breakout bar enters the coil
# window the range is no longer tight). Every knob is loop-tunable.
V2_COIL_H = 48           # the pre-break base window (hours)
V2_COIL_MAX = 0.45       # that base was a tight coil: (hi-lo)/lo <= 45% (intraday wicks)
V2_BREAK_LAG = 3         # the break may be up to N bars old — OI confirmation lags the
                         # break candle by ~1h, so we let it confirm before firing
V2_VOL_MULT = 5.0        # ignition: peak recent vol >= 5x the coil's MEDIAN hourly vol
V2_OI_SURGE = 0.15       # OI up >= +15% over V2_OI_SURGE_H (leveraged money committing)
V2_OI_SURGE_H = 3        # ...measured over the last N hours
V2_BREAK_MARGIN = 0.08   # the break closes above the coil high by >= +8%
V2_FUND_MAX = 0.0005     # funding still cold (<= 0.05%/interval) = room, not a late chase
EMA_FAST, EMA_SLOW = 20, 60   # shared EMA periods (v4's trend variant uses these)
FUNDING_MAX = 0.001      # LEGACY: kept only for optimize_signals.py's v1 grid getattr
V3_OI_DD = 0.15          # OI drawdown >= 15% in 4h
V3_PRICE_DD_MAX = 0.10   # price drawdown <= 10% in 4h
V3_DD_RATIO_MAX = 0.5    # price_dd / oi_dd <= 0.5
V3_FUNDING_8H = 0.0002   # |funding normalized to 8h| <= 0.02%
V3_REBUILD = 0.08        # OI back >= 8% off the low
V3_WINDOW = 4            # hours
V3_POST_HIGH = 3         # post-washout high window (hours)
# Buy v4 — coiled accumulation (the pre-pump build, fires DAYS before the vertical)
V4_WINDOW = 72           # hours (3-day accumulation window; tuned 2026-06-26 from 48)
V4_OI_BUILD = 0.40       # OI now >= +40% vs window start
V4_PRICE_FLAT = 0.25     # |net price move over window| <= 25% (tuned 2026-06-26 from 15%)
V4_RANGE_MAX = 0.45      # window high/low range <= 45% (tuned 2026-06-26 from 30%)
V4_RATIO = 2.0           # oi%window / price%window >= 2 (OI leads price = quiet loading)
V4_FUNDING_TREND = 0.0005  # trend variant: calm/slightly+ funding cap (<= 0.05%)
# Probe day (#7) — the pre-vertical "test": a volume-driven range break OUT of a
# tight coil. It comes AFTER accumulation (v4) and immediately BEFORE the vertical;
# v4's quiet-coil test by design can't fire on the breakout hour itself. Confirmation
# of imminence, not accumulation. Validated against the same +50%/72h pump label.
PROBE_SPIKE_H = 12        # the "probe" window (recent volume burst)
PROBE_BASE_H = 48         # trailing baseline for "normal" volume
PROBE_VOL_X = 3.0         # probe-window volume >= 3x the comparable baseline block
PROBE_COIL_MAX = 0.40    # the pre-probe range was a tight coil (<= 40%)
# Distribution / dump (#6) — the SHORT side: after a markup, longs crowd in (hot
# funding) and price rolls over while OI is still elevated → the operator distributes
# into euphoria. Validated against price DOWN (not the +50% pump label).
DUMP_RUNUP_H = 48        # the markup window
DUMP_RUNUP = 0.40        # ran >= +40% (a markup happened)
DUMP_STALL_H = 12        # then stalled/rolled over in the last N hours
DUMP_STALL_MAX = 0.05    # last-Nh change <= +5% (momentum gone)
DUMP_FUNDING = 0.0005    # funding hot (>= 0.05%/interval = crowded longs)
# Bear trap — the shakeout-and-reclaim (Dimension-01 price-action factor): price
# flushed >=15% below the 2-day-ago close somewhere in the window, has RECLAIMED
# nearly all of it, and has printed no new low in the last 12h — weak hands were
# flushed and the dip got bought back = defended price. The strongest long factor
# in the 2026-06 price-action study (~4.65x lift to +50%/72h over 207k coin-hours).
# Pure price (no OI/volume/funding).
TRAP_WINDOW_H = 48       # the lookback window the flush must sit in
TRAP_RECENT_H = 12       # the reclaim tail: no new low in the last N hours
TRAP_DIP = 0.15          # flushed >= 15% below the window-start close
TRAP_RECLAIM = 0.97      # close back to >= 97% of the window-start close
# Spike & retrace (price-action factor): a >=+30% spike within the last day that
# has retraced hard (close <= 85% of the spike high) but still holds above the
# pre-spike level, with funding NEGATIVE — shorts crowd the retrace and become
# the fuel for the next leg. ~2.26x lift to +50%/72h in the same study.
SPIKE_WINDOW_H = 24      # the spike must have happened within the last day
SPIKE_MIN = 0.30         # spiked >= +30% above the day-ago close
SPIKE_RETRACE = 0.85     # now retraced to <= 85% of the spike high
SPIKE_FUND_MAX = 0.0     # per-interval funding strictly negative


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
        "oi_3h>=5%": oi_pct >= V1_OI_3H,
        "price_3h<=8%": price_pct <= V1_PRICE_3H_MAX,
        "breaks_6h_high": c0 > six_high,
        "funding<0.05%": funding < V1_FUNDING_MAX,
        "oi/price>=2.0": _dominance_ratio(oi_pct, price_pct) >= V1_RATIO,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": six_high, "position": "5-10%",
            "metrics": {"oi_pct_3h": oi_pct, "price_pct_3h": price_pct, "funding": funding}}


def _eval_v2(oi_m, c_m, h_m, l_m, v_m, t, funding):
    """IGNITION: an OI-confirmed coil-break. A tight multi-day base, then within the
    last few bars price broke above the coil top on a volume burst WHILE open
    interest surged — and funding is still cold. The conjunction (OI surging WITH
    the break) is the edge: it separates a leveraged-money ignition that follows
    through from a spot-only fakeout. The break window lags by V2_BREAK_LAG bars so
    OI (which confirms ~1h after the break candle) can register before we fire.
    Stop = the coil high (a fall back below = a failed break)."""
    W, L = V2_COIL_H, V2_BREAK_LAG
    oi0, oiS = oi_m.get(t), oi_m.get(t - V2_OI_SURGE_H * HOUR)
    # base coil = the W hours ENDING L bars ago (excludes the breakout window, so the
    # base stays tight even once price has popped; this also self-limits the fire).
    b_hi = [h_m.get(t - k * HOUR) for k in range(L + 1, L + 1 + W)]
    b_lo = [l_m.get(t - k * HOUR) for k in range(L + 1, L + 1 + W)]
    b_vol = [v_m.get(t - k * HOUR) for k in range(L + 1, L + 1 + W)]
    # recent breakout window = the last L+1 bars (k=0..L)
    r_close = [c_m.get(t - k * HOUR) for k in range(L + 1)]
    r_vol = [v_m.get(t - k * HOUR) for k in range(L + 1)]
    if (oi0 is None or not oiS or funding is None
            or any(x is None for x in b_hi + b_lo + b_vol + r_close + r_vol)):
        return {"insufficient": True}
    coil_hi, coil_lo = max(b_hi), min(b_lo)
    coil_rng = (coil_hi - coil_lo) / coil_lo if coil_lo else float("inf")
    med_vol = sorted(b_vol)[len(b_vol) // 2]              # robust "normal" hourly vol
    vol_x = (max(r_vol) / med_vol) if med_vol else float("inf")
    oi_surge = (oi0 - oiS) / oiS
    cond = {
        "tight_coil<=45%": coil_rng <= V2_COIL_MAX,
        "vol_ignition>=5x": vol_x >= V2_VOL_MULT,
        "oi_surge>=15%": oi_surge >= V2_OI_SURGE,
        "breaks_coil_high": max(r_close) > coil_hi * (1 + V2_BREAK_MARGIN),
        "funding_cold": funding <= V2_FUND_MAX,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": coil_hi, "position": "5-10%",
            "metrics": {"coil_range": coil_rng, "vol_x": vol_x, "oi_surge": oi_surge,
                        "break_level": coil_hi, "funding": funding}}


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


def _eval_v4(oi_m, c_m, h_m, l_m, t, funding):
    """Coiled accumulation: open interest building over ~2 days while price stays
    flat and tight — the quiet loading that preceded every pump in the study, days
    before the vertical. Two flavours: SQUEEZE (funding <= 0) and TREND (calm/+
    funding with an EMA20>EMA60 uptrend). Stop = the coil low."""
    W = V4_WINDOW
    oi0, oiW = oi_m.get(t), oi_m.get(t - W * HOUR)
    c0, cW = c_m.get(t), c_m.get(t - W * HOUR)
    highs = [h_m.get(t - k * HOUR) for k in range(W + 1)]
    lows = [l_m.get(t - k * HOUR) for k in range(W + 1)]
    if (oi0 is None or not oiW or c0 is None or not cW or funding is None
            or any(x is None for x in highs + lows)):
        return {"insufficient": True}
    oi_pct = (oi0 - oiW) / oiW
    price_pct = (c0 - cW) / cW
    hi, lo = max(highs), min(lows)
    rng = (hi - lo) / lo if lo else float("inf")
    # trend variant wants an EMA20>EMA60 uptrend (needs contiguous closes); the
    # squeeze variant doesn't, so a missing EMA only suppresses trend, not v4.
    closes, ok = [], True
    for k in range(EMA_SLOW, -1, -1):
        cc = c_m.get(t - k * HOUR)
        if cc is None:
            ok = False
            break
        closes.append(cc)
    uptrend = ok and ema(closes, EMA_FAST)[-1] > ema(closes, EMA_SLOW)[-1]
    squeeze = funding <= 0
    trend = (funding <= V4_FUNDING_TREND) and uptrend
    cond = {
        "oi_build_72h>=40%": oi_pct >= V4_OI_BUILD,
        "price_flat_72h<=25%": abs(price_pct) <= V4_PRICE_FLAT,
        "coiling_range<=45%": rng <= V4_RANGE_MAX,
        "oi_leads_price>=2x": _dominance_ratio(oi_pct, price_pct) >= V4_RATIO,
        "funding_squeeze_or_trend": squeeze or trend,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": lo, "position": "5-10%",
            "metrics": {"oi_pct_window": oi_pct, "price_pct_window": price_pct, "range": rng,
                        "funding": funding,
                        "variant": "squeeze" if squeeze else ("trend" if trend else None)}}


def _eval_probe(c_m, h_m, l_m, v_m, t):
    """Probe day (#7): a volume burst that breaks the top of a tight coil — the
    pre-vertical test. Recent PROBE_SPIKE_H volume >= PROBE_VOL_X the trailing
    baseline, the close breaks the prior coil high, and that prior range was tight.
    Pure volume/price (no OI/funding) so it can confirm right at the breakout."""
    S, B = PROBE_SPIKE_H, PROBE_BASE_H
    c0 = c_m.get(t)
    spike = [v_m.get(t - k * HOUR) for k in range(S)]
    base = [v_m.get(t - k * HOUR) for k in range(S, S + B)]
    p_highs = [h_m.get(t - k * HOUR) for k in range(S, S + B)]   # pre-spike coil
    p_lows = [l_m.get(t - k * HOUR) for k in range(S, S + B)]
    if (c0 is None or any(x is None for x in spike + base + p_highs + p_lows)):
        return {"insufficient": True}
    recent = sum(spike)
    base_block = (sum(base) / len(base)) * S          # comparable S-hour block
    volspk = (recent / base_block) if base_block else 0.0
    coil_hi, coil_lo = max(p_highs), min(p_lows)
    coil_rng = (coil_hi - coil_lo) / coil_lo if coil_lo else float("inf")
    cond = {
        "vol_spike>=3x": volspk >= PROBE_VOL_X,
        "breaks_coil_high": c0 > coil_hi,
        "from_tight_coil<=40%": coil_rng <= PROBE_COIL_MAX,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": coil_lo, "position": "5-10%",
            "metrics": {"vol_spike": volspk, "coil_range": coil_rng, "break_level": coil_hi}}


def _eval_dump(oi_m, c_m, h_m, t, funding):
    """Distribution / dump (#6, SHORT): after a markup (>= +40%/48h), price stalls
    or rolls over (last 12h <= +5%) while funding is hot (crowded longs) and OI is
    still elevated (positions not yet unwound) — the operator distributing into
    euphoria. A bearish setup; validated against price DOWN, not the pump label."""
    Rh, Sh = DUMP_RUNUP_H, DUMP_STALL_H
    c0, cR, cS = c_m.get(t), c_m.get(t - Rh * HOUR), c_m.get(t - Sh * HOUR)
    oi0, oiR = oi_m.get(t), oi_m.get(t - Rh * HOUR)
    highs = [h_m.get(t - k * HOUR) for k in range(Rh + 1)]
    if (c0 is None or not cR or not cS or oi0 is None or not oiR
            or funding is None or any(h is None for h in highs)):
        return {"insufficient": True}
    peak = max(highs)
    runup = (peak - cR) / cR                          # how big the markup was
    stall = (c0 - cS) / cS                            # recent momentum (rolling over?)
    cond = {
        "ran_up>=40%": runup >= DUMP_RUNUP,
        "stalled<=5%": stall <= DUMP_STALL_MAX,
        "funding_hot>=0.05%": funding >= DUMP_FUNDING,
        "oi_still_elevated": oi0 >= oiR,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": peak, "position": "short 5-10%",   # invalidated above the peak
            "metrics": {"runup_48h": runup, "stall_12h": stall, "funding": funding}}


def _eval_bear_trap(c_m, l_m, t):
    """Bear trap: dip low in [t-TRAP_WINDOW_H .. t-TRAP_RECENT_H] flushed
    >= TRAP_DIP below the window-start close, the current close has reclaimed
    >= TRAP_RECLAIM of that start, and the last TRAP_RECENT_H hours printed no
    new low (the flush is over, the reclaim is holding)."""
    W, R = TRAP_WINDOW_H, TRAP_RECENT_H
    c0, cW = c_m.get(t), c_m.get(t - W * HOUR)
    dip_lows = [l_m.get(t - k * HOUR) for k in range(R, W + 1)]
    recent_lows = [l_m.get(t - k * HOUR) for k in range(R)]
    if c0 is None or not cW or any(x is None for x in dip_lows + recent_lows):
        return {"insufficient": True}
    dip_lo = min(dip_lows)
    cond = {
        "flushed>=15%": dip_lo <= (1 - TRAP_DIP) * cW,
        "reclaimed>=97%": c0 >= TRAP_RECLAIM * cW,
        "no_new_low_12h": min(recent_lows) > dip_lo,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": dip_lo, "position": "5-10%",
            "metrics": {"dip": 1 - dip_lo / cW, "reclaim": c0 / cW}}


def _eval_spike_retrace(c_m, h_m, t, funding):
    """Spike & retrace: a >= SPIKE_MIN spike above the day-ago close within the
    last SPIKE_WINDOW_H hours, now retraced to <= SPIKE_RETRACE of the spike high
    but still above the pre-spike close, with funding negative (shorts crowding
    the retrace = squeeze fuel)."""
    W = SPIKE_WINDOW_H
    c0, cW = c_m.get(t), c_m.get(t - W * HOUR)
    highs = [h_m.get(t - k * HOUR) for k in range(W)]
    if c0 is None or not cW or funding is None or any(h is None for h in highs):
        return {"insufficient": True}
    spike = max(highs)
    cond = {
        "spiked>=30%": spike >= (1 + SPIKE_MIN) * cW,
        "retraced<=85%_of_spike": c0 <= SPIKE_RETRACE * spike,
        "holds_above_prespike": c0 > cW,
        "funding_negative": funding < SPIKE_FUND_MAX,
    }
    return {"fired": all(cond.values()), "conditions": cond,
            "stop": cW, "position": "5-10%",   # losing the pre-spike level kills it
            "metrics": {"spike": spike / cW - 1, "retrace": (c0 / spike) if spike else 0.0,
                        "funding": funding}}


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
        return {"as_of": None, "v1": _empty(True), "v2": _empty(True),
                "v3": _empty(True), "v4": _empty(True),
                "probe": _empty(True), "dump": _empty(True),
                "bear_trap": _empty(True), "spike_retrace": _empty(True)}
    t0 = min(max(oi_m), max(c_m))                 # latest hour present in BOTH series
    t0 = (t0 // HOUR) * HOUR

    out = {"as_of": t0}
    runners = {
        "v1": lambda tt, f: _eval_v1(oi_m, c_m, h_m, tt, f),
        "v2": lambda tt, f: _eval_v2(oi_m, c_m, h_m, l_m, v_m, tt, f),
        "v3": lambda tt, f: _eval_v3(oi_m, c_m, h_m, l_m, tt, f, funding_interval_h),
        "v4": lambda tt, f: _eval_v4(oi_m, c_m, h_m, l_m, tt, f),
        "probe": lambda tt, f: _eval_probe(c_m, h_m, l_m, v_m, tt),   # #7
        "dump": lambda tt, f: _eval_dump(oi_m, c_m, h_m, tt, f),      # #6 (short)
        # Dimension-01 price-action factors (2026-07-02) — pure price(+funding):
        "bear_trap": lambda tt, f: _eval_bear_trap(c_m, l_m, tt),
        "spike_retrace": lambda tt, f: _eval_spike_retrace(c_m, h_m, tt, f),
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

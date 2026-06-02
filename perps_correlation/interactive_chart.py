"""Build an interactive Plotly candlestick for a listing, with the listing
events marked on it. Returned as an HTML snippet (div + script) to embed in a
detail page; Plotly's JS is loaded once per page separately.

Features: zoom / pan / hover, a timeframe switcher (5m / 15m / 1h / 4h), and a
show/hide toggle for the listing markers + announcement arrow.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import plotly.graph_objects as go

from listing_chart import parse_iso

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"

from venues import venue_color

TIMEFRAMES = [("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)]


def _load_rows(token: str) -> list | None:
    p = CACHE / f"{token.lower()}_klines_5m_alpha.json"
    if not p.exists():
        return None
    rows = json.loads(p.read_text(encoding="utf-8")).get("rows")
    return rows or None


def _aggregate(rows: list, minutes: int) -> tuple[list, list, list, list, list]:
    """Bucket 5m OHLC rows into `minutes` candles (open=first, close=last)."""
    bucket_ms = minutes * 60_000
    buckets: dict[int, list] = {}
    for ts, o, h, l, c in rows:
        key = (ts // bucket_ms) * bucket_ms
        b = buckets.get(key)
        if b is None:
            buckets[key] = [o, h, l, c]
        else:
            b[1] = max(b[1], h)
            b[2] = min(b[2], l)
            b[3] = c
    xs, op, hi, lo, cl = [], [], [], [], []
    for key in sorted(buckets):
        o, h, l, c = buckets[key]
        xs.append(datetime.fromtimestamp(key / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"))
        op.append(o); hi.append(h); lo.append(l); cl.append(c)
    return xs, op, hi, lo, cl


def _close_at_or_before(rows: list, t: datetime) -> float:
    tms = int(t.timestamp() * 1000)
    prev = rows[0][4]
    for ts, _o, _h, _l, c in rows:
        if ts > tms:
            break
        prev = c
    return prev


def _pre_move_close(rows: list, t: datetime) -> float:
    """Close of the candle before the one containing t (foot of an in-bar move)."""
    tms = int(t.timestamp() * 1000)
    prev2, prev1 = rows[0][4], rows[0][4]
    for ts, _o, _h, _l, c in rows:
        if ts > tms:
            break
        prev2, prev1 = prev1, c
    return prev2


def chart_html(cfg: dict, height: int = 560, announcements: dict | None = None) -> str | None:
    token = cfg["token"]
    rows = _load_rows(token)
    if not rows:
        return None

    # Pick price precision from the data so sub-cent tokens don't render as
    # "$0.0000" (e.g. NEX trades around 0.0000036).
    pos = [r[4] for r in rows if r[4] > 0]
    ref = min(pos) if pos else 1.0
    dec = max(4, 2 - math.floor(math.log10(ref))) if ref > 0 else 4
    pfmt = f".{dec}f"

    # Default view = the launch-reaction window, even though the cached candles now
    # extend to "now" (refresh_klines.py keeps them current). The reaction stays the
    # headline; users pan/scroll right to follow the token's full live history.
    _win_lo = cfg.get("window_start_utc")
    _win_hi = cfg.get("window_end_utc")
    # Default view shows the FULL chart: launch (the data's left edge / window_start)
    # through the most recent candle. A small right-pad keeps the latest point off the
    # frame edge. fitY (below) scales the y-axis to whatever x-range is in view.
    lo_dt = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
    if _win_lo:
        try:
            lo_dt = min(lo_dt, parse_iso(_win_lo))
        except Exception:
            pass
    hi_dt = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc)
    pad = (hi_dt - lo_dt) * 0.02 or timedelta(hours=6)
    win_range = [lo_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 (hi_dt + pad).strftime("%Y-%m-%dT%H:%M:%SZ")]

    # L-2: bound the inline payload. Long histories (esp. Binance-fallback tokens with
    # 100k+ candles) would make pages multi-MB. Keep full 5m resolution where it's
    # actually viewed — the default window + the last 3 days — and decimate everything
    # older to hourly. The launch reaction stays crisp; old tails shrink ~12x.
    if len(rows) > 4000:
        lo_ms = int(parse_iso(win_range[0]).timestamp()*1000) if win_range else 0
        hi_ms = int(parse_iso(win_range[1]).timestamp()*1000) if win_range else 1 << 62
        recent_ms = rows[-1][0] - 3*86400*1000
        rows = [r for r in rows
                if (lo_ms <= r[0] <= hi_ms) or r[0] >= recent_ms
                or (r[0] % 3_600_000) < 300_000]

    fig = go.Figure()
    for i, (_name, mins) in enumerate(TIMEFRAMES):
        xs, _op, _hi, _lo, cl = _aggregate(rows, mins)
        fig.add_trace(go.Scatter(
            x=xs, y=cl, name=f"{token} {_name}", visible=(i == 0),
            mode="lines", line=dict(color="#1f4e79", width=2, shape="linear"),
            fill="tozeroy", fillcolor="rgba(31,78,121,0.07)",
            hovertemplate="%{x|%b %d %H:%M}  <b>$%{y:" + pfmt + "}</b><extra></extra>",
        ))

    # Listing markers: a thin vertical guide line per event plus a hover-only dot
    # on the price line. No always-on text labels — they overlap badly when
    # several venues list within minutes of each other; the dot's hover shows
    # which venue + time instead.
    # Only mark events that fall within the price data's time range — otherwise a
    # listing months after the charted window (or before it) would stretch the
    # x-axis and float in empty space with no price under it.
    t_lo, t_hi = rows[0][0], rows[-1][0]
    TOL_MS = 2 * 86400 * 1000  # listings are date-stamped; the price data may start
    # a few hours into that day, so clamp markers within ~2 days of the data edge
    # onto the edge instead of dropping them. Events genuinely far outside (e.g. an
    # Alpha listing months before any price exists) stay clipped -> events table only.

    def _placed_x(t):
        """Return (clamped_x_string, clamped_dt) if the event is within tolerance
        of the data range, else None."""
        ms = int(t.timestamp() * 1000)
        if ms < t_lo - TOL_MS or ms > t_hi + TOL_MS:
            return None
        cms = min(max(ms, t_lo), t_hi)
        cdt = datetime.fromtimestamp(cms / 1000, tz=timezone.utc)
        return cdt.strftime("%Y-%m-%d %H:%M"), cdt

    shapes = []
    mx, my, mcolor, mtext = [], [], [], []
    for i, ev in enumerate(sorted(cfg.get("events", []), key=lambda e: e["iso_time_utc"])):
        t = parse_iso(ev["iso_time_utc"])
        placed = _placed_x(t)
        if placed is None:
            continue
        x, cdt = placed
        color = venue_color(ev["exchange"])
        shapes.append(dict(type="line", xref="x", yref="paper", x0=x, x1=x,
                           y0=0, y1=1, opacity=0.5,
                           line=dict(color=color, width=1, dash="dot")))
        mx.append(x)
        my.append(_close_at_or_before(rows, cdt))
        mcolor.append(color)
        mtext.append(f"<b>{ev['exchange']}</b><br>{t.strftime('%Y-%m-%d %H:%M')} UTC")
    fig.add_trace(go.Scatter(
        x=mx, y=my, mode="markers", name="Listings", visible=True,
        marker=dict(size=9, color=mcolor, symbol="circle",
                    line=dict(color="white", width=1.5)),
        customdata=mtext, hovertemplate="%{customdata}<extra></extra>",
    ))

    # Announcement markers: square pins along the BOTTOM of the chart at each
    # article's PUBLISH date (when the exchange said it would list — distinct from
    # the listing moment above). Hover shows the date; not clickable (the article
    # link lives in the Listing Events table's Note column). Always-on trace, kept
    # visible across timeframe toggles like the Listings trace.
    ax, ay, atext = [], [], []
    for label, a in (announcements or {}).items():
        d = a.get("date") if isinstance(a, dict) else None
        if not d:
            continue
        placed = _placed_x(parse_iso(d))
        if placed is None:
            continue
        ax.append(placed[0])
        ay.append(0.0)  # pinned to the x-axis via the fixed overlay axis y2 (below)
        title = (a.get("title") or "")[:90]
        atext.append(f"<b>{label} — listing announced</b><br>"
                     f"{placed[1].strftime('%Y-%m-%d')}<br>{title}")
    # Pinned to the x-axis on a fixed [0,1] overlay axis (y2) so the squares always sit
    # ON the axis line, never floating in the plot and never drifting when the price
    # y-axis is zoomed (audit/UX: "announcement square should stick to the X axis").
    fig.add_trace(go.Scatter(
        x=ax, y=ay, mode="markers", name="Announcements", visible=True, yaxis="y2",
        marker=dict(size=11, color="#e67e22", symbol="triangle-up",
                    line=dict(color="white", width=1)),
        customdata=atext, hovertemplate="%{customdata}<extra></extra>",
    ))

    # announcement annotations: arrow to the foot of the move (no vertical line)
    annotations = []
    for ann in cfg.get("annotations", []):
        t = parse_iso(ann["iso_time_utc"])
        if not (t_lo <= int(t.timestamp() * 1000) <= t_hi):
            continue
        annotations.append(dict(
            x=t.strftime("%Y-%m-%d %H:%M"), y=_pre_move_close(rows, t),
            xref="x", yref="y", text=f"{ann['label']} {t.strftime('%H:%M')}",
            showarrow=True, arrowhead=2, arrowwidth=1, arrowcolor="#555555",
            ax=-55, ay=-45, font=dict(size=10, color="#555555", family="serif"),
            bgcolor="rgba(255,255,255,0.85)", bordercolor="#999999", borderpad=2))

    # Always-on listing labels grouped by day, so several venues listing on the
    # same day (which would overlap into one dot) all stay visible. Labels stack
    # vertically when groups are close in time.
    SHORT = {"Binance Alpha": "Alpha", "Binance Spot": "BN Spot", "Binance Perp": "Perp",
             "Coinbase Spot": "Coinbase", "Coinbase INTX": "CB-INTX", "Upbit": "Upbit",
             "Bithumb": "Bithumb", "Coinone": "Coinone"}
    evs = []
    for ev in sorted(cfg.get("events", []), key=lambda e: e["iso_time_utc"]):
        t = parse_iso(ev["iso_time_utc"])
        placed = _placed_x(t)
        if placed is None:
            continue
        evs.append((ev, t, placed[1]))  # (event, real time, clamped dt)
    groups: list[list] = []
    last_day = None
    for ev, t, cdt in evs:
        day = t.strftime("%Y-%m-%d")  # group by the real listing day
        if groups and day == last_day:
            groups[-1].append((ev, t, cdt))
        else:
            groups.append([(ev, t, cdt)])
        last_day = day
    span_ms = (t_hi - t_lo) or 1
    min_dx = span_ms * 0.085
    placed: list[tuple[int, int]] = []
    label_anns = []  # the venue text boxes — off by default (they cluster ugly),
                     # toggled on via the "Labels" button; the dots' hover + the
                     # color-matched Listing Events table convey the same info.
    for g in groups:
        gt = g[0][2]  # clamped time for x-position
        gx = int(gt.timestamp() * 1000)
        used = {r for xx, r in placed if abs(xx - gx) < min_dx}
        rung = 0
        while rung in used:
            rung += 1
        placed.append((gx, rung))
        shorts = " · ".join(SHORT.get(ev["exchange"], ev["exchange"]) for ev, *_ in g)
        color = (venue_color("Binance Alpha") if any(ev["exchange"] == "Binance Alpha" for ev, *_ in g)
                 else venue_color(g[0][0]["exchange"]))
        real_date = g[0][1].strftime("%m-%d")  # real listing date for the label text
        label_anns.append(dict(
            x=gt.strftime("%Y-%m-%d %H:%M"), y=1.0, xref="x", yref="paper",
            yanchor="top", yshift=-4 - rung * 24, xanchor="left", xshift=3,
            text=f"{shorts} · {real_date}", showarrow=False,
            font=dict(size=9, color=color), align="left",
            bgcolor="rgba(255,255,255,0.92)", bordercolor=color, borderwidth=1, borderpad=2))

    n_tf = len(TIMEFRAMES)
    tf_buttons = []
    for i, (name, _m) in enumerate(TIMEFRAMES):
        # keep the Listings + Announcements marker traces (last two) visible across timeframes
        tf_buttons.append(dict(label=name, method="update",
                               args=[{"visible": [j == i for j in range(n_tf)] + [True, True]}]))
    # Lines + dots always show; this toggles only the (clustering-prone) text
    # labels. Default is off — see label_anns above.
    marker_buttons = [
        dict(label="Labels: off", method="relayout",
             args=[{"annotations": annotations}]),
        dict(label="Labels: on", method="relayout",
             args=[{"annotations": annotations + label_anns}]),
    ]

    fig.update_layout(
        shapes=shapes, annotations=annotations,
        height=height, margin=dict(l=58, r=24, t=64, b=44),
        font=dict(family="Segoe UI, -apple-system, Roboto, sans-serif",
                  size=12, color="#1d2733"),
        paper_bgcolor="white", plot_bgcolor="white",
        xaxis=dict(rangeslider=dict(visible=False), title=None,
                   range=win_range, autorange=(win_range is None),
                   showgrid=False, showline=True, linecolor="#e1e7ee",
                   ticks="outside", tickcolor="#e1e7ee", tickfont=dict(size=11),
                   showspikes=True, spikemode="across", spikethickness=1,
                   spikedash="solid", spikecolor="#c5ccd3"),
        yaxis=dict(title=dict(text="Price (USD)", font=dict(size=11, color="#6b7785")),
                   tickprefix="$", tickformat=pfmt, showgrid=True,
                   gridcolor="#eef2f6", zeroline=False, tickfont=dict(size=11)),
        # fixed [0,1] overlay so the announcement pins stick to the x-axis baseline
        yaxis2=dict(overlaying="y", side="left", range=[0, 1], visible=False,
                    fixedrange=True),
        hoverlabel=dict(bgcolor="white", bordercolor="#e1e7ee",
                        font=dict(size=12, color="#1d2733")),
        hovermode="x", template="plotly_white", showlegend=False,
        dragmode="pan",
        updatemenus=[
            dict(type="buttons", direction="right", x=0, xanchor="left",
                 y=1.12, yanchor="top", showactive=True, buttons=tf_buttons,
                 pad=dict(r=4, t=2)),
            dict(type="buttons", direction="right", x=1, xanchor="right",
                 y=1.12, yanchor="top", showactive=False, buttons=marker_buttons,
                 pad=dict(l=4, t=2)),
        ],
    )
    div_id = f"chart-{token.lower()}"
    # Show a curated modebar (zoom / pan / box-zoom / reset / PNG) on hover so users
    # have real chart controls; drop the noisy/duplicate tools. It sits top-right
    # inside the plot, clear of the custom timeframe buttons in the top margin.
    snippet = fig.to_html(full_html=False, include_plotlyjs=False, div_id=div_id,
                          config={"scrollZoom": True, "displaylogo": False,
                                  "displayModeBar": "hover", "responsive": True,
                                  "modeBarButtonsToRemove": ["lasso2d", "select2d",
                                      "autoScale2d", "zoomIn2d", "zoomOut2d",
                                      "toggleSpikelines", "hoverCompareCartesian",
                                      "hoverClosestCartesian"]})
    return snippet + _autofit_js(div_id)


def _autofit_js(div_id: str) -> str:
    """On x-zoom/pan, rescale the y-axis to the data in view so zooming reveals
    detail instead of just stretching the chart horizontally."""
    return f"""
<script>
(function() {{
  var gd = document.getElementById("{div_id}");
  if (!gd) return;
  var lock = false;
  function lineIdx() {{
    for (var i = 0; i < gd.data.length; i++) {{
      var d = gd.data[i];
      if (d.mode === "lines" && d.visible !== false && d.visible !== "legendonly") return i;
    }}
    return 0;
  }}
  function fitY() {{
    var tr = gd.data[lineIdx()];
    var ax = gd.layout.xaxis;
    if (!tr || !ax || !ax.range) return;
    var lo = new Date(ax.range[0]).getTime(), hi = new Date(ax.range[1]).getTime();
    var ys = [];
    for (var k = 0; k < tr.x.length; k++) {{
      var tx = new Date(tr.x[k]).getTime();
      if (tx >= lo && tx <= hi && tr.y[k] != null) ys.push(tr.y[k]);
    }}
    if (ys.length < 2) return;
    var mn = Math.min.apply(null, ys), mx = Math.max.apply(null, ys);
    var pad = (mx - mn) * 0.10 || mx * 0.02;
    lock = true;
    Plotly.relayout(gd, {{"yaxis.range": [mn - pad, mx + pad]}}).then(function() {{ lock = false; }});
  }}
  function attach() {{
    if (!gd.on) {{ setTimeout(attach, 60); return; }}
    gd.on("plotly_relayout", function(e) {{
      if (lock) return;
      if ("xaxis.autorange" in e) return;            // double-click reset: let it auto
      if (("xaxis.range[0]" in e) || ("xaxis.range" in e)) fitY();
    }});
    gd.on("plotly_restyle", function() {{             // timeframe switch while zoomed
      var ax = gd.layout.xaxis;
      if (ax && ax.range && ax.autorange !== true) fitY();
    }});
  }}
  attach();
  // Fit the y-axis to the default (launch-window) x-range on first render, so the
  // reaction isn't squished by later all-time highs that sit off-screen to the right.
  if (gd.once) gd.once("plotly_afterplot", function() {{ try {{ fitY(); }} catch (e) {{}} }});
  else setTimeout(function() {{ try {{ fitY(); }} catch (e) {{}} }}, 200);
}})();
</script>"""

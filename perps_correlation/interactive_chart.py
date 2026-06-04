"""Build an interactive **TradingView Lightweight Charts** price chart for a
listing, with the listing events + announcement marked on it. Returned as an
HTML snippet (div + script) to embed in a detail page; the Lightweight Charts
library is loaded once per page separately (vendored, see build_listing_report).

Why Lightweight Charts (not Plotly): it's a purpose-built financial charting
library — ~160KB, robust, with native series markers and a crosshair. The old
Plotly path was heavy and its custom y-autofit glue was flaky.

Features: zoom / pan / crosshair tooltip, a timeframe switcher (5m / 15m / 1h /
4h), listing markers (one per venue, colored, with a short label) and an
announcement marker.

SYNC FIX (the important bit): most tracked tokens only have a *date* for their
Binance Alpha listing — stored as a `00:00:00Z` placeholder — so a naive marker
lands at midnight, hours off from the real price reaction. Here we **derive the
real Alpha listing moment from the data**: the on-chain pool's first candle is
the pool's birth ≈ the listing. So a placeholder Alpha event is snapped to the
first candle, and unresolvable midnight CEX events are dropped from the chart
(they stay in the Listing Events table). See `_resolved_events`.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from listing_chart import parse_iso
from venues import venue_color

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"

TIMEFRAMES = [("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)]

# Listings are date-stamped; the price data may start a few hours into that day,
# so clamp precise markers within ~2 days of the data edge onto the edge instead
# of dropping them. Events genuinely far outside stay clipped (table only).
TOL_MS = 2 * 86400 * 1000

SHORT = {"Binance Alpha": "Alpha", "Binance Spot": "BN Spot", "Binance Perp": "Perp",
         "Coinbase Spot": "Coinbase", "Coinbase INTX": "CB-INTX", "Upbit": "Upbit",
         "Bithumb": "Bithumb", "Coinone": "Coinone"}


def _load_rows(token: str) -> list | None:
    p = CACHE / f"{token.lower()}_klines_5m_alpha.json"
    if not p.exists():
        return None
    rows = json.loads(p.read_text(encoding="utf-8")).get("rows")
    return rows or None


def _is_placeholder(iso: str) -> bool:
    """A date-only listing entry, stored at midnight UTC — not a real observed time."""
    return iso.endswith("00:00:00Z") or iso.endswith("00:00:00+00:00")


def first_candle_dt(token: str) -> datetime | None:
    """Timestamp of the on-chain pool's first candle ≈ the real Alpha listing
    moment (the pool is born when the token starts trading). Used to snap
    placeholder Alpha events onto the actual price reaction."""
    rows = _load_rows(token)
    if not rows:
        return None
    return datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)


def _resolved_events(cfg: dict, rows: list) -> list[tuple[dict, int]]:
    """(event, epoch_ms) pairs to actually plot, with placeholder Alpha events
    snapped to the first candle and unresolvable midnight events dropped.

    - Binance Alpha at a `00:00Z` placeholder -> snapped to the first candle.
    - Any other event at a `00:00Z` placeholder -> dropped (we can't place it
      from on-chain data; it remains in the Listing Events table).
    - Precise events -> plotted at their real time, clamped to the data edge if
      within TOL.
    """
    t_lo, t_hi = rows[0][0], rows[-1][0]
    first_dt = datetime.fromtimestamp(t_lo / 1000, tz=timezone.utc)
    out: list[tuple[dict, int]] = []
    for ev in cfg.get("events", []):
        iso = ev.get("iso_time_utc")
        if not iso:
            continue
        if _is_placeholder(iso):
            if ev.get("exchange") == "Binance Alpha":
                t = first_dt                       # snap to pool birth = listing
            else:
                continue                           # unresolvable -> table only
        else:
            t = parse_iso(iso)
        ms = int(t.timestamp() * 1000)
        if ms < t_lo - TOL_MS or ms > t_hi + TOL_MS:
            continue
        out.append((ev, min(max(ms, t_lo), t_hi)))
    return out


def chart_html(cfg: dict, height: int = 560, announcements: dict | None = None) -> str | None:
    token = cfg["token"]
    rows = _load_rows(token)
    if not rows:
        return None

    # Price precision from the data so sub-cent tokens don't render as "$0.0000".
    pos = [r[4] for r in rows if r[4] > 0]
    ref = min(pos) if pos else 1.0
    dec = max(4, 2 - math.floor(math.log10(ref))) if ref > 0 else 4

    # Bound the inline payload. Long histories (Binance-fallback tokens with 100k+
    # candles) would make pages multi-MB. Keep full 5m resolution for the launch
    # window + the last 3 days; decimate older candles to hourly. ~12x smaller tail.
    t_lo, t_hi = rows[0][0], rows[-1][0]
    if len(rows) > 4000:
        recent_ms = t_hi - 3 * 86400 * 1000
        win_hi_ms = t_lo + 7 * 86400 * 1000   # launch window ≈ first week, kept crisp
        rows = [r for r in rows
                if (t_lo <= r[0] <= win_hi_ms) or r[0] >= recent_ms
                or (r[0] % 3_600_000) < 300_000]

    # Compact rows for JS: [tsSeconds, o, h, l, c]. (Aggregation + marker bucketing
    # happen client-side so the timeframe switcher is instant and the payload is one
    # series.) Round to the data's precision to shave bytes.
    q = dec + 2
    js_rows = [[r[0] // 1000, round(r[1], q), round(r[2], q), round(r[3], q), round(r[4], q)]
               for r in rows]

    # Listing markers (auto-derived; see _resolved_events).
    listing_markers = []
    for ev, ms in _resolved_events(cfg, rows):
        ex = ev["exchange"]
        listing_markers.append({
            "time": ms // 1000, "position": "aboveBar", "shape": "circle",
            "color": venue_color(ex), "text": SHORT.get(ex, ex),
        })

    # Announcement markers: where an announcement carries a date, pin it on the
    # x-axis (belowBar arrow). Most tokens have none yet — those simply show no
    # announcement marker (the chart stays clean rather than guessing a time).
    ann_markers = []
    for label, a in (announcements or {}).items():
        d = a.get("date") if isinstance(a, dict) else None
        if not d:
            continue
        try:
            t = parse_iso(d)
        except Exception:
            continue
        ms = int(t.timestamp() * 1000)
        if ms < t_lo - TOL_MS or ms > t_hi + TOL_MS:
            continue
        ms = min(max(ms, t_lo), t_hi)
        ann_markers.append({
            "time": ms // 1000, "position": "belowBar", "shape": "arrowUp",
            "color": "#e67e22", "text": f"{label} announced",
        })

    # Default view frames the listing activity: from the first candle to a little
    # past the last listing/announcement marker, so every marker is on-screen by
    # default and the reaction is the headline. Users pan/scroll right for the full
    # live history. Bounded to [24h, 10d] so a same-day cluster still shows some
    # price context and a far-out event doesn't blow the window open. A small left
    # pad keeps launch markers off the price axis (Lightweight Charts happily shows
    # empty space left of the first candle).
    marker_secs = [m["time"] for m in listing_markers + ann_markers]
    last_ev_ms = (max(marker_secs) * 1000) if marker_secs else t_lo
    win_to_ms = min(t_hi,
                    max(t_lo + 24 * 3600 * 1000, last_ev_ms + 6 * 3600 * 1000),
                    t_lo + 10 * 86400 * 1000)
    left_pad = max(2 * 3600 * 1000, (win_to_ms - t_lo) // 25)
    win_from = (t_lo - left_pad) // 1000
    win_to = win_to_ms // 1000

    div_id = f"tvchart-{token.lower()}"
    cfg_js = json.dumps({
        "rows": js_rows, "dec": dec,
        "listing": listing_markers, "ann": ann_markers,
        "win": {"from": win_from, "to": win_to},
        "tfs": TIMEFRAMES,
    }, separators=(",", ":"))

    toolbar = "".join(
        f'<button class="tv-tf{" active" if i == 0 else ""}" data-m="{m}">{name}</button>'
        for i, (name, m) in enumerate(TIMEFRAMES)
    )
    return (
        f'<div class="tvchart-wrap" style="height:{height}px">'
        f'<div class="tv-toolbar">{toolbar}'
        f'<button class="tv-reset" title="Reset to launch window">⤺ launch</button>'
        f'<span class="tv-legend"><i class="dot"></i>listing '
        f'<i class="tri"></i>announcement</span></div>'
        f'<div id="{div_id}" class="tvchart"></div>'
        f'<div class="tv-tip" id="{div_id}-tip"></div>'
        f'</div>'
        f'<script>(function(){{var CFG={cfg_js};{_CHART_JS}'
        f'\nmount("{div_id}",CFG);}})();</script>'
    )


# Client-side chart builder, shared by every embedded chart on the page. Pure
# Lightweight Charts v4 API. Aggregates the 5m rows into the active timeframe and
# snaps markers to the matching bucket so they always land on a real data point.
_CHART_JS = r"""
function mount(id, cfg){
  var el = document.getElementById(id);
  if(!el || !window.LightweightCharts){ return; }
  var DEC = cfg.dec, minMove = 1/Math.pow(10, DEC);
  var LC = window.LightweightCharts;
  var chart = LC.createChart(el, {
    width: el.clientWidth || 800,
    height: el.clientHeight || (el.parentElement ? el.parentElement.clientHeight : 520) || 520,
    layout: { background:{ type:'solid', color:'#ffffff' }, textColor:'#1d2733',
              fontFamily:'Segoe UI, -apple-system, Roboto, sans-serif', fontSize:12 },
    grid: { vertLines:{ visible:false }, horzLines:{ color:'#eef2f6' } },
    rightPriceScale: { borderColor:'#e1e7ee' },
    timeScale: { borderColor:'#e1e7ee', timeVisible:true, secondsVisible:false },
    crosshair: { mode: LC.CrosshairMode.Normal,
                 vertLine:{ color:'#c5ccd3', width:1, style:0, labelBackgroundColor:'#1f4e79' },
                 horzLine:{ color:'#c5ccd3', width:1, style:0, labelBackgroundColor:'#1f4e79' } },
    localization: { priceFormatter: function(p){ return '$' + p.toFixed(DEC); } },
    handleScale: true, handleScroll: true,
  });
  var series = chart.addAreaSeries({
    lineColor:'#1f4e79', topColor:'rgba(31,78,121,0.18)', bottomColor:'rgba(31,78,121,0.02)',
    lineWidth:2, priceFormat:{ type:'price', precision:DEC, minMove:minMove },
    priceLineVisible:false, lastValueVisible:true,
  });

  function agg(rows, mins){
    var bs = mins*60, map = {}, order = [];
    for(var i=0;i<rows.length;i++){
      var r = rows[i], k = Math.floor(r[0]/bs)*bs;
      if(map[k]===undefined){ order.push(k); }
      map[k] = r[4];                       // close = last row in the bucket
    }
    order.sort(function(a,b){ return a-b; });
    return order.map(function(k){ return { time:k, value:map[k] }; });
  }
  function bucket(t, mins){ var bs = mins*60; return Math.floor(t/bs)*bs; }
  function markers(mins){
    var out = [];
    [].concat(cfg.listing, cfg.ann).forEach(function(m){
      out.push({ time: bucket(m.time, mins), position:m.position, color:m.color,
                 shape:m.shape, text:m.text });
    });
    out.sort(function(a,b){ return a.time-b.time; });
    return out;
  }

  var active = cfg.tfs[0][1];
  function render(mins){
    active = mins;
    series.setData(agg(cfg.rows, mins));
    series.setMarkers(markers(mins));
  }
  function toLaunch(){
    if(cfg.win){ chart.timeScale().setVisibleRange({ from:cfg.win.from, to:cfg.win.to }); }
  }
  render(active); toLaunch();

  // Crosshair tooltip (date + price), floating inside the wrap.
  var wrap = el.parentElement, tip = document.getElementById(id+'-tip');
  chart.subscribeCrosshairMove(function(p){
    if(!p || !p.time || !p.point){ if(tip) tip.style.display='none'; return; }
    var d = p.seriesData.get(series);
    if(!d){ if(tip) tip.style.display='none'; return; }
    var price = (d.value!==undefined)? d.value : d.close;
    var dt = new Date(p.time*1000).toISOString().slice(0,16).replace('T',' ');
    if(tip){
      tip.innerHTML = '<b>$'+price.toFixed(DEC)+'</b><br>'+dt+' UTC';
      tip.style.display='block';
      var x = p.point.x + 16, y = p.point.y + 12;
      if(x > wrap.clientWidth - 130){ x = p.point.x - 130; }
      tip.style.left = x+'px'; tip.style.top = y+'px';
    }
  });

  // Toolbar: timeframe switch + reset-to-launch.
  wrap.querySelectorAll('.tv-tf').forEach(function(b){
    b.addEventListener('click', function(){
      wrap.querySelectorAll('.tv-tf').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      render(parseInt(b.dataset.m,10));
    });
  });
  var rb = wrap.querySelector('.tv-reset');
  if(rb){ rb.addEventListener('click', toLaunch); }

  // Keep width in sync with the responsive card.
  if(window.ResizeObserver){
    new ResizeObserver(function(){ chart.applyOptions({ width: el.clientWidth }); }).observe(el);
  } else {
    window.addEventListener('resize', function(){ chart.applyOptions({ width: el.clientWidth }); });
  }
}
"""


def _autofit_js(div_id: str) -> str:
    """Plotly y-axis autofit glue, kept for the OTHER reports that still render with
    Plotly (e.g. build_scams.py's OI/funding history charts). The listing reaction
    charts no longer use this — they're rendered by Lightweight Charts above. On
    x-zoom/pan, rescale the y-axis to the data in view so zooming reveals detail
    instead of just stretching the chart horizontally."""
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
  if (gd.once) gd.once("plotly_afterplot", function() {{ try {{ fitY(); }} catch (e) {{}} }});
  else setTimeout(function() {{ try {{ fitY(); }} catch (e) {{}} }}, 200);
}})();
</script>"""

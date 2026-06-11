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

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from lib.listing_chart import parse_iso
from lib.venues import venue_color

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
CACHE = HERE.parent / "cache"

TIMEFRAMES = [("5m", 5), ("15m", 15), ("1h", 60), ("4h", 240)]

# Listings are date-stamped; the price data may start a few hours into that day,
# so clamp precise markers within ~2 days of the data edge onto the edge instead
# of dropping them. Events genuinely far outside stay clipped (table only).
TOL_MS = 2 * 86400 * 1000

SHORT = {"Binance Alpha": "Alpha", "Binance Spot": "BN Spot", "Binance Perp": "Perp",
         "Coinbase Spot": "Coinbase", "Coinbase INTX": "CB-INTX", "Upbit": "Upbit",
         "Bithumb": "Bithumb", "Coinone": "Coinone"}


from functools import lru_cache


@lru_cache(maxsize=None)
def _load_rows(token: str) -> list | None:
    """Cached per process: a build touches each token's kline JSON ~6 times
    (sparkline, list row, metrics ×2, chart, annotations) — parse it once.
    Callers must treat the returned list as read-only."""
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
            # No on-chart text — the label would clutter the plot (and crowds badly
            # when a token has several announcements). The marker is just a dot; the
            # `label` rides along so the crosshair tooltip can reveal it on hover.
            "time": ms // 1000, "position": "belowBar", "shape": "arrowUp",
            "color": "#e67e22", "text": "", "label": label,
        })

    # The DEFAULT view shows the FULL history fitted to the frame ("All", like
    # CoinMarketCap) — not the launch window — so the chart reads as one continuous
    # price history instead of a zoomed-in launch with the rest crammed off-screen.
    # The "launch" button still jumps to the reaction window below (win_*), and the
    # "all" button returns to the full fit.
    marker_secs = [m["time"] for m in listing_markers + ann_markers]
    last_ev_ms = (max(marker_secs) * 1000) if marker_secs else t_lo
    win_to_ms = min(t_hi,
                    max(t_lo + 24 * 3600 * 1000, last_ev_ms + 6 * 3600 * 1000),
                    t_lo + 10 * 86400 * 1000)
    left_pad = max(2 * 3600 * 1000, (win_to_ms - t_lo) // 25)
    win_from = (t_lo - left_pad) // 1000
    win_to = win_to_ms // 1000

    # Pick a default candle resolution from the total span so the full-history view
    # is evenly spaced and clean (Lightweight Charts packs points by index, so a fine
    # resolution over a long span — esp. mixed-resolution decimated data — looks
    # "chunky"). Coarser timeframe for longer history.
    span_days = (t_hi - t_lo) / 86_400_000
    if span_days <= 2:
        def_tf = 0     # 5m
    elif span_days <= 10:
        def_tf = 1     # 15m
    elif span_days <= 45:
        def_tf = 2     # 1h
    else:
        def_tf = 3     # 4h

    div_id = f"tvchart-{token.lower()}"
    cfg_js = json.dumps({
        "rows": js_rows, "dec": dec,
        "listing": listing_markers, "ann": ann_markers,
        "win": {"from": win_from, "to": win_to},
        "tfs": TIMEFRAMES, "def_tf": def_tf,
    }, separators=(",", ":"))

    toolbar = "".join(
        f'<button class="tv-tf{" active" if i == def_tf else ""}" data-m="{m}">{name}</button>'
        for i, (name, m) in enumerate(TIMEFRAMES)
    )
    return (
        f'<div class="tvchart-wrap" style="height:{height}px">'
        f'<div class="tv-toolbar">{toolbar}'
        f'<button class="tv-reset tv-all active" title="Show full history">all</button>'
        f'<button class="tv-reset tv-launch" title="Zoom to the launch reaction">⤺ launch</button>'
        f'<span class="tv-legend"><i class="dot"></i>listing '
        f'<i class="tri"></i>announcement</span></div>'
        f'<div id="{div_id}" class="tvchart"></div>'
        f'<div class="tv-tip" id="{div_id}-tip"></div>'
        f'</div>'
        f'<script>(function(){{var CFG={cfg_js};{_CHART_JS}'
        # Defer to after layout: the script runs inline during parse, when the flex/grid
        # column width isn't settled yet, so el.clientWidth reads too wide and Lightweight
        # Charts builds the plot+price-scale ~64px wider than the card — pushing the y-axis
        # off the card edge where overflow:hidden clips it (the "no price labels" bug).
        f'\nrequestAnimationFrame(function(){{requestAnimationFrame(function(){{'
        f'mount("{div_id}",CFG);}});}});}})();</script>'
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
    // autoSize lets Lightweight Charts measure the container itself and fit the
    // plot + price axis INSIDE it. Without it we passed el.clientWidth read before
    // the flex/grid layout had settled, so the chart rendered ~64px too wide and the
    // price-scale column spilled past the card's right edge where .card{overflow:hidden}
    // clipped it — which is why the y-axis price labels were invisible.
    autoSize: true,   // size to the container; do NOT also pass width/height (that
                      // pins an initial size that fights autoSize and let the price
                      // scale overflow the card).
    layout: { background:{ type:'solid', color:'#ffffff' }, textColor:'#1d2733',
              fontFamily:'Segoe UI, -apple-system, Roboto, sans-serif', fontSize:12 },
    grid: { vertLines:{ visible:false }, horzLines:{ color:'#eef2f6' } },
    rightPriceScale: { visible:true, borderColor:'#e1e7ee' },
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

  // Announcement bucket-time -> label, rebuilt per timeframe. Announcement markers
  // carry no on-chart text (see chart_html); this lets the crosshair tooltip reveal
  // "Announcement" when the cursor lands on the marker's candle.
  var annMap = {};
  function buildAnnMap(mins){
    annMap = {};
    (cfg.ann || []).forEach(function(m){ annMap[bucket(m.time, mins)] = m.label || 'announcement'; });
  }

  var active = cfg.tfs[cfg.def_tf || 0][1];
  function render(mins){
    active = mins;
    series.setData(agg(cfg.rows, mins));
    series.setMarkers(markers(mins));
    buildAnnMap(mins);
  }
  function toAll(){ chart.timeScale().fitContent(); }
  function toLaunch(){
    if(cfg.win){ chart.timeScale().setVisibleRange({ from:cfg.win.from, to:cfg.win.to }); }
  }
  // Default = full history fitted to the frame ("All", like CoinMarketCap).
  render(active); toAll();
  // Keep the chart sized to its (responsive) container. autoSize handles most of it;
  // this is a belt-and-braces resize for browsers/timing where the first measure is
  // stale. (The y-axis-labels-invisible bug was actually the page's global
  // `table{table-layout:fixed}` leaking into LWC's internal table — fixed in CSS.)
  if(window.ResizeObserver){
    new ResizeObserver(function(){ if(el.clientWidth){ chart.resize(el.clientWidth, el.clientHeight); } }).observe(el);
  }

  // Crosshair tooltip (date + price), floating inside the wrap.
  var wrap = el.parentElement, tip = document.getElementById(id+'-tip');
  chart.subscribeCrosshairMove(function(p){
    if(!p || !p.time || !p.point){ if(tip) tip.style.display='none'; return; }
    var d = p.seriesData.get(series);
    if(!d){ if(tip) tip.style.display='none'; return; }
    var price = (d.value!==undefined)? d.value : d.close;
    var dt = new Date(p.time*1000).toISOString().slice(0,16).replace('T',' ');
    if(tip){
      var ann = annMap[p.time];
      var annLine = ann ? '<br><span style="color:#e67e22">📣 '+ann+'</span>' : '';
      tip.innerHTML = '<b>$'+price.toFixed(DEC)+'</b><br>'+dt+' UTC'+annLine;
      tip.style.display='block';
      var x = p.point.x + 16, y = p.point.y + 12;
      if(x > wrap.clientWidth - 130){ x = p.point.x - 130; }
      tip.style.left = x+'px'; tip.style.top = y+'px';
    }
  });

  // Toolbar: timeframe (candle resolution) switch.
  wrap.querySelectorAll('.tv-tf').forEach(function(b){
    b.addEventListener('click', function(){
      wrap.querySelectorAll('.tv-tf').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      render(parseInt(b.dataset.m,10));
    });
  });
  // Range buttons: all (full history) / launch (reaction window). Highlight which.
  var ab = wrap.querySelector('.tv-all'), lb = wrap.querySelector('.tv-launch');
  function setRange(activeBtn){
    [ab, lb].forEach(function(x){ if(x) x.classList.remove('active'); });
    if(activeBtn) activeBtn.classList.add('active');
  }
  if(ab){ ab.addEventListener('click', function(){ toAll(); setRange(ab); }); }
  if(lb){ lb.addEventListener('click', function(){ toLaunch(); setRange(lb); }); }

  // Sizing is handled by autoSize:true (above), which fits the plot + price axis to
  // the container — no manual ResizeObserver needed (the manual one set width to a
  // stale el.clientWidth and pushed the price scale off the card).
}
"""


def timeseries_html(div_id: str, series: list[dict], height: int = 520,
                    ranges: list[tuple] | None = None, sync: str | None = None,
                    sync_main: int = 0) -> str:
    """Generic Lightweight Charts time-series embed, reused by other reports for
    their price / value-over-time *trading* charts (e.g. the Scam Watchlist price
    chart and OI+funding history). Renders one or more line/area/histogram series,
    optionally on dual (left/right) price scales or an overlay scale, with a
    crosshair tooltip and auto-resize.

    `ranges`: optional [(label, days|None), …] -> a toolbar of range buttons (e.g.
    1M / 3M / All), giving the same kind of zoom controls the listing-reaction chart
    has. (These series are daily, so a 5m/1h resolution switcher doesn't apply — a
    range selector is the equivalent control.) None days = fit all.

    `sync`: charts on the same page passing the same group name get a SHARED
    crosshair + visible range (hover one, the line appears on the other).

    series: list of dicts, each:
      {"data": [[t_seconds, value], ...],   # ascending; gaps allowed (omit points)
       "kind": "area" | "line" | "hist",    # hist = histogram bars
       "color": "#1f4e79",
       "scale": "right" | "left" | "<id>",  # <id> = overlay scale (no axis labels)
       "margins": {"top": 0.72, "bottom": 0},  # optional scale margins (pin bars low)
       "name": "Price",                      # tooltip label
       "fmt":  {"kind": "usd"|"usdCompact"|"percent"|"price"|"num", "dec": 4},
       "fill": True}                         # area only
    """
    cfg_js = json.dumps({"height": height, "series": series, "ranges": ranges or [],
                         "sync": sync, "sync_main": sync_main},
                        separators=(",", ":"))
    toolbar = ""
    if ranges:
        btns = "".join(
            f'<button class="tv-reset tv-range{" active" if d is None else ""}" '
            f'data-days="{"" if d is None else d}">{lbl}</button>'
            for lbl, d in ranges)
        toolbar = f'<div class="tv-toolbar">{btns}</div>'
    return (
        f'<div class="tvchart-wrap" style="height:{height}px">'
        f'{toolbar}'
        f'<div id="{div_id}" class="tvchart"></div>'
        f'<div class="tv-tip" id="{div_id}-tip"></div>'
        f'</div>'
        f'<script>(function(){{var CFG={cfg_js};{_TS_JS}'
        # Defer to after layout (see chart_html) so the price scale fits the card.
        f'\nrequestAnimationFrame(function(){{requestAnimationFrame(function(){{'
        f'mountTS("{div_id}",CFG);}});}});}})();</script>'
    )


# Generic time-series mount (no markers). Shared by reports that just need a robust
# price/value-vs-time chart on the same engine as the listing charts.
_TS_JS = r"""
function mountTS(id, cfg){
  var el = document.getElementById(id);
  if(!el || !window.LightweightCharts){ return; }
  var LC = window.LightweightCharts;
  function usdC(v){ var a=Math.abs(v);
    if(a>=1e9) return '$'+(v/1e9).toFixed(2)+'B';
    if(a>=1e6) return '$'+(v/1e6).toFixed(1)+'M';
    if(a>=1e3) return '$'+(v/1e3).toFixed(1)+'K';
    return '$'+v.toFixed(0); }
  function fmtVal(f, v){
    if(v===null || v===undefined) return '';
    f = f || {kind:'price', dec:2};
    if(f.kind==='usdCompact') return usdC(v);
    if(f.kind==='usd') return '$'+Math.round(v).toLocaleString();
    if(f.kind==='percent') return v.toFixed(f.dec)+'%';
    if(f.kind==='num') return v.toFixed(f.dec!=null?f.dec:2);
    return '$'+v.toFixed(f.dec);
  }
  function minMove(f){ f=f||{}; if(f.kind==='usd'||f.kind==='usdCompact') return 1;
    return 1/Math.pow(10, (f.dec!=null?f.dec:2)); }

  var usesLeft = cfg.series.some(function(s){ return s.scale==='left'; });
  var usesRight = cfg.series.some(function(s){ return s.scale!=='left'; });
  // When a single price scale is in use (e.g. the price chart), drive the axis tick
  // labels with that series' formatter via localization.priceFormatter — without it
  // the y-axis renders blank (grid lines but no $ labels), same as the listing chart.
  var single = !(usesLeft && usesRight);
  var primaryFmt = (cfg.series.filter(function(s){ return s.scale!=='left'; })[0]
                    || cfg.series[0] || {}).fmt;
  var chart = LC.createChart(el, {
    // autoSize: fit plot + price axis inside the container (see _CHART_JS note —
    // without it the price scale spilled past the card and got clipped, hiding the
    // y-axis labels).
    autoSize: true,   // size to container; no explicit width/height (see _CHART_JS).
    layout: { background:{ type:'solid', color:'#ffffff' }, textColor:'#1d2733',
              fontFamily:'Segoe UI, -apple-system, Roboto, sans-serif', fontSize:12 },
    grid: { vertLines:{ visible:false }, horzLines:{ color:'#eef2f6' } },
    localization: single ? { priceFormatter: function(p){ return fmtVal(primaryFmt, p); } } : {},
    rightPriceScale: { visible: usesRight, borderColor:'#e1e7ee' },
    leftPriceScale:  { visible: usesLeft,  borderColor:'#e1e7ee' },
    timeScale: { borderColor:'#e1e7ee', timeVisible:true, secondsVisible:false },
    crosshair: { mode: LC.CrosshairMode.Normal,
                 vertLine:{ color:'#c5ccd3', width:1, style:0, labelBackgroundColor:'#1f4e79' },
                 horzLine:{ color:'#c5ccd3', width:1, style:0, labelBackgroundColor:'#1f4e79' } },
    handleScale: true, handleScroll: true,
  });

  function clean(data){
    var map = {};
    for(var i=0;i<data.length;i++){ if(data[i][1]!==null && data[i][1]!==undefined) map[data[i][0]] = data[i][1]; }
    return Object.keys(map).map(Number).sort(function(a,b){return a-b;})
      .map(function(t){ return { time:t, value:map[t] }; });
  }

  var built = [];
  cfg.series.forEach(function(s){
    // 'left'/'right' = the visible axes; any other id = an OVERLAY scale
    // (no axis labels — used to pin volume bars under the main series).
    var scaleId = s.scale || 'right';
    // 'price' kind uses the built-in price format (type:'price'), which generates the
    // y-axis tick labels — a 'custom' format leaves the axis blank. Other kinds keep
    // a custom formatter (they pair with localization.priceFormatter for the axis).
    var f = s.fmt || {};
    var pf = (f.kind === 'price')
      ? { type:'price', precision:(f.dec!=null?f.dec:2), minMove:minMove(f) }
      : { type:'custom', minMove:minMove(f), formatter:function(p){ return fmtVal(f, p); } };
    var ser;
    if(s.kind==='area'){
      ser = chart.addAreaSeries({ lineColor:s.color, lineWidth:2,
        topColor:'rgba(31,78,121,0.18)', bottomColor:'rgba(31,78,121,0.02)',
        priceScaleId:scaleId, priceFormat:pf, priceLineVisible:false });
    } else if(s.kind==='hist'){
      ser = chart.addHistogramSeries({ color:s.color,
        priceScaleId:scaleId, priceFormat:pf, priceLineVisible:false,
        lastValueVisible:false });
    } else {
      ser = chart.addLineSeries({ color:s.color, lineWidth:2,
        priceScaleId:scaleId, priceFormat:pf, priceLineVisible:false });
    }
    if(s.margins){ ser.priceScale().applyOptions({ scaleMargins:s.margins }); }
    ser.setData(clean(s.data));
    built.push({ s:ser, def:s });
  });
  chart.timeScale().fitContent();

  // Cross-chart sync (cfg.sync = group name): hovering one chart shows the
  // crosshair at the same time on its siblings, and pan/zoom follows. The
  // group guard stops the echo loop.
  if(cfg.sync){
    var REG = window.__tssync = window.__tssync || {};
    var G = REG[cfg.sync] = REG[cfg.sync] || { charts:[], busy:false, rbusy:false };
    var mi = cfg.sync_main || 0;        // which series anchors the shared crosshair
    var map = {};
    (cfg.series[mi] && cfg.series[mi].data || []).forEach(function(p){
      if(p[1]!==null && p[1]!==undefined) map[p[0]] = p[1];
    });
    var me = { chart:chart, s0:(built[mi]&&built[mi].s), map:map };
    G.charts.push(me);
    chart.subscribeCrosshairMove(function(p){
      if(G.busy) return;
      G.busy = true;
      G.charts.forEach(function(o){
        if(o===me) return;
        if(p && p.time && o.map[p.time]!==undefined && o.s0){
          o.chart.setCrosshairPosition(o.map[p.time], p.time, o.s0);
        } else {
          o.chart.clearCrosshairPosition();
        }
      });
      G.busy = false;
    });
    chart.timeScale().subscribeVisibleLogicalRangeChange(function(r){
      if(G.rbusy || !r) return;
      G.rbusy = true;
      G.charts.forEach(function(o){
        if(o!==me){ o.chart.timeScale().setVisibleLogicalRange(r); }
      });
      G.rbusy = false;
    });
  }

  var wrap = el.parentElement;
  // Range buttons (e.g. 1M / 3M / All) — the zoom controls equivalent to the
  // listing chart's timeframe switcher (these series are daily, so resolution
  // switching doesn't apply). Default = All (fitContent above).
  function lastT(){ var m=0; built.forEach(function(b){ var d=b.def.data; if(d&&d.length){ var t=d[d.length-1][0]; if(t>m)m=t; } }); return m; }
  (wrap.querySelectorAll('.tv-range')||[]).forEach(function(b){
    b.addEventListener('click', function(){
      wrap.querySelectorAll('.tv-range').forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active');
      var days = b.dataset.days;
      if(!days){ chart.timeScale().fitContent(); return; }
      var to = lastT(); if(!to) return;
      chart.timeScale().setVisibleRange({ from: to - parseInt(days,10)*86400, to: to });
    });
  });

  // Crosshair tooltip: date + each series' formatted value.
  var tip = document.getElementById(id+'-tip');
  chart.subscribeCrosshairMove(function(p){
    if(!p || !p.time || !p.point){ if(tip) tip.style.display='none'; return; }
    var lines = [], any=false;
    built.forEach(function(b){
      var d = p.seriesData.get(b.s);
      if(d && d.value!==undefined){ any=true;
        lines.push('<span style="color:'+b.def.color+'">●</span> '+
                   (b.def.name?b.def.name+': ':'')+'<b>'+fmtVal(b.def.fmt, d.value)+'</b>'); }
    });
    if(!any){ if(tip) tip.style.display='none'; return; }
    var dt = new Date(p.time*1000).toISOString().slice(0,16).replace('T',' ');
    if(tip){
      tip.innerHTML = lines.join('<br>')+'<br><span style="color:#8a95a1">'+dt+' UTC</span>';
      tip.style.display='block';
      var x = p.point.x + 16, y = p.point.y + 12;
      if(x > wrap.clientWidth - 150){ x = p.point.x - 150; }
      tip.style.left = x+'px'; tip.style.top = y+'px';
    }
  });

  // Sizing handled by autoSize:true (above).
}
"""

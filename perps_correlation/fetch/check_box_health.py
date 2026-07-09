"""Box-down watchdog — the thing that watches the box, run from CI (independent of
the box itself, since a dead box can't alert about itself).

Each CI run it measures how stale the screener box's data is (screener.json's
`generated_utc` = the box's OWN self-reported run time, so it tracks the BOX, not CI
timing). If the box has gone silent past BOX_STALE_HOURS it pings Telegram ONCE, and
pings again ONCE when the box recovers. Transition state lives in cache/box_health.json
(committed back by the workflow) so it never repeats an alert.

Keyless-safe: no-ops silently without TELEGRAM_* in the env (so it can never break the
otherwise-keyless build). The token is supplied ONLY via GitHub Actions secrets — never
committed. This is the single, intentional CI secret; everything else stays keyless.

  python fetch/check_box_health.py          # CI runs this; reads TELEGRAM_* from env
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fetch import notify_telegram as tg

HERE = Path(__file__).resolve().parents[1]            # perps_correlation/
CACHE = HERE.parent / "cache"
SCREENER = CACHE / "screener" / "screener.json"
STATE = CACHE / "box_health.json"
# Box pushes hourly; 3h tolerates one missed run so a single hiccup doesn't flap a
# DOWN/RECOVERED pair. Tunable via env.
STALE_H = float(os.environ.get("BOX_STALE_HOURS", "3"))

# ── screener_daemon.py (real-time websocket alerter) heartbeat ──────────────
# Independent of the box-down check above: the box itself can be alive (still
# committing hourly) while the daemon process crashed. heartbeat.json is only
# committed as part of screener_cron.sh's HOURLY push (the daemon has no push
# access of its own), so this can only be as fresh as that cadence — NOT a
# true near-real-time check. Default tolerates one missed hourly commit + the
# usual CI poll lag without false-alarming. See fetch/screener_daemon.py.
DAEMON_HEARTBEAT = CACHE / "screener" / "daemon" / "heartbeat.json"
DAEMON_STALE_H = float(os.environ.get("DAEMON_STALE_HOURS", "2.5"))

# ── two more organs (2026-07-09 audit F-22 — watchdog blind spots) ───────────
# The nightly signal loop writes loop_heartbeat.json on the box; screener_cron's
# hourly push commits it, so CI can tell a dead tuning loop from a healthy one.
LOOP_HEARTBEAT = CACHE / "screener" / "loop_heartbeat.json"
LOOP_STALE_H = float(os.environ.get("LOOP_STALE_HOURS", "30"))
# The ~60s live-cells chain (box minute-cron → Caddy → ZeroSSL cert). Checked
# directly over HTTPS; arms only after it has been seen healthy once.
LIVE_URL = os.environ.get("LIVE_URL", "https://45-32-102-44.sslip.io/live.json")
LIVE_STALE_MIN = float(os.environ.get("LIVE_STALE_MIN", "20"))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _box_run_time() -> dt.datetime | None:
    """The box's last self-reported run time from screener.json (UTC), or None."""
    try:
        g = json.loads(SCREENER.read_text(encoding="utf-8")).get("generated_utc")
        if not g:
            return None
        return dt.datetime.strptime(g, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "ok"}


def _daemon_heartbeat_age_h() -> float | None:
    try:
        hb = json.loads(DAEMON_HEARTBEAT.read_text(encoding="utf-8"))
        ts = hb.get("ts")
        if not ts:
            return None
        return (_now() - dt.datetime.fromtimestamp(ts, dt.timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def _check_daemon(state: dict) -> dict:
    """Own DOWN/RECOVERED pair for screener_daemon.py, tracked under `daemon_*`
    keys in the same box_health.json (distinct from the box-down `state` key
    above). No-ops (leaves state untouched) until heartbeat.json first exists —
    e.g. before the daemon is deployed — so it never false-alarms on a fleet
    that hasn't set it up yet."""
    age_h = _daemon_heartbeat_age_h()
    if age_h is None:
        print("daemon-health: no heartbeat.json yet; skipping (not deployed, or stale read)")
        return state
    prev = state.get("daemon_state", "ok")
    now_state = "down" if age_h > DAEMON_STALE_H else "ok"
    print(f"daemon-health: heartbeat age {age_h:.1f}h (threshold {DAEMON_STALE_H}h) "
          f"-> {now_state} (was {prev})")
    sent: bool | None = None
    if now_state == "down" and prev == "ok":
        sent = tg.send(
            f"\U0001F7E0 <b>REAL-TIME DAEMON DOWN</b> — screener_daemon.py's heartbeat "
            f"is {age_h:.1f}h stale. The hourly cron still covers v1/v4/v2/probe fires "
            f"(just up to ~an hour later) — this only affects the FASTER live alerting.\n"
            f"Check: <code>ssh box</code> · <code>systemctl status screener-daemon</code> · "
            f"<code>journalctl -u screener-daemon -n 50</code>")
    elif now_state == "ok" and prev == "down":
        sent = tg.send("✅ <b>REAL-TIME DAEMON RECOVERED</b> — live alerting resumed.")
    if now_state != prev and sent is False:
        # transition happened but the ping didn't go out — hold state, retry next run
        print("daemon-health: transition but Telegram unconfigured/failed — state held")
        return state
    state["daemon_state"] = now_state
    state["daemon_age_h_at_check"] = round(age_h, 2)
    if now_state != prev:
        state["daemon_since"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return state


def _check_loop(state: dict) -> dict:
    """DOWN/RECOVERED pair for the nightly signal loop, from the committed
    loop_heartbeat.json. No-ops until the heartbeat first exists (fleet that
    hasn't deployed it yet never false-alarms)."""
    try:
        ts = json.loads(LOOP_HEARTBEAT.read_text(encoding="utf-8")).get("ts")
        age_h = (_now() - dt.datetime.fromtimestamp(ts, dt.timezone.utc)).total_seconds() / 3600
    except Exception:
        print("loop-health: no loop_heartbeat.json yet; skipping")
        return state
    prev = state.get("loop_state", "ok")
    now_state = "down" if age_h > LOOP_STALE_H else "ok"
    print(f"loop-health: heartbeat age {age_h:.1f}h (threshold {LOOP_STALE_H}h) "
          f"-> {now_state} (was {prev})")
    sent: bool | None = None
    if now_state == "down" and prev == "ok":
        sent = tg.send(
            f"\U0001F7E0 <b>NIGHTLY LOOP SILENT</b> — signal_loop's heartbeat is "
            f"{age_h:.0f}h stale. Tuning/grading paused; live alerts still run.\n"
            f"Check: <code>ssh box</code> · <code>tail /root/signal_loop.log</code>")
    elif now_state == "ok" and prev == "down":
        sent = tg.send("✅ <b>NIGHTLY LOOP RECOVERED</b> — tuning/grading resumed.")
    if now_state != prev and sent is False:
        print("loop-health: transition but Telegram unconfigured/failed — state held")
        return state
    state["loop_state"] = now_state
    return state


def _check_live(state: dict) -> dict:
    """DOWN/RECOVERED pair for the ~60s live.json chain. Arms only after the
    first successful healthy read (state['live_seen'])."""
    age_min = None
    try:
        import urllib.request
        req = urllib.request.Request(LIVE_URL, headers={"User-Agent": "watchdog"})
        with urllib.request.urlopen(req, timeout=10) as r:
            g = json.load(r).get("generated_utc")
        t = dt.datetime.strptime(g, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age_min = (_now() - t).total_seconds() / 60
    except Exception as e:
        if not state.get("live_seen"):
            print(f"live-health: unreachable and never seen healthy — skipping ({type(e).__name__})")
            return state
    healthy = age_min is not None and age_min <= LIVE_STALE_MIN
    if healthy:
        state["live_seen"] = True
    prev = state.get("live_state", "ok")
    now_state = "ok" if healthy else "down"
    shown = "n/a" if age_min is None else f"{age_min:.1f}"
    print(f"live-health: age {shown}min (threshold {LIVE_STALE_MIN}min) "
          f"-> {now_state} (was {prev})")
    sent: bool | None = None
    if now_state == "down" and prev == "ok":
        sent = tg.send(
            f"\U0001F7E0 <b>LIVE CELLS DOWN</b> — live.json is stale/unreachable; the site "
            f"shows build-time numbers (~20min cadence) until it's back.\n"
            f"Check: <code>ssh box</code> · <code>tail /root/live_cron.log</code> · "
            f"<code>systemctl status caddy</code>")
    elif now_state == "ok" and prev == "down":
        sent = tg.send("✅ <b>LIVE CELLS RECOVERED</b> — ~60s updates resumed.")
    if now_state != prev and sent is False:
        print("live-health: transition but Telegram unconfigured/failed — state held")
        return state
    state["live_state"] = now_state
    return state


def main() -> int:
    t = _box_run_time()
    if t is None:
        print("box-health: no screener.json generated_utc; skipping (no false alarm)")
        return 0
    age_h = (_now() - t).total_seconds() / 3600
    st = _load_state()
    prev = st.get("state", "ok")
    now_state = "down" if age_h > STALE_H else "ok"
    print(f"box-health: last box run {t:%Y-%m-%d %H:%M} UTC · age {age_h:.1f}h "
          f"(threshold {STALE_H}h) -> {now_state} (was {prev})")

    sent: bool | None = None
    if now_state == "down" and prev == "ok":
        sent = tg.send(
            f"\U0001F534 <b>BOX DOWN</b> — the screener box hasn't updated in "
            f"{age_h:.1f}h (last run {t:%Y-%m-%d %H:%M} UTC).\n"
            f"Live signals, alerts and the Manipulated tab are frozen until it's back.\n"
            f"Check: <code>ssh box</code> · <code>uptime</code> · "
            f"<code>tail ~/screener_cron.log</code>")
    elif now_state == "ok" and prev == "down":
        sent = tg.send(
            f"✅ <b>BOX RECOVERED</b> — the screener box is updating again "
            f"(last run {t:%H:%M} UTC). Live signals + alerts resumed.")

    new = {
        "state": now_state,
        "age_h_at_check": round(age_h, 2),
        "last_box_run_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checked_utc": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since": st.get("since"),
    }
    # Carry the sub-checks' transition state across runs — without this each
    # run started from "ok" and a sustained outage would re-alert every ~20min
    # (latent repeat-alert bug found while wiring the new checks, 2026-07-09).
    for k in ("daemon_state", "daemon_since", "loop_state", "live_state", "live_seen"):
        if k in st:
            new[k] = st[k]
    if now_state != prev:
        if sent is False:
            # A transition happened but the ping didn't go out (token unset/failed).
            # Don't advance the state, so the next run retries the alert.
            print("box-health: transition but Telegram unconfigured/failed — "
                  "state held so the next run retries")
            new["state"] = prev
        else:
            new["since"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"box-health: ALERT sent ({prev} -> {now_state})")

    new = _check_daemon(new)
    new = _check_loop(new)
    new = _check_live(new)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(new, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(new, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

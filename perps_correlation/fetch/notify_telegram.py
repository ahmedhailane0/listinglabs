"""Telegram sender for the perp-screener alerts.

Reads the bot token + chat id from the ENVIRONMENT — never hard-coded — so this
file is safe in the public repo. No-ops silently when unconfigured (e.g. CI, or
the box before the token is set), so a missing token can never break the hourly
fetch or the nightly grader.

  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     the chat to send to

On the box the cron exports these from a box-local env file
(/root/.config/listinglabs/telegram.env) that is NOT in the repo.
"""
from __future__ import annotations

import json
import os
import urllib.request

_API = "https://api.telegram.org/bot{token}/sendMessage"


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str, parse_mode: str = "HTML") -> bool:
    """POST one message. Returns True on success, False if unconfigured/failed.
    Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    payload = json.dumps({
        "chat_id": chat, "text": text, "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        _API.format(token=token), data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"  telegram: send failed ({e})")
        return False

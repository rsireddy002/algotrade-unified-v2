"""
execution/telegram_alert.py — Best-effort Telegram bot notifications.

Configure two values via the same os.environ / Streamlit Secrets bridge
already used for UPSTOX_ACCESS_TOKEN (see dashboard/app.py's
st.secrets -> os.environ block near the top):
  - TELEGRAM_BOT_TOKEN : from @BotFather after creating a bot
  - TELEGRAM_CHAT_ID   : the chat (personal or group) the bot should post to

Both are optional at the code level on purpose: if either is missing,
send_telegram_alert() just returns False rather than raising, so a
scanner running without Telegram configured yet keeps working normally
with only the in-app visual alerts.
"""

import os

import requests

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_telegram_alert(text, timeout=10):
    """Send `text` to the configured Telegram chat. Returns True if the
    message was accepted by Telegram, False if not configured or if the
    send failed for any reason (network error, bad token, etc.) — a
    Telegram outage should never crash or block the scanner."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )
        return resp.ok
    except Exception:
        return False

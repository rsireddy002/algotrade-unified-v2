"""
core/telegram.py — Shared Telegram alert sender.

Any module (breakout scanner, morning fade, LLM gatekeeper, etc.) can call
send_telegram_message() directly. Kept deliberately dumb — formatting each
signal type into a message is that module's job, not this one's.
"""

import os

import requests


def send_telegram_message(text):
    """Send a Telegram alert via the bot API, if TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID are configured. No-ops (returns False) if either is
    missing. Raises on a genuine send failure so the caller can decide how
    to surface it."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    resp.raise_for_status()
    return True

"""
core/auth.py — Upstox authentication + a retry-safe GET helper.

Every other module in this platform should call upstox_get() rather than
hitting requests.get() directly, so the 429/Cloudflare retry behavior (and
any future auth changes) live in exactly one place.
"""

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()


class UpstoxError(Exception):
    """Raised for any Upstox API failure (auth, rate limit, bad response)."""


def get_token():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise UpstoxError(
            "UPSTOX_ACCESS_TOKEN is not set — use an Analytics Token "
            "(account.upstox.com > Developer Apps > Analytics tab), 1-year "
            "validity, no daily refresh needed."
        )
    return token


def upstox_get(url, params=None, max_retries=4, timeout=15):
    """GET against the Upstox API with auth applied and automatic retry on
    HTTP 429 (Upstox sits behind Cloudflare, which rate-limits bursty
    request patterns from a single IP more aggressively than the origin API
    itself does — confirmed in production on a fresh EC2 IP's first scan)."""
    token = get_token()
    for attempt in range(max_retries + 1):
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        if resp.status_code == 401:
            raise UpstoxError(
                "Upstox access token expired or invalid — refresh UPSTOX_ACCESS_TOKEN in .env"
            )
        if resp.status_code == 429:
            if attempt == max_retries:
                raise UpstoxError(f"Upstox API error {resp.status_code}: {resp.text[:200]}")
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else (2 ** attempt)
            time.sleep(min(wait, 30))
            continue
        if not resp.ok:
            raise UpstoxError(f"Upstox API error {resp.status_code}: {resp.text[:200]}")

        payload = resp.json()
        if payload.get("status") != "success":
            raise UpstoxError(f"Upstox API returned an error: {str(payload)[:200]}")
        return payload

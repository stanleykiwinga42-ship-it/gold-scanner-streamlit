"""ntfy phone alerts."""

from __future__ import annotations

import re

import requests

import scan_config as config


def _ascii(value: str) -> str:
    """HTTP headers must be plain ASCII (an em dash would break ntfy)."""
    value = re.sub("[\u2012-\u2015]", "-", value or "")
    return re.sub(r"[^\x20-\x7E]", "", value).strip()


def send(title: str, message: str, direction: str | None = None,
         result: str | None = None) -> dict:
    """Returns {'ok': bool, 'message': str}. Never raises."""
    topic = config.get("NTFY_TOPIC")
    server = (config.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    if not topic:
        return {"ok": False, "message": "NTFY_TOPIC is not configured"}

    tags = "chart_with_upwards_trend"
    if direction and direction.upper() == "SELL":
        tags = "chart_with_downwards_trend"
    if result == "TP":
        tags = "white_check_mark"
    if result == "SL":
        tags = "warning"

    try:
        response = requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": _ascii(title or "Liquidity Scanner"),
                "Priority": "high",
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=15)
        if response.status_code >= 300:
            return {"ok": False,
                    "message": f"ntfy rejected it ({response.status_code}): "
                               f"{response.text[:160]}"}
        return {"ok": True, "message": "sent"}
    except requests.RequestException as error:
        return {"ok": False, "message": str(error)}

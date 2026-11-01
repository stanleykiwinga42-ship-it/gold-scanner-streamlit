"""Twelve Data candles and live price for XAU/USD."""

from __future__ import annotations

import requests

import scan_config as config

BASE = "https://api.twelvedata.com"
SYMBOL = "XAU/USD"


def _key() -> str:
    key = config.get("TWELVE_DATA_API_KEY")
    if not key:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured")
    return key


def fetch_candles(outputsize: int = 300) -> list[dict]:
    """1-minute candles, oldest first. Times are UTC 'YYYY-MM-DD HH:MM:SS'."""
    response = requests.get(
        f"{BASE}/time_series",
        params={"symbol": SYMBOL, "interval": "1min",
                "outputsize": outputsize, "timezone": "UTC",
                "apikey": _key()},
        timeout=20)
    data = response.json()
    if "values" not in data:
        raise RuntimeError(f"Unable to obtain candle data: {data.get('message', data)}")
    return [{
        "time": row["datetime"],
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
    } for row in reversed(data["values"])]


def fetch_price() -> float:
    response = requests.get(
        f"{BASE}/price", params={"symbol": SYMBOL, "apikey": _key()}, timeout=15)
    data = response.json()
    if "price" not in data:
        raise RuntimeError(f"Unable to obtain XAUUSD price: {data.get('message', data)}")
    return float(data["price"])

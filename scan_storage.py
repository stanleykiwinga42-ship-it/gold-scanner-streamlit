"""
Small key/value store shared by the Streamlit app and the background monitor.

Keys
  appState     zones, prices, settings, scanner on/off     (written by the app)
  engineState  setups, open trade, last candle             (written by the monitor)
  journal      list of trades                              (both)

With DATABASE_URL (any Postgres, e.g. Neon / Supabase) data is permanent and
shared. Without it a local JSON file is used (NOT shared, NOT permanent on
Streamlit Cloud) - fine only for testing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import scan_config as config

_FILE_DIR = Path(os.environ.get("SCANNER_DATA_DIR", "data"))


def using_database() -> bool:
    return bool(config.get("DATABASE_URL"))


def _connect():
    import psycopg2
    return psycopg2.connect(config.get("DATABASE_URL"), connect_timeout=10)


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS kv_store (
                   key TEXT PRIMARY KEY,
                   value JSONB NOT NULL,
                   updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.commit()


def read(key: str, default: Any = None) -> Any:
    if using_database():
        conn = _connect()
        try:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                row = cur.fetchone()
            return default if row is None else row[0]
        finally:
            conn.close()
    path = _FILE_DIR / f"{key}.json"
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write(key: str, value: Any) -> None:
    if using_database():
        conn = _connect()
        try:
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO kv_store (key, value, updated_at)
                       VALUES (%s, %s::jsonb, NOW())
                       ON CONFLICT (key) DO UPDATE
                       SET value = EXCLUDED.value, updated_at = NOW()""",
                    (key, json.dumps(value)))
            conn.commit()
        finally:
            conn.close()
        return
    _FILE_DIR.mkdir(parents=True, exist_ok=True)
    (_FILE_DIR / f"{key}.json").write_text(json.dumps(value, indent=2))


# ---------------------------------------------------------------- app state
DEFAULT_APP_STATE = {
    "scanner_running": False,
    "start_id": None,
    "started_at": None,
    "buy_zones": [],
    "sell_zones": [],
    "futures_price": None,
    "tradingview_price": None,
    "difference": None,
    "difference_calculated": False,
    "locked_difference": None,
    "settings": {},
}


def load_app_state() -> dict:
    saved = read("appState", None) or {}
    return {**DEFAULT_APP_STATE, **saved}


def save_app_state(state: dict) -> None:
    write("appState", state)


def load_engine_state() -> dict:
    return read("engineState", None) or {}


def save_engine_state(state: dict) -> None:
    write("engineState", state)


# ------------------------------------------------------------------ journal
def load_journal() -> list[dict]:
    data = read("journal", [])
    return data if isinstance(data, list) else []


def journal_add(trade: dict) -> str:
    from scan_engine import new_id
    journal = load_journal()
    entry = {"id": new_id(), "symbol": "XAUUSD", **trade}
    journal.insert(0, entry)
    write("journal", journal)
    return entry["id"]


def journal_update(trade_id: str, fields: dict) -> None:
    if not trade_id:
        return
    journal = load_journal()
    for entry in journal:
        if entry.get("id") == trade_id:
            entry.update(fields)
            break
    write("journal", journal)


def journal_clear() -> None:
    write("journal", [])

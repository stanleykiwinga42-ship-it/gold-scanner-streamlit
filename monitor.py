"""
Background monitor - runs the scanner with the app closed.

Started by GitHub Actions (.github/workflows/monitor.yml). It loops for
MONITOR_MINUTES, reading the zones / settings the Streamlit app saved in the
database, and sends ntfy alerts. Run locally with:  python monitor.py
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import scan_market as market
import scan_notify as notify
import scan_storage as storage
from scan_engine import Services, now_iso
from scan_runner import run_tick

POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "10"))
MONITOR_MINUTES = float(os.environ.get("MONITOR_MINUTES", "340"))
PAUSE_WEEKEND = os.environ.get("WEEKEND_PAUSE", "on").lower() != "off"


def market_paused(now: datetime | None = None) -> bool:
    """Gold is closed from Friday ~22:00 UTC to Sunday ~22:00 UTC."""
    if not PAUSE_WEEKEND:
        return False
    now = now or datetime.now(timezone.utc)
    day, hour = now.weekday(), now.hour          # Mon=0 ... Sun=6
    if day == 5:
        return True
    if day == 4 and hour >= 22:
        return True
    if day == 6 and hour < 22:
        return True
    return False


def alert(title, message, direction=None, result=None):
    outcome = notify.send(title, message, direction=direction, result=result)
    if not outcome["ok"]:
        print("ALERT FAILED:", outcome["message"], flush=True)
    return outcome


SERVICES = Services(
    notify=alert,
    journal_add=storage.journal_add,
    journal_update=storage.journal_update,
)


def beat(**fields) -> None:
    storage.write("monitorStatus", {"last_beat": now_iso(), **fields})


def step() -> float:
    """Runs one pass. Returns how many seconds to wait before the next."""
    app = storage.load_app_state()

    if not app.get("scanner_running"):
        beat(running=False, note="scanner stopped")
        return 30

    if market_paused():
        beat(running=True, paused=True, note="market closed (weekend)")
        return 300

    candles = market.fetch_candles(300)
    saved = storage.load_engine_state()
    state, lines = run_tick(app, saved, candles, SERVICES)
    storage.save_engine_state(state)
    beat(running=True, paused=False, last_tick=now_iso(), last_error=None)
    for line in lines:
        print(now_iso(), line, flush=True)
    return POLL_SECONDS


def main() -> None:
    deadline = time.time() + MONITOR_MINUTES * 60
    print(f"monitor started, running for {MONITOR_MINUTES:g} min "
          f"(poll every {POLL_SECONDS:g}s)", flush=True)
    while time.time() < deadline:
        try:
            wait = step()
        except Exception as error:                      # keep going
            print("tick failed:", error, flush=True)
            try:
                beat(running=True, last_error=str(error))
            except Exception:
                pass
            wait = max(POLL_SECONDS, 15)
        time.sleep(wait)
    print("monitor finished its window - the next scheduled run continues",
          flush=True)


if __name__ == "__main__":
    main()

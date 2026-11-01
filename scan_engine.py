"""
A-B-C-D zone scanner engine (pure Python, no network, no UI).

SELL  (zone = resistance)
  A = price breaks ABOVE the zone (a group of candles; A keeps extending
      while new highs are made)
  B = pullback DOWN to the zone bottom or lower (group of candles)
  C = push back UP to the zone top or higher (group of candles)
  D = the C -> D move breaks the zone LOW  ->  ENTRY at the zone low
  SL = zone top.   TP = lower edge of the next BUY zone below.

BUY is the exact mirror (break below, bounce, push down, D breaks the
zone HIGH -> entry at the zone high, SL = zone bottom,
TP = upper edge of the next SELL zone above).

Rules kept from the original app
  * a candle that extends A / B / C never completes the next step on the
    same candle; D must come from a later candle than C
  * minimum risk/reward, one trade at a time (trade lock)
  * every zone is monitored from 20 minutes BEFORE it was entered until
    it is deleted
  * the newest (still forming) candle is re-read on every tick, so a break
    is detected the moment it happens
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

LOOKBACK_MINUTES = 20

DEFAULT_SETTINGS = {
    "enable_buy": True,
    "enable_sell": True,
    "trade_lock": True,
    "minimum_rr": 2.0,
}

STAGE_TEXT = {
    "CB_A": "A SPIKE - WAITING FOR B PULLBACK",
    "CB_B": "B COMPLETE - WAITING FOR C PUSH",
    "CB_C": "C PUSH - WAITING FOR D (ZONE BREAK)",
}


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def to_ms(value: Any) -> Optional[float]:
    """Candle time -> milliseconds since epoch (UTC)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        if len(text) == 19 and text[10] == " ":
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).timestamp() * 1000
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except ValueError:
        return None


def now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def make_zone(kind: str, futures_upper: float, futures_lower: float,
              difference: float) -> dict:
    """kind = 'BUY' | 'SELL'.  TradingView price = futures price - difference."""
    upper = float(futures_upper)
    lower = float(futures_lower)
    if upper < lower:
        upper, lower = lower, upper
    return {
        "id": new_id(),
        "type": kind,
        "futures_upper": upper,
        "futures_lower": lower,
        "converted_upper": upper - difference,
        "converted_lower": lower - difference,
        "created_at": now_iso(),
        "monitor_from_ms": now_ms() - LOOKBACK_MINUTES * 60000,
    }


def reconvert_zones(zones: list[dict], difference: float) -> None:
    for zone in zones:
        zone["converted_upper"] = zone["futures_upper"] - difference
        zone["converted_lower"] = zone["futures_lower"] - difference


def risk_reward(direction: str, entry: float, sl: float,
                tp: float) -> Optional[float]:
    risk = entry - sl if direction == "BUY" else sl - entry
    reward = tp - entry if direction == "BUY" else entry - tp
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


# ----------------------------------------------------------------------
# side effects (alerts + journal) are injected so the engine stays testable
# ----------------------------------------------------------------------

class Services:
    """Override / replace these callables."""

    def __init__(self,
                 notify: Optional[Callable[..., Any]] = None,
                 journal_add: Optional[Callable[[dict], Any]] = None,
                 journal_update: Optional[Callable[[Any, dict], Any]] = None):
        self.notify = notify or (lambda *a, **k: None)
        self.journal_add = journal_add or (lambda trade: None)
        self.journal_update = journal_update or (lambda jid, fields: None)


class Engine:
    """Holds the scanner state and processes candles."""

    def __init__(self, zones_buy: list[dict], zones_sell: list[dict],
                 settings: Optional[dict] = None,
                 state: Optional[dict] = None,
                 services: Optional[Services] = None):
        self.buy_zones = zones_buy
        self.sell_zones = zones_sell
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        state = state or {}
        self.setups: list[dict] = state.get("setups") or []
        self.open_trade: Optional[dict] = state.get("open_trade")
        self.last_processed_time: Optional[str] = state.get("last_processed_time")
        self.seen_zone_ids: list[str] = state.get("seen_zone_ids") or []
        self.services = services or Services()
        self.current_candle_ms: Optional[float] = None
        self._replay_ids: Optional[list[str]] = None
        self.events: list[str] = []          # human readable log of this run

    # ---- state -------------------------------------------------------
    def export_state(self) -> dict:
        return {
            "setups": self.setups,
            "open_trade": self.open_trade,
            "last_processed_time": self.last_processed_time,
            "seen_zone_ids": self.seen_zone_ids,
        }

    def zone_by_id(self, zone_id: str) -> Optional[dict]:
        for zone in self.buy_zones + self.sell_zones:
            if zone["id"] == zone_id:
                return zone
        return None

    def _log(self, text: str) -> None:
        self.events.append(text)

    # ---- take profit -------------------------------------------------
    def take_profit(self, setup: dict) -> Optional[float]:
        zone = self.zone_by_id(setup["zone_id"])
        if not zone:
            return None
        if setup["direction"] == "BUY":
            above = sorted(
                (z for z in self.sell_zones
                 if z["converted_lower"] > zone["converted_upper"]),
                key=lambda z: z["converted_lower"])
            return above[0]["converted_upper"] if above else None
        below = sorted(
            (z for z in self.buy_zones
             if z["converted_upper"] < zone["converted_lower"]),
            key=lambda z: -z["converted_upper"])
        return below[0]["converted_lower"] if below else None

    # ---- setups ------------------------------------------------------
    def zone_has_setup(self, zone: dict) -> bool:
        return any(s["zone_id"] == zone["id"] for s in self.setups)

    @staticmethod
    def in_window(zone: dict, candle: dict) -> bool:
        start = zone.get("monitor_from_ms")
        if start is None:
            created = to_ms(zone.get("created_at"))
            start = None if created is None else created - LOOKBACK_MINUTES * 60000
        t = to_ms(candle["time"])
        if start is None or t is None:
            return True
        return t + 60000 > start          # a 1-minute candle overlapping the window

    def find_setups(self, direction: str, candle: dict) -> None:
        zones = self.sell_zones if direction == "SELL" else self.buy_zones
        for zone in zones:
            if self.zone_has_setup(zone):
                continue
            if self._replay_ids is not None and zone["id"] not in self._replay_ids:
                continue
            if not self.in_window(zone, candle):
                continue
            spiked = (candle["high"] > zone["converted_upper"]
                      if direction == "SELL"
                      else candle["low"] < zone["converted_lower"])
            if not spiked:
                continue
            sell = direction == "SELL"
            self.setups.append({
                "id": new_id(),
                "direction": direction,
                "zone_id": zone["id"],
                "stage": "CB_A",
                "a_ms": to_ms(candle["time"]),
                "a_price": candle["high"] if sell else candle["low"],
                "b_ms": None, "b_price": None,
                "c_ms": None, "c_price": None,
                "entry_price": zone["converted_lower"] if sell else zone["converted_upper"],
                "sl_price": zone["converted_upper"] if sell else zone["converted_lower"],
            })
            self._log(f"{direction} A SPIKE | zone "
                      f"{zone['converted_lower']:.2f} - {zone['converted_upper']:.2f}")

    def _remove(self, setup: dict) -> None:
        if setup in self.setups:
            self.setups.remove(setup)

    def process_setup(self, setup: dict, candle: dict) -> None:
        zone = self.zone_by_id(setup["zone_id"])
        if not zone:
            return
        t = to_ms(candle["time"])
        if t is None or not t > (setup["a_ms"] or 0):
            return
        top, bottom = zone["converted_upper"], zone["converted_lower"]
        sell = setup["direction"] == "SELL"
        high, low = candle["high"], candle["low"]

        # ---- A: spike outside the zone, waiting for B ----------------
        if setup["stage"] == "CB_A":
            if (high > setup["a_price"]) if sell else (low < setup["a_price"]):
                setup["a_price"] = high if sell else low
                setup["a_ms"] = t
                return
            if (low <= bottom) if sell else (high >= top):
                setup["stage"] = "CB_B"
                setup["b_ms"] = t
                setup["b_price"] = low if sell else high
            return

        # ---- B: pulled back, waiting for C ---------------------------
        if setup["stage"] == "CB_B":
            if (low < setup["b_price"]) if sell else (high > setup["b_price"]):
                setup["b_price"] = low if sell else high
                setup["b_ms"] = t
                return
            if not t > setup["b_ms"]:
                return
            if (high >= top) if sell else (low <= bottom):
                setup["stage"] = "CB_C"
                setup["c_ms"] = t
                setup["c_price"] = high if sell else low
            return

        # ---- C: pushed back, waiting for D ---------------------------
        if setup["stage"] == "CB_C":
            if (high > setup["c_price"]) if sell else (low < setup["c_price"]):
                setup["c_price"] = high if sell else low
                setup["c_ms"] = t
                return
            if not t > setup["c_ms"]:
                return
            if (low <= bottom) if sell else (high >= top):
                self._log(f"{setup['direction']} D BREAK @ {setup['entry_price']:.2f}")
                self.execute_entry(setup)

    # ---- entry / trade -----------------------------------------------
    def execute_entry(self, setup: dict) -> None:
        if self.open_trade:
            return
        zone = self.zone_by_id(setup["zone_id"])
        if not zone:
            return
        direction = setup["direction"]
        entry, sl = setup["entry_price"], setup["sl_price"]
        tp = self.take_profit(setup)

        def blocked(title: str, text: str, rr=None) -> None:
            self._remove(setup)
            self._log(text)
            self.services.notify(title, text, direction=direction)

        if tp is None:
            return blocked("ENTRY BLOCKED", "ENTRY BLOCKED - NO OPPOSING ZONE FOR TP")
        rr = risk_reward(direction, entry, sl, tp)
        if rr is None:
            return blocked("ENTRY BLOCKED", "ENTRY BLOCKED - INVALID RISK/REWARD")
        minimum = float(self.settings.get("minimum_rr") or 2)
        if rr < minimum:
            return blocked("ENTRY BLOCKED - LOW RR",
                           f"ENTRY BLOCKED - RR {rr:.2f}:1 BELOW MINIMUM {minimum:g}:1")

        trade = {
            "direction": direction,
            "zone_id": zone["id"],
            "zone_lower": zone["converted_lower"],
            "zone_upper": zone["converted_upper"],
            "entry": entry, "sl": sl, "tp": tp, "rr": rr,
            "opened_at": now_iso(),
            "entry_candle_ms": self.current_candle_ms,
            "journal_id": None,
        }
        self.open_trade = trade
        self._remove(setup)
        text = (f"{direction} ENTRY @ {entry:.2f} | SL {sl:.2f} | "
                f"TP {tp:.2f} | RR {rr:.2f}:1")
        self._log(text)
        # alert FIRST, journal second
        self.services.notify(f"{direction} ENTRY", text, direction=direction)
        try:
            trade["journal_id"] = self.services.journal_add({
                "direction": direction, "entry": entry, "sl": sl, "tp": tp,
                "rr": rr, "opened_at": trade["opened_at"], "status": "OPEN",
            })
        except Exception as error:                      # never lose the trade
            self._log(f"journal save failed: {error}")

    def monitor_open_trade(self, candle: dict) -> None:
        trade = self.open_trade
        if not trade:
            return
        t = to_ms(candle["time"])
        entry_ms = trade.get("entry_candle_ms")
        # the entry candle is re-read while it forms: skip it
        if entry_ms is not None and t is not None and t <= entry_ms:
            return
        if trade["direction"] == "BUY":
            hit_sl = candle["low"] <= trade["sl"]
            hit_tp = candle["high"] >= trade["tp"]
        else:
            hit_sl = candle["high"] >= trade["sl"]
            hit_tp = candle["low"] <= trade["tp"]
        if hit_sl:                                  # conservative: SL first
            self.close_trade("SL")
        elif hit_tp:
            self.close_trade("TP")

    def close_trade(self, result: str) -> None:
        trade = self.open_trade
        if not trade:
            return
        self.open_trade = None                      # never close twice
        exit_price = trade["tp"] if result == "TP" else trade["sl"]
        pnl_r = trade["rr"] if result == "TP" else -1.0
        text = (f"{trade['direction']} {result} @ {exit_price:.2f} | "
                f"RR {trade['rr']:.2f}:1 | Result {pnl_r:+.2f}R")
        self._log(text)
        self.services.notify(f"{trade['direction']} {result}", text,
                             direction=trade["direction"], result=result)
        try:
            self.services.journal_update(trade.get("journal_id"), {
                "status": result, "exit": exit_price, "result": result,
                "result_r": pnl_r, "closed_at": now_iso(),
            })
        except Exception as error:
            self._log(f"journal update failed: {error}")

    # ---- candle processing -------------------------------------------
    def process_candle(self, candle: dict) -> None:
        self.current_candle_ms = to_ms(candle["time"])

        if self.open_trade:
            self.monitor_open_trade(candle)
            if self.settings.get("trade_lock", True):
                return

        for setup in list(self.setups):
            if self.open_trade and self.settings.get("trade_lock", True):
                break
            if setup in self.setups:
                self.process_setup(setup, candle)
        self.setups = [s for s in self.setups if self.zone_by_id(s["zone_id"])]

        if not self.open_trade:
            if self.settings.get("enable_buy", True):
                self.find_setups("BUY", candle)
            if self.settings.get("enable_sell", True):
                self.find_setups("SELL", candle)

    def process_candles(self, candles: list[dict]) -> None:
        """Oldest -> newest. The newest candle is re-read on every tick."""
        last = to_ms(self.last_processed_time)
        for candle in candles:
            t = to_ms(candle["time"])
            if t is None:
                continue
            if last is not None and t < last:
                continue
            self.last_processed_time = candle["time"]
            self.process_candle(candle)

    # ---- zones added while running ------------------------------------
    def _replay_new_zones(self, candles: list[dict]) -> list[str]:
        seen = set(self.seen_zone_ids)
        fresh = [z for z in self.buy_zones + self.sell_zones if z["id"] not in seen]
        if not fresh:
            return []
        if self.open_trade:                  # one trade at a time: replay later
            return []
        kept = self.setups
        self.setups = []
        self._replay_ids = [z["id"] for z in fresh]
        limit = to_ms(self.last_processed_time)
        try:
            for candle in candles:
                t = to_ms(candle["time"])
                if t is None:
                    continue
                if limit is not None and t > limit:
                    break
                self.process_candle(candle)
        finally:
            self._replay_ids = None
        self.setups = kept + self.setups
        return [z["id"] for z in fresh]

    def tick(self, candles: list[dict]) -> None:
        """One scanner pass over freshly downloaded candles."""
        first_run = self.last_processed_time is None
        handled: list[str] = []
        if not first_run:
            handled = self._replay_new_zones(candles)
        self.process_candles(candles)
        if first_run:
            handled = [z["id"] for z in self.buy_zones + self.sell_zones]
        current = {z["id"] for z in self.buy_zones + self.sell_zones}
        self.seen_zone_ids = [i for i in dict.fromkeys(self.seen_zone_ids + handled)
                              if i in current]

    # ---- display helpers ----------------------------------------------
    def zone_status(self, zone: dict) -> str:
        trade = self.open_trade
        if trade and trade["zone_id"] == zone["id"]:
            return "TRADE ACTIVE"
        if trade and self.settings.get("trade_lock", True):
            return "PAUSED - TRADE ACTIVE"
        setup = next((s for s in self.setups if s["zone_id"] == zone["id"]), None)
        if setup:
            return STAGE_TEXT.get(setup["stage"], setup["stage"])
        if zone["type"] == "SELL":
            return "WAITING FOR A - BREAK ABOVE THE ZONE"
        return "WAITING FOR A - BREAK BELOW THE ZONE"

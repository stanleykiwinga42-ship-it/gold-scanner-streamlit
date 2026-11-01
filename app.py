"""XAUUSD Liquidity Zone Scanner - Streamlit control panel.

The page only edits settings / zones and shows what the background monitor
(monitor.py, started by GitHub Actions) is doing. Closing the page does not
stop the scanner.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import scan_market as market
import scan_notify as notify
import scan_storage as storage
from scan_engine import (DEFAULT_SETTINGS, LOOKBACK_MINUTES, Engine,
                            make_zone, new_id, now_iso, reconvert_zones, to_ms)

st.set_page_config(page_title="XAUUSD Liquidity Scanner",
                   page_icon=":chart_with_upwards_trend:", layout="centered")


# ---------------------------------------------------------------- helpers
def save(app: dict) -> None:
    storage.save_app_state(app)


def current_difference(app: dict):
    """The difference used to convert zones (locked while the scanner runs)."""
    if app.get("scanner_running") and app.get("locked_difference") is not None:
        return app["locked_difference"]
    if app.get("difference_calculated") and app.get("difference") is not None:
        return app["difference"]
    return None


@st.cache_data(ttl=10, show_spinner=False)
def live_price():
    try:
        return market.fetch_price()
    except Exception:
        return None


def money(value) -> str:
    return "--" if value is None else f"{float(value):.2f}"


def age_seconds(iso_text) -> float | None:
    ms = to_ms(iso_text)
    if ms is None:
        return None
    return datetime.now(timezone.utc).timestamp() - ms / 1000


def engine_state_for(app: dict, saved: dict) -> dict:
    """Ignore state left over from an earlier Start."""
    return saved if saved.get("start_id") == app.get("start_id") else {}


# ------------------------------------------------------------- start up
try:
    app = storage.load_app_state()
except Exception as error:
    st.error(f"Cannot reach the database: {error}")
    st.info("Check DATABASE_URL in the app secrets.")
    st.stop()

st.title("XAUUSD - 1 MINUTE ENGINE")

if not storage.using_database():
    st.warning("No DATABASE_URL set: data is kept in a local file and will be "
               "lost when the app restarts, and the background monitor cannot "
               "see it. Add DATABASE_URL in the app secrets.")

tab_scanner, tab_xau, tab_journal, tab_settings = st.tabs(
    ["Scanner", "XAUUSD", "Journal", "Settings"])


# ============================================================ SCANNER TAB
def use_live_price() -> None:
    """Button callback: runs before the page is drawn, so it may set widget values."""
    price = live_price()
    if price is None:
        st.session_state["live_price_failed"] = True
    else:
        st.session_state["tv_input"] = float(price)


def price_conversion(app: dict) -> None:
    st.subheader("1. PRICE CONVERSION")
    st.caption("Enter the current Futures Gold price and the TradingView "
               "XAUUSD price. Zones you enter in futures prices are converted "
               "to TradingView prices (futures price minus the difference).")

    st.session_state.setdefault("futures_input", float(app.get("futures_price") or 0.0))
    st.session_state.setdefault("tv_input", float(app.get("tradingview_price") or 0.0))

    st.number_input("Futures Gold price", min_value=0.0, step=0.01,
                    format="%.2f", key="futures_input")
    st.number_input("TradingView XAUUSD price", min_value=0.0, step=0.01,
                    format="%.2f", key="tv_input")

    left, right = st.columns(2)
    left.button("Use live XAUUSD price", use_container_width=True,
                on_click=use_live_price)
    if st.session_state.pop("live_price_failed", False):
        st.warning("Could not get the live price (check TWELVE_DATA_API_KEY).")

    if right.button("CALCULATE DIFFERENCE", type="primary",
                    use_container_width=True):
        futures = float(st.session_state["futures_input"])
        tv = float(st.session_state["tv_input"])
        if futures <= 0 or tv <= 0:
            st.warning("Enter BOTH prices first.")
        else:
            difference = futures - tv
            app.update({"futures_price": futures, "tradingview_price": tv,
                        "difference": difference, "difference_calculated": True})
            note = ""
            if not app.get("scanner_running"):
                zones = app["buy_zones"] + app["sell_zones"]
                reconvert_zones(zones, difference)
                if zones:
                    note = f" | {len(zones)} zone(s) converted to TradingView prices"
            else:
                note = " | scanner is running: zones stay locked"
            save(app)
            st.success(f"Price difference = {difference:.2f}{note}")

    shown = current_difference(app)
    st.metric("Futures - XAUUSD", "--" if shown is None else f"{shown:.2f}")


def zone_block(app: dict, kind: str, number: int) -> None:
    key = "buy_zones" if kind == "BUY" else "sell_zones"
    st.subheader(f"{number}. {kind} ZONES")
    st.caption(f"Enter the upper and lower price of every {kind} zone from "
               f"your futures chart. Each zone is monitored from "
               f"{LOOKBACK_MINUTES} minutes before you enter it, until you delete it.")

    with st.form(f"add_{kind}", clear_on_submit=True):
        c1, c2 = st.columns(2)
        upper = c1.number_input("Upper price", min_value=0.0, step=0.01, format="%.2f")
        lower = c2.number_input("Lower price", min_value=0.0, step=0.01, format="%.2f")
        submitted = st.form_submit_button(f"+ ADD {kind} ZONE")

    if submitted:
        difference = current_difference(app)
        if upper <= 0 or lower <= 0 or upper == lower:
            st.warning("Enter two different prices.")
        elif difference is None:
            st.warning("Enter BOTH prices and press CALCULATE DIFFERENCE first, "
                       "so the zone can be converted to TradingView prices.")
        else:
            app[key].append(make_zone(kind, upper, lower, difference))
            app[key].sort(key=lambda z: z["converted_lower"])
            save(app)
            st.rerun()

    if not app[key]:
        st.write(f"No {kind} zones yet.")
    for zone in app[key]:
        text, button = st.columns([4, 1])
        text.markdown(
            f"**TradingView {zone['converted_lower']:.2f} - {zone['converted_upper']:.2f}**  \n"
            f"futures {zone['futures_lower']:.2f} - {zone['futures_upper']:.2f}")
        if button.button("Delete", key=f"del_{zone['id']}"):
            app[key] = [z for z in app[key] if z["id"] != zone["id"]]
            save(app)
            st.rerun()


@st.fragment(run_every=5)
def live_panel() -> None:
    app = storage.load_app_state()
    saved = storage.load_engine_state()
    eng_state = engine_state_for(app, saved)
    monitor = storage.read("monitorStatus", {}) or {}

    # ---- monitor heartbeat
    if app.get("scanner_running"):
        age = age_seconds(monitor.get("last_beat"))
        if age is None or age > 180:
            st.error("MONITOR OFFLINE - the background job is not reporting. "
                     "Check the GitHub Actions tab of your repository.")
        elif monitor.get("paused"):
            st.info("SCANNING PAUSED - market closed (weekend).")
        else:
            st.success(f"SCANNING - monitor online (last check {int(age)} s ago)")
        if monitor.get("last_error"):
            st.warning(f"Last monitor error: {monitor['last_error']}")
    else:
        st.info("SCANNER STOPPED")

    price = live_price()
    if price is not None:
        st.metric("XAUUSD live price", f"{price:.2f}")

    engine = Engine(app["buy_zones"], app["sell_zones"], app.get("settings"), eng_state)

    # ---- CURRENT SIGNAL: only a validated (entered) trade
    st.subheader("CURRENT SIGNAL")
    trade = engine.open_trade
    with st.container(border=True):
        if trade:
            st.markdown(f"### {trade['direction']} - TRADE ACTIVE")
            zone_text = (f"{trade['zone_lower']:.2f} - {trade['zone_upper']:.2f}"
                         if trade.get("zone_lower") is not None else "--")
            c1, c2 = st.columns(2)
            c1.metric("Zone", zone_text)
            c2.metric("Entry", money(trade["entry"]))
            c3, c4, c5 = st.columns(3)
            c3.metric("Stop Loss", money(trade["sl"]))
            c4.metric("Take Profit", money(trade["tp"]))
            c5.metric("Risk / Reward", f"{trade['rr']:.2f}:1")
        else:
            st.markdown("**NO ACTIVE TRADE**")
            st.caption("Setups still waiting for their entry are listed below, "
                       "in the monitored zones.")

    # ---- monitored zones (waiting for entry)
    st.subheader("MONITORED ZONES")
    rows = []
    for zones in (app["buy_zones"], app["sell_zones"]):
        for zone in zones:
            sell = zone["type"] == "SELL"
            tp = engine.take_profit({"direction": zone["type"], "zone_id": zone["id"]})
            rows.append({
                "Type": zone["type"],
                "Zone (TradingView)": f"{zone['converted_lower']:.2f} - {zone['converted_upper']:.2f}",
                "Status": engine.zone_status(zone) if app.get("scanner_running") else "STOPPED",
                "Entry": money(zone["converted_lower"] if sell else zone["converted_upper"]),
                "SL": money(zone["converted_upper"] if sell else zone["converted_lower"]),
                "TP": money(tp),
            })
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.write("No zones yet.")


def controls(app: dict) -> None:
    running = bool(app.get("scanner_running"))
    start, stop = st.columns(2)

    if start.button("START 1M SCANNER", disabled=running, type="primary",
                    use_container_width=True):
        difference = current_difference(app)
        if not (app["buy_zones"] or app["sell_zones"]):
            st.warning("Add at least one zone first.")
        elif difference is None:
            st.warning("Press CALCULATE DIFFERENCE first.")
        else:
            app.update({"scanner_running": True, "start_id": new_id(),
                        "started_at": now_iso(), "locked_difference": difference})
            save(app)
            st.rerun()

    if stop.button("STOP SCANNER", disabled=not running, use_container_width=True):
        app["scanner_running"] = False
        save(app)
        st.rerun()

    if st.button("TEST NOTIFICATION"):
        outcome = notify.send("Test notification",
                              "Liquidity Zone Scanner test alert", direction="BUY")
        if outcome["ok"]:
            st.success("Notification sent - check your phone.")
        else:
            st.error(f"Notification failed: {outcome['message']}")

    if running:
        st.caption("The scanner runs in the background job. You can close this page.")


with tab_scanner:
    price_conversion(app)
    zone_block(app, "BUY", 2)
    zone_block(app, "SELL", 3)
    st.divider()
    controls(app)
    live_panel()


# ============================================================== XAUUSD TAB
with tab_xau:
    st.subheader("Converted zones (TradingView prices)")
    price = live_price()
    if price is not None:
        st.metric("XAUUSD live price", f"{price:.2f}")
    shown = current_difference(app)
    st.caption("Difference in use: " + ("--" if shown is None else f"{shown:.2f}"))
    for kind, key in (("BUY", "buy_zones"), ("SELL", "sell_zones")):
        st.markdown(f"**{kind} zones**")
        if not app[key]:
            st.write("No converted zones.")
            continue
        st.dataframe(pd.DataFrame([{
            "TradingView lower": round(z["converted_lower"], 2),
            "TradingView upper": round(z["converted_upper"], 2),
            "Futures lower": round(z["futures_lower"], 2),
            "Futures upper": round(z["futures_upper"], 2),
        } for z in app[key]]), hide_index=True, use_container_width=True)


# ============================================================ JOURNAL TAB
with tab_journal:
    st.subheader("Trading journal")
    journal = storage.load_journal()
    closed = [t for t in journal if t.get("status") in ("TP", "SL")]
    wins = sum(1 for t in closed if t["status"] == "TP")
    gains = sum(t.get("result_r", 0) for t in closed if t.get("result_r", 0) > 0)
    losses_r = -sum(t.get("result_r", 0) for t in closed if t.get("result_r", 0) < 0)
    total_r = sum(t.get("result_r", 0) for t in closed)

    a, b, c = st.columns(3)
    a.metric("Trades", len(journal))
    b.metric("Wins", wins)
    c.metric("Losses", len(closed) - wins)
    d, e, f = st.columns(3)
    d.metric("Win rate", f"{wins / len(closed) * 100:.1f}%" if closed else "--")
    e.metric("Total R", f"{total_r:+.2f}R")
    f.metric("Profit factor", f"{gains / losses_r:.2f}" if losses_r > 0 else ("inf" if gains > 0 else "--"))

    if journal:
        frame = pd.DataFrame(journal)
        wanted = ["opened_at", "direction", "entry", "sl", "tp", "rr", "status", "exit", "result_r"]
        st.dataframe(frame[[col for col in wanted if col in frame.columns]],
                     hide_index=True, use_container_width=True)
    else:
        st.write("No trades yet.")

    sure = st.checkbox("I want to delete the whole journal")
    if st.button("DELETE JOURNAL", disabled=not sure):
        storage.journal_clear()
        st.rerun()


# =========================================================== SETTINGS TAB
with tab_settings:
    st.subheader("Settings")
    current = {**DEFAULT_SETTINGS, **(app.get("settings") or {})}
    enable_buy = st.toggle("Enable BUY setups", value=bool(current["enable_buy"]))
    enable_sell = st.toggle("Enable SELL setups", value=bool(current["enable_sell"]))
    trade_lock = st.toggle("Trade lock (pause other zones while a trade is open)",
                           value=bool(current["trade_lock"]))
    minimum_rr = st.number_input("Minimum risk / reward", min_value=0.1,
                                 max_value=50.0, step=0.1,
                                 value=float(current["minimum_rr"]))
    if st.button("SAVE SETTINGS", type="primary"):
        app["settings"] = {"enable_buy": enable_buy, "enable_sell": enable_sell,
                           "trade_lock": trade_lock, "minimum_rr": float(minimum_rr)}
        save(app)
        st.success("Settings saved. The background monitor uses them on its next check.")

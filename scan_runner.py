"""One pass of the scanner: app settings + saved engine state + candles."""

from __future__ import annotations

from scan_engine import Engine, Services, now_iso


def run_tick(app: dict, saved: dict, candles: list[dict],
             services: Services) -> tuple[dict, list[str]]:
    """Returns (new engine state, log lines)."""
    if saved.get("start_id") != app.get("start_id"):
        saved = {}                                   # Start was pressed: fresh run
    engine = Engine(app.get("buy_zones", []), app.get("sell_zones", []),
                    app.get("settings"), saved, services)
    engine.tick(candles)
    state = engine.export_state()
    state.update({
        "start_id": app.get("start_id"),
        "running": True,
        "updated_at": now_iso(),
    })
    return state, engine.events

"""
State Writer — serialises SharedState to state.json every cycle.
The dashboard reads this file for live data.
Points to v2/state.json so both v1 and v2 can run side-by-side.
"""
import json
import asyncio
from datetime import datetime
from pathlib import Path

# Write alongside v2/ so the dashboard can be pointed at it
STATE_PATH = Path(__file__).parent.parent / "state_v2.json"


def write_state(state, bridge, settings):
    """Synchronous — call via asyncio.to_thread."""
    try:
        account   = bridge.get_account_info()
        positions = bridge.get_positions()

        interval_min = settings.scan_interval_minutes
        last         = state.last_scan_time
        next_scan    = None
        if last:
            try:
                from datetime import timedelta
                last_dt  = datetime.strptime(last, "%Y-%m-%d %H:%M UTC")
                next_dt  = last_dt + timedelta(minutes=interval_min)
                next_scan = next_dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                pass

        # Enrich positions with current tick P&L if possible
        enriched = []
        for p in positions:
            pos = dict(p)
            # Normalise field names for dashboard compatibility
            pos.setdefault("symbol",       pos.pop("pair",       pos.get("symbol", "")))
            pos.setdefault("currentPrice", pos.pop("open_price", pos.get("currentPrice", 0)))
            pos.setdefault("type",         pos.get("direction",  ""))
            pos.setdefault("openTime",     pos.get("open_time",  ""))
            enriched.append(pos)

        data = {
            "updated_at":            datetime.utcnow().isoformat() + "Z",
            "version":               "2",
            "mode":                  getattr(state, "mode", "auto"),
            "running":               getattr(state, "trading_active", True),
            "trading_active":        getattr(state, "trading_active", True),
            "circuit_breaker_active":getattr(state, "circuit_breaker_active", False),
            "entry_block_reason":     getattr(state, "entry_block_reason", ""),
            "trading_session_key":    getattr(state, "trading_session_key", None),
            "session_start_equity":   getattr(state, "session_start_equity", None),
            "session_equity_pnl_pct": round(getattr(state, "session_equity_pnl_pct", 0), 3),
            "balance":               account.get("balance", 0),
            "equity":                account.get("equity", 0),
            "daily_pnl_usd":         round(getattr(state, "daily_pnl_usd", 0), 2),
            "session_pnl":           round(getattr(state, "daily_pnl_usd", 0), 2),
            "total_trades_today":    getattr(state, "total_trades_today", 0),
            "wins_today":            getattr(state, "wins_today", 0),
            "losses_today":          getattr(state, "losses_today", 0),
            "win_rate_today":        round(getattr(state, "win_rate_today", 0), 1),
            "open_positions":        enriched,
            "active_sessions":       getattr(state, "active_sessions", []),
            "session_bonus":         getattr(state, "session_bonus", 0),
            "last_scan_time":        last,
            "next_scan_time":        next_scan,
            "scan_count":            getattr(state, "scan_count", 0),
            "pairs":                 list(settings.pairs_with_suffix),
            "broker":                settings.broker.get("name", ""),
            "scan_interval_minutes": interval_min,
            "dev_mode":              getattr(settings, "dev_mode", True),
        }

        STATE_PATH.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        print(f"[StateWriter] Warning: {e}")


async def write_state_async(state, bridge, settings):
    await asyncio.to_thread(write_state, state, bridge, settings)

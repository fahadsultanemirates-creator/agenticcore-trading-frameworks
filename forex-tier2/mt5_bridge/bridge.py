"""
MT5Bridge v2 — wraps MetaTrader5 Python library.
Key improvement over v1: get_tick() fetches the LIVE bid/ask at order time,
so SL/TP are never calculated from stale candle-close prices.
Falls back to MockBridge on Linux / when MT5 not installed.
"""
import sys
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    mt5 = None  # type: ignore


def _verify_connected_account(info, expected_login: int, expected_server: str) -> None:
    """Fail closed if the pre-logged-in MT5 terminal is not the configured account."""
    actual_login = int(getattr(info, "login", 0) or 0)
    actual_server = str(getattr(info, "server", "") or "")
    if actual_login != int(expected_login) or actual_server != str(expected_server):
        raise ConnectionError(
            "MT5 terminal identity does not match the protected configuration; "
            "refusing to trade."
        )


def _aggregate_position_exit_deals(deals, position_ticket: int, exit_entries: set) -> dict:
    """Aggregate every exit deal for one MT5 position, including partial exits."""
    exits = [
        deal for deal in deals
        if deal.position_id == position_ticket and deal.entry in exit_entries
    ]
    if not exits:
        return {}
    latest = exits[-1]
    return {
        "ticket": position_ticket,
        "deal_ids": [int(deal.ticket) for deal in exits],
        "pair": latest.symbol,
        "profit": sum(
            deal.profit + deal.swap + deal.commission + getattr(deal, "fee", 0)
            for deal in exits
        ),
        "time": latest.time,
        "reason": str(latest.reason),
    }


class MT5Bridge:
    """Real MT5 bridge — Windows + MT5 terminal installed and logged in."""

    TF_MAP = {
        "M1":  mt5.TIMEFRAME_M1  if MT5_AVAILABLE else 1,
        "M5":  mt5.TIMEFRAME_M5  if MT5_AVAILABLE else 5,
        "M15": mt5.TIMEFRAME_M15 if MT5_AVAILABLE else 15,
        "M30": mt5.TIMEFRAME_M30 if MT5_AVAILABLE else 30,
        "H1":  mt5.TIMEFRAME_H1  if MT5_AVAILABLE else 60,
        "H4":  mt5.TIMEFRAME_H4  if MT5_AVAILABLE else 240,
        "D1":  mt5.TIMEFRAME_D1  if MT5_AVAILABLE else 1440,
    } if MT5_AVAILABLE else {}

    def __init__(self, login: int, password: str, server: str):
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 library not available (requires Windows).")
        # Connect to already-running terminal — don't re-auth (kicks existing session)
        if not mt5.initialize():
            raise ConnectionError(f"MT5 init failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise ConnectionError("MT5 connected but no account info — is MT5 logged in?")
        try:
            _verify_connected_account(info, login, server)
        except ConnectionError:
            mt5.shutdown()
            raise
        print(f"[MT5Bridge] Connected to MT5 server {info.server}")

    def is_connected(self) -> bool:
        return MT5_AVAILABLE and mt5.terminal_info() is not None

    def get_ohlcv(self, pair: str, timeframe: str = "M15", bars: int = 200) -> Optional[pd.DataFrame]:
        tf = self.TF_MAP.get(timeframe, mt5.TIMEFRAME_M15)
        mt5.symbol_select(pair, True)
        rates = mt5.copy_rates_from_pos(pair, tf, 0, bars)
        if rates is None or len(rates) == 0:
            print(f"[MT5Bridge] No data for {pair} {timeframe}")
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df[["time", "open", "high", "low", "close", "tick_volume"]]

    def get_tick(self, pair: str) -> dict:
        """
        KEY FIX: Fetch the LIVE bid/ask at the moment of order placement.
        Never use TA candle-close price for SL/TP calculation.
        """
        mt5.symbol_select(pair, True)
        tick = mt5.symbol_info_tick(pair)
        if tick is None:
            raise RuntimeError(f"No tick for {pair} — is the symbol visible in Market Watch?")
        return {"bid": tick.bid, "ask": tick.ask, "pair": pair}

    def get_account_info(self) -> dict:
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance": info.balance, "equity": info.equity,
            "margin": info.margin, "free_margin": info.margin_free,
            "currency": info.currency, "leverage": info.leverage,
        }

    def get_positions(self) -> list[dict]:
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [{
            "ticket":     p.ticket,
            "pair":       p.symbol,
            "direction":  "BUY" if p.type == 0 else "SELL",
            "lot":        p.volume,
            "open_price": p.price_open,
            "sl":         p.sl,
            "tp":         p.tp,
            "profit":     p.profit,
            "open_time":  str(p.time),
        } for p in positions]

    def _best_filling(self, request: dict) -> dict:
        """Try FOK → IOC → RETURN until one succeeds."""
        for filling in [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                return {"success": True, "ticket": result.order, "price": result.price}
            if result.retcode not in (10009, 10030):
                # 10009 = Done (alternate retcode), 10030 = unsupported filling — try next
                return {"success": False, "error": result.comment, "retcode": result.retcode}
        return {"success": False, "error": result.comment, "retcode": result.retcode}

    def _filling_for_config(self, mode: str) -> list:
        """Return filling modes to try based on config (auto | FOK | IOC | RETURN)."""
        if mode == "FOK":
            return [mt5.ORDER_FILLING_FOK]
        elif mode == "IOC":
            return [mt5.ORDER_FILLING_IOC]
        elif mode == "RETURN":
            return [mt5.ORDER_FILLING_RETURN]
        else:  # auto
            return [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]

    def place_order(self, pair: str, direction: str, lot: float,
                    sl_price: float, tp_price: float, comment: str = "",
                    filling_mode: str = "auto") -> dict:
        """
        Place order using LIVE tick price for entry.
        sl_price and tp_price must already be absolute price levels
        (calculated from live tick in RiskManagementAgent).
        """
        # Fetch the freshest possible tick right before sending
        tick  = self.get_tick(pair)
        price = tick["ask"] if direction == "BUY" else tick["bid"]

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    pair,
            "volume":    lot,
            "type":      order_type,
            "price":     price,
            "sl":        sl_price,
            "tp":        tp_price,
            "deviation": 20,
            "magic":     20260819,
            "comment":   comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        modes = self._filling_for_config(filling_mode)
        for filling in modes:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result.retcode in (mt5.TRADE_RETCODE_DONE, 10009):
                return {"success": True, "ticket": result.order, "price": result.price}
            if result.retcode != 10030:
                return {"success": False, "error": result.comment, "retcode": result.retcode}
        return {"success": False, "error": result.comment, "retcode": result.retcode}

    def close_position(self, ticket: int) -> dict:
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"success": False, "error": "Position not found"}
        p          = pos[0]
        close_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        tick       = self.get_tick(p.symbol)
        price      = tick["bid"] if p.type == 0 else tick["ask"]
        request = {
            "action":      mt5.TRADE_ACTION_DEAL,
            "symbol":      p.symbol,
            "volume":      p.volume,
            "type":        close_type,
            "position":    ticket,
            "price":       price,
            "deviation":   20,
            "magic":       20260819,
            "comment":     "AC-Plus close",
            "type_time":   mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result  = mt5.order_send(request)
        success = result.retcode in (mt5.TRADE_RETCODE_DONE, 10009)
        return {
            "success":   success,
            "profit":    p.profit if success else 0,
            "pair":      p.symbol,
            "direction": "BUY" if p.type == 0 else "SELL",
            "error":     result.comment if not success else "",
        }

    def close_all_positions(self) -> list[dict]:
        return [self.close_position(p["ticket"]) for p in self.get_positions()]

    def get_closed_deal(self, position_ticket: int) -> dict:
        """Return the aggregate broker-confirmed exit result for one position."""
        # A position may be partially closed long before its final exit. Query a
        # wide account-history window so its final reconciliation includes every
        # exit deal, not only the most recent close.
        since = datetime(2000, 1, 1)
        deals = mt5.history_deals_get(since, datetime.now()) or []
        exit_entries = {mt5.DEAL_ENTRY_OUT, getattr(mt5, "DEAL_ENTRY_OUT_BY", -1)}
        return _aggregate_position_exit_deals(deals, position_ticket, exit_entries)

    def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl": sl, "tp": tp,
        }
        result = mt5.order_send(request)
        return {"success": result.retcode in (mt5.TRADE_RETCODE_DONE, 10009)}

    def shutdown(self):
        if MT5_AVAILABLE:
            mt5.shutdown()


def get_bridge(settings):
    """Return real MT5Bridge or MockBridge based on config and OS."""
    from mt5_bridge.mock_bridge import MockBridge
    print(f"[Bridge] MT5 available: {MT5_AVAILABLE} | dev_mode: {settings.dev_mode}")
    if settings.dev_mode:
        print("[Bridge] Dev mode — using MockBridge (synthetic data).")
        return MockBridge()
    if not MT5_AVAILABLE:
        raise RuntimeError("Live mode requires the MetaTrader5 package and terminal; refusing MockBridge.")

    mt5_cfg = settings.mt5
    missing = [
        env_name for env_name, value in (
            ("MT5_LOGIN", mt5_cfg.get("login")),
            ("MT5_PASSWORD", mt5_cfg.get("password")),
            ("MT5_SERVER", mt5_cfg.get("server")),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            f"Live MT5 startup blocked: configure protected environment values {', '.join(missing)}."
        )
    try:
        print(f"[Bridge] Connecting to configured MT5 server {mt5_cfg['server']}")
        bridge = MT5Bridge(
            login=int(mt5_cfg["login"]),
            password=mt5_cfg["password"],
            server=mt5_cfg["server"],
        )
    except Exception as error:
        raise RuntimeError(f"Live MT5 connection failed; refusing MockBridge: {error}") from error
    print("[Bridge] ✅ MT5Bridge connected — live trading active.")
    return bridge

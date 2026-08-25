"""
Premium Tier 1 MT5 Bridge.

Key design decisions:
- Explicit terminal-path support via mt5.initialize(path=...) when provided.
- NO credentials passed to mt5.initialize() — the terminal must already be
  logged in (starting credentials triggers a new session and can kick the
  existing one).
- Broker/account/server identity is validated BEFORE allowing any trading
  action. Identity validation is separate from connection.
- Symbol suffix is applied transparently.
- Completed-candle retrieval uses copy_rates_from_pos with offset=1 so the
  most-recent (incomplete) candle is excluded.
- Live tick retrieved separately for entry/spread decisions.
- Magic number: PREMIUM_MAGIC_NUMBER (20260101), never shared with Tier 2.
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

from config.settings import PREMIUM_MAGIC_NUMBER


class IdentityValidationError(Exception):
    """Raised when the connected terminal does not match expected identity."""


class PremiumMT5Bridge:
    """
    Real MT5 bridge for Premium Tier 1.

    Does NOT pass credentials to initialize() — the terminal must be
    logged in before this bridge connects.  Identity (broker, server,
    account number, account type) is verified before any trading action.
    """

    TF_MAP = {
        "M1":  getattr(mt5, "TIMEFRAME_M1",  1),
        "M5":  getattr(mt5, "TIMEFRAME_M5",  5),
        "M15": getattr(mt5, "TIMEFRAME_M15", 15),
        "M30": getattr(mt5, "TIMEFRAME_M30", 30),
        "H1":  getattr(mt5, "TIMEFRAME_H1",  60),
        "H4":  getattr(mt5, "TIMEFRAME_H4",  240),
        "D1":  getattr(mt5, "TIMEFRAME_D1",  1440),
    }

    def __init__(
        self,
        terminal_path: Optional[str] = None,
        symbol_suffix: str = "",
        expected_broker: Optional[str] = None,
        expected_server: Optional[str] = None,
        expected_login: Optional[int] = None,
        expected_account_type: Optional[str] = None,
    ):
        if not MT5_AVAILABLE:
            raise RuntimeError(
                "MetaTrader5 library not available — requires Windows with MT5 installed."
            )

        self.symbol_suffix = symbol_suffix
        self.expected_broker = expected_broker
        self.expected_server = expected_server
        self.expected_login = expected_login
        self.expected_account_type = expected_account_type
        self._identity_verified = False

        # Connect to already-running terminal. Supply path if given.
        init_kwargs = {}
        if terminal_path:
            init_kwargs["path"] = terminal_path

        if not mt5.initialize(**init_kwargs):
            raise ConnectionError(
                f"Premium MT5 initialize() failed: {mt5.last_error()}. "
                "Ensure the MT5 terminal is running and logged in."
            )

        info = mt5.account_info()
        if info is None:
            raise ConnectionError(
                "Premium MT5 connected but no account info. Is the terminal logged in?"
            )

        print(
            f"[PremiumMT5] Connected — account {info.login} @ {info.server} "
            f"broker='{info.company}' type={'DEMO' if info.trade_mode == 0 else 'LIVE'}"
        )

    def validate_identity(self) -> bool:
        """
        Validate broker/server/account/type before allowing trading actions.
        Returns True if identity matches all configured expectations.
        Raises IdentityValidationError with a clear message on mismatch.
        """
        if not MT5_AVAILABLE:
            return False

        info = mt5.account_info()
        if info is None:
            raise IdentityValidationError("Cannot retrieve account info for identity check.")

        actual_type = "DEMO" if info.trade_mode == 0 else "LIVE"

        errors = []
        if self.expected_broker and self.expected_broker.lower() not in info.company.lower():
            errors.append(
                f"Broker mismatch: expected '{self.expected_broker}', got '{info.company}'"
            )
        if self.expected_server and self.expected_server.lower() not in info.server.lower():
            errors.append(
                f"Server mismatch: expected '{self.expected_server}', got '{info.server}'"
            )
        if self.expected_login and info.login != self.expected_login:
            errors.append(
                f"Account mismatch: expected {self.expected_login}, got {info.login}"
            )
        if self.expected_account_type and actual_type != self.expected_account_type.upper():
            errors.append(
                f"Account type mismatch: expected {self.expected_account_type}, got {actual_type}"
            )

        if errors:
            raise IdentityValidationError(
                "Premium MT5 identity validation FAILED:\n" + "\n".join(errors)
            )

        self._identity_verified = True
        print(f"[PremiumMT5] Identity validated — {actual_type} account {info.login} @ {info.server}")
        return True

    def _sym(self, pair: str) -> str:
        """Apply symbol suffix if the raw pair doesn't already have it."""
        if self.symbol_suffix and not pair.endswith(self.symbol_suffix):
            return pair + self.symbol_suffix
        return pair

    def is_connected(self) -> bool:
        return MT5_AVAILABLE and mt5.terminal_info() is not None

    def get_live_tick(self, pair: str) -> dict:
        """
        Fetch the live bid/ask tick. Used for spread checks and entry price.
        NEVER use OHLCV close prices for SL/TP calculation — always call this.
        """
        sym = self._sym(pair)
        mt5.symbol_select(sym, True)
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            raise RuntimeError(
                f"[PremiumMT5] No tick for {sym}. "
                "Is the symbol visible in Market Watch?"
            )
        spread = tick.ask - tick.bid
        return {
            "pair": pair,
            "symbol": sym,
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": spread,
            "time": datetime.utcfromtimestamp(tick.time).isoformat(),
        }

    def get_completed_candles(
        self, pair: str, timeframe: str = "M15", bars: int = 200
    ) -> Optional[pd.DataFrame]:
        """
        Return only COMPLETED candles (offset=1 excludes the forming candle).
        Column order: time, open, high, low, close, tick_volume.
        """
        sym = self._sym(pair)
        tf = self.TF_MAP.get(timeframe)
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        mt5.symbol_select(sym, True)
        # offset=1 skips the currently-forming candle
        rates = mt5.copy_rates_from_pos(sym, tf, 1, bars)
        if rates is None or len(rates) == 0:
            print(f"[PremiumMT5] No candle data for {sym} {timeframe}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)

    def get_account_info(self) -> dict:
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "currency": info.currency,
            "leverage": info.leverage,
            "login": info.login,
            "server": info.server,
            "company": info.company,
            "account_type": "DEMO" if info.trade_mode == 0 else "LIVE",
        }

    def get_positions(self) -> list:
        """Return all open positions with Premium magic number."""
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket": p.ticket,
                "pair": p.symbol,
                "direction": "BUY" if p.type == 0 else "SELL",
                "lot": p.volume,
                "open_price": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "magic": p.magic,
                "open_time": str(p.time),
            }
            for p in positions
            if p.magic == PREMIUM_MAGIC_NUMBER
        ]

    def place_order(
        self,
        pair: str,
        direction: str,
        lot: float,
        sl_price: float,
        tp_price: float,
        comment: str = "",
        filling_mode: str = "auto",
    ) -> dict:
        """
        Place a market order using LIVE tick price.
        sl_price/tp_price must be absolute price levels.
        Identity must be validated before calling.
        """
        if not self._identity_verified:
            return {
                "success": False,
                "error": "Identity not validated. Call validate_identity() before trading.",
            }

        tick = self.get_live_tick(pair)
        price = tick["ask"] if direction == "BUY" else tick["bid"]
        order_type = getattr(mt5, "ORDER_TYPE_BUY") if direction == "BUY" else getattr(mt5, "ORDER_TYPE_SELL")

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self._sym(pair),
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": 20,
            "magic": PREMIUM_MAGIC_NUMBER,
            "comment": f"PREMIUM-T1 {comment}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        modes = self._filling_modes(filling_mode)
        last_result = None
        for filling in modes:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            last_result = result
            if result.retcode in (mt5.TRADE_RETCODE_DONE, 10009):
                return {"success": True, "ticket": result.order, "price": result.price}
            if result.retcode != 10030:
                return {"success": False, "error": result.comment, "retcode": result.retcode}

        err = last_result.comment if last_result else "no result"
        return {"success": False, "error": err}

    def _filling_modes(self, mode: str) -> list:
        if mode == "FOK":
            return [mt5.ORDER_FILLING_FOK]
        elif mode == "IOC":
            return [mt5.ORDER_FILLING_IOC]
        elif mode == "RETURN":
            return [mt5.ORDER_FILLING_RETURN]
        return [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]

    def close_position(self, ticket: int) -> dict:
        if not self._identity_verified:
            return {"success": False, "error": "Identity not validated."}

        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return {"success": False, "error": "Position not found"}
        p = pos[0]
        if p.magic != PREMIUM_MAGIC_NUMBER:
            return {"success": False, "error": "Position does not belong to Premium Tier 1"}

        close_type = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        tick = self.get_live_tick(p.symbol)
        price = tick["bid"] if p.type == 0 else tick["ask"]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": PREMIUM_MAGIC_NUMBER,
            "comment": "PREMIUM-T1 close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(request)
        success = result.retcode in (mt5.TRADE_RETCODE_DONE, 10009)
        return {
            "success": success,
            "profit": p.profit if success else 0,
            "pair": p.symbol,
            "error": result.comment if not success else "",
        }

    def shutdown(self):
        if MT5_AVAILABLE:
            mt5.shutdown()

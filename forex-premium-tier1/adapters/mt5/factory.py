"""
Premium Tier 1 — MT5 bridge factory.

Returns the appropriate bridge based on mode and platform availability.
Mock and signal modes always use PremiumMockBridge on Linux.
Demo/auto modes attempt PremiumMT5Bridge on Windows only.
"""
import sys
from typing import Union

from config.settings import PremiumSettings
from .bridge import PremiumMT5Bridge
from .mock_bridge import PremiumMockBridge

try:
    import MetaTrader5  # noqa: F401
    _MT5_IMPORTABLE = True
except ImportError:
    _MT5_IMPORTABLE = False


def get_premium_bridge(
    settings: PremiumSettings,
) -> Union[PremiumMT5Bridge, PremiumMockBridge]:
    """
    Return the correct bridge for the given mode.

    mock on any platform          → PremiumMockBridge
    signal/demo/auto with MT5     → PremiumMT5Bridge (validates identity before use)
    signal/demo/auto without MT5  → PremiumMockBridge with warning
    """
    mode = settings.mode

    if mode == "mock" or not _MT5_IMPORTABLE:
        if mode != "mock" and not _MT5_IMPORTABLE:
            print(
                "[PremiumBridgeFactory] WARNING: MetaTrader5 not importable. "
                f"Cannot use mode='{mode}' without MT5. Falling back to mock."
            )
        else:
            print(f"[PremiumBridgeFactory] mode='{mode}' → PremiumMockBridge")
        return PremiumMockBridge(symbol_suffix=settings.mt5.symbol_suffix)

    # Windows + MT5 available, demo or auto mode
    try:
        bridge = PremiumMT5Bridge(
            terminal_path=settings.mt5.terminal_path,
            symbol_suffix=settings.mt5.symbol_suffix,
            expected_broker=settings.mt5.expected_broker,
            expected_server=settings.mt5.server,
            expected_login=settings.mt5.login,
            expected_account_type=settings.mt5.expected_account_type,
        )
        print(f"[PremiumBridgeFactory] mode='{mode}' → PremiumMT5Bridge connected")
        return bridge
    except Exception as exc:
        print(
            f"[PremiumBridgeFactory] MT5 connection failed: {exc}\n"
            "Falling back to PremiumMockBridge — execution DISABLED."
        )
        return PremiumMockBridge(symbol_suffix=settings.mt5.symbol_suffix)

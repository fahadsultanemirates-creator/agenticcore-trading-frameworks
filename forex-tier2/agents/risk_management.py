"""
Agent 5 — Risk Management Agent (Tier 2)
KEY FIX: SL/TP are now calculated from the LIVE tick price fetched
at order time, not from the stale TA candle-close price.
Returns pip distances; TradeExecutionAgent fetches the live tick
and converts to absolute price levels right before placing the order.
"""
from typing import Optional


# Pip value per pip per 0.01 lot in USD
PIP_VALUES_USD = {
    "EURUSD": 0.10, "GBPUSD": 0.10, "AUDUSD": 0.10, "NZDUSD": 0.10,
    "USDCAD": 0.074, "USDCHF": 0.111, "USDJPY": 0.067,
    "EURJPY": 0.067, "GBPJPY": 0.067,
    "XAUUSD": 0.10, "XAGUSD": 0.05,
    "USOIL":  0.10, "UKOIL":  0.10,
}
DEFAULT_PIP_VALUE = 0.10

# Pip size in price units
PIP_SIZE = {
    "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "CADJPY": 0.01,
    "XAUUSD": 0.10, "XAGUSD": 0.01, "USOIL": 0.01, "UKOIL": 0.01,
}
DEFAULT_PIP_SIZE = 0.0001


def _resolve(pair: str, table: dict, default):
    """Look up a pair, stripping any broker suffix if not found."""
    if pair in table:
        return table[pair]
    # Try without suffix (.m, etc.)
    clean = pair.rstrip("m").rstrip(".")
    return table.get(clean, default)


def _pip_size(pair: str) -> float:
    return _resolve(pair, PIP_SIZE, DEFAULT_PIP_SIZE)


def _pip_value_per_lot(pair: str) -> float:
    """USD value per pip for a standard lot (1.0)."""
    return _resolve(pair, PIP_VALUES_USD, DEFAULT_PIP_VALUE) * 100


def _fixed_lot_for_pair(pair: str, default_lot: Optional[float],
                        overrides: dict) -> Optional[float]:
    """Resolve a pair-specific fixed lot before the global default."""
    if overrides:
        if pair in overrides:
            return overrides[pair]
        clean_pair = pair.rstrip("m").rstrip(".")
        if clean_pair in overrides:
            return overrides[clean_pair]
    return default_lot


def _pair_target(pair: str, overrides: dict, default: float) -> float:
    """Resolve a pair-specific TP while retaining the currency-pair default."""
    if overrides:
        if pair in overrides:
            return float(overrides[pair])
        clean_pair = pair.rstrip("m").rstrip(".")
        if clean_pair in overrides:
            return float(overrides[clean_pair])
    return float(default)


def calculate_lot_size(balance: float, risk_pct: float, sl_usd: float,
                       fixed_lot_size: Optional[float] = None) -> float:
    """Return the configured fixed lot, or calculate a risk-based lot.

    A fixed lot is intentionally independent of balance and stop-loss dollar
    settings. This is useful for demo accounts or strategies that specify
    their exposure directly. Set fixed_lot_size to null to use the legacy
    risk-based calculation.
    """
    if fixed_lot_size is not None:
        fixed_lot = round(float(fixed_lot_size), 2)
        if fixed_lot <= 0:
            raise ValueError("fixed_lot_size must be greater than zero")
        return max(0.01, min(fixed_lot, 10.0))

    risk_usd = balance * (risk_pct / 100)
    lot      = round(risk_usd / max(sl_usd, 1.0) * 0.01, 2)
    return max(0.01, min(lot, 10.0))


def sl_tp_pips(pair: str, sl_usd: float, tp_usd: float, lot: float) -> tuple[float, float]:
    """
    Convert USD SL/TP amounts to pip distances.
    These pips are applied to the LIVE tick price at order execution time.
    """
    pip_val = _pip_value_per_lot(pair)
    sl_pips = sl_usd / (pip_val * lot) if pip_val and lot else 30
    tp_pips = tp_usd / (pip_val * lot) if pip_val and lot else 90
    return round(sl_pips, 1), round(tp_pips, 1)


class RiskManagementAgent:
    def __init__(self, settings):
        self.settings = settings

    def run(self, signals: dict, account_info: dict,
            open_positions: list, state) -> list[dict]:
        """
        Filter signals through risk rules.
        Returns approved trades with sl_pips/tp_pips (NOT price levels).
        TradeExecutionAgent converts these to absolute prices at execution time.
        """
        balance     = account_info.get("balance", 10000.0)
        risk_pct    = self.settings.risk_per_trade_pct
        sl_usd      = self.settings.default_sl_usd
        default_tp_usd = self.settings.default_tp_usd
        pair_tp_overrides = self.settings.get("pair_tp_usd", {})
        default_fixed_lot = self.settings.get("fixed_lot_size")
        fixed_lot_overrides = self.settings.get("fixed_lot_sizes", {})
        limits = self.settings.get("trading_guard", {}) or {}
        normal_limit = int(limits.get("normal_max_open_positions", 5))
        absolute_limit = int(limits.get("max_open_positions", 7))
        exceptional_confidence = float(limits.get("exceptional_min_confidence", 80))
        min_confidence = float(self.settings.get("min_signal_confidence", 65))

        # The manager and Telegram approval path set this after checking the
        # Dubai schedule and session-level P&L. Keep this second guard here so
        # no caller can accidentally bypass the policy.
        if state.entry_block_reason:
            print(f"[Risk] New entries blocked — {state.entry_block_reason}")
            return []

        open_pairs      = {p.get("pair", p.get("symbol", "")) for p in open_positions}
        if len(open_positions) >= absolute_limit:
            print(f"[Risk] Absolute position cap ({absolute_limit}) reached")
            return []

        candidates = sorted(
            (
                (pair, sig) for pair, sig in signals.items()
                if sig.get("direction") != "HOLD"
                and pair not in open_pairs
                and float(sig.get("confidence", 0)) >= min_confidence
            ),
            key=lambda item: float(item[1].get("confidence", 0)),
            reverse=True,
        )

        approved = []
        for pair, sig in candidates:
            occupied = len(open_positions) + len(approved)
            if occupied >= absolute_limit:
                break
            if occupied >= normal_limit and float(sig["confidence"]) <= exceptional_confidence:
                print(
                    f"[Risk] {pair} needs >{exceptional_confidence:.0f}% confidence "
                    f"for reserved position slot {occupied + 1}"
                )
                continue

            fixed_lot = _fixed_lot_for_pair(
                pair, default_fixed_lot, fixed_lot_overrides
            )
            lot = calculate_lot_size(
                balance, risk_pct, sl_usd, fixed_lot_size=fixed_lot
            )
            tp_usd = _pair_target(pair, pair_tp_overrides, default_tp_usd)
            sl_pips, tp_pips = sl_tp_pips(pair, sl_usd, tp_usd, lot)

            trade = {
                "pair":       pair,
                "direction":  sig["direction"],
                "lot":        lot,
                "sl_pips":    sl_pips,   # pip distance — applied to live tick at execution
                "tp_pips":    tp_pips,   # pip distance — applied to live tick at execution
                "sl_usd":     sl_usd,
                "tp_usd":     tp_usd,
                "confidence": sig["confidence"],
                "summary":    sig["summary"],
            }
            print(f"[Risk] ✅ Approved: {pair} {sig['direction']} lot={lot} "
                  f"SL={sl_pips}pips TP={tp_pips}pips conf={sig['confidence']:.0f}%")
            approved.append(trade)

        return approved

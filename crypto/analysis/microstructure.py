"""Conservative public order-book and recent-trade metrics for deep scanning."""

from __future__ import annotations

from typing import List, Optional

from domain.models import ContractDetail, Microstructure, OrderBook, RecentTrade


def analyze_microstructure(
    contract: Optional[ContractDetail],
    order_book: Optional[OrderBook],
    trades: List[RecentTrade],
) -> Microstructure:
    """
    Build exchange-level flow proxies from public data.

    Depth and aggressor pressure are ratios in exchange contract units, so they
    do not require fabricated currency conversion. Large-trade notional is only
    emitted when MEXC's contract size is explicitly known.
    """
    bid_volume = sum(quantity for _, quantity in (order_book.bids if order_book else []))
    ask_volume = sum(quantity for _, quantity in (order_book.asks if order_book else []))
    total_depth = bid_volume + ask_volume
    imbalance = (
        round(((bid_volume - ask_volume) / total_depth) * 100, 2)
        if total_depth > 0
        else None
    )

    buy_volume = sum(
        trade.quantity or 0.0 for trade in trades if trade.side == "buy"
    )
    sell_volume = sum(
        trade.quantity or 0.0 for trade in trades if trade.side == "sell"
    )
    total_trade_volume = buy_volume + sell_volume
    buy_pressure = (
        round((buy_volume / total_trade_volume) * 100, 2)
        if total_trade_volume > 0
        else None
    )

    notionals: List[float] = []
    contract_size = contract.contract_size if contract else None
    if contract_size is not None and contract_size > 0:
        for trade in trades:
            if (
                trade.price is not None
                and trade.price > 0
                and trade.quantity is not None
                and trade.quantity > 0
            ):
                notionals.append(trade.price * trade.quantity * contract_size)

    large_trade_count: Optional[int] = None
    largest_notional: Optional[float] = None
    if notionals:
        # Use the lower median so one extreme value in a very short public
        # sample cannot raise its own threshold and disappear as an outlier.
        baseline = sorted(notionals)[(len(notionals) - 1) // 2]
        threshold = max(5_000.0, baseline * 5)
        large_trade_count = sum(1 for value in notionals if value >= threshold)
        largest_notional = round(max(notionals), 2)

    return Microstructure(
        buy_pressure_pct=buy_pressure,
        order_book_imbalance_pct=imbalance,
        large_trade_count=large_trade_count,
        largest_trade_notional_usdt=largest_notional,
        fetched_at=order_book.fetched_at if order_book else None,
    )
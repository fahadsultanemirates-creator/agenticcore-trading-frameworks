"""
Agent 6 — Trade Execution Agent (Tier 2)
KEY UPGRADES over Tier 1:
  1. Fetches LIVE tick price at the moment of order — SL/TP calculated from
     live bid/ask, never from stale TA candle close. Fixes "Invalid stops".
  2. Sends Telegram alert on every trade OPEN with entry, SL, TP, confidence.
   3. Queues a close-accepted alert, then lets the monitor report broker-confirmed P&L.
   4. Records broker-confirmed closed trades to TradeMemoryAgent for adaptive confidence.
"""
import asyncio
from datetime import datetime
from typing import Optional, Callable


def _pips_to_price(direction: str, tick: dict, pair: str,
                   sl_pips: float, tp_pips: float) -> tuple[float, float, float]:
    """
    Convert pip distances to absolute SL/TP price levels using the LIVE tick.
    Returns (entry_price, sl_price, tp_price).
    """
    entry = tick["ask"] if direction == "BUY" else tick["bid"]

    # Determine pip size for this pair
    pip = 0.0001  # default for most majors
    if "JPY" in pair:
        pip = 0.01
    elif "XAU" in pair:
        pip = 0.10
    elif "XAG" in pair or "OIL" in pair:
        pip = 0.01

    if direction == "BUY":
        sl_price = round(entry - sl_pips * pip, 5)
        tp_price = round(entry + tp_pips * pip, 5)
    else:
        sl_price = round(entry + sl_pips * pip, 5)
        tp_price = round(entry - tp_pips * pip, 5)

    return entry, sl_price, tp_price


class TradeExecutionAgent:
    def __init__(self, bridge, settings, notify_fn: Optional[Callable] = None,
                 memory_agent=None, reporter=None, close_ledger=None):
        self.bridge   = bridge
        self.settings = settings
        self.notify   = notify_fn   # async fn(message: str)
        self.memory   = memory_agent
        self.reporter = reporter
        self.close_ledger = close_ledger

    async def execute_trade(self, trade: dict) -> dict:
        pair      = trade["pair"]
        direction = trade["direction"]
        lot       = trade["lot"]
        sl_pips   = trade["sl_pips"]
        tp_pips   = trade["tp_pips"]
        comment   = f"AC2-{direction[:1]}-{int(trade['confidence'])}"
        filling   = self.settings.broker.get("filling_mode", "auto")

        print(f"[Exec] Fetching live tick for {pair}...")
        try:
            tick = await asyncio.to_thread(self.bridge.get_tick, pair)
        except Exception as e:
            print(f"[Exec] ❌ Could not get tick for {pair}: {e}")
            return {"success": False, "error": str(e), "pair": pair}

        entry, sl_price, tp_price = _pips_to_price(direction, tick, pair, sl_pips, tp_pips)
        print(f"[Exec] {direction} {lot}lot {pair} | LIVE entry={entry:.5f} SL={sl_price:.5f} TP={tp_price:.5f} "
              f"| conf={trade['confidence']:.0f}%")

        try:
            result = await asyncio.to_thread(
                self.bridge.place_order, pair, direction, lot, sl_price, tp_price, comment, filling
            )
            if result.get("success"):
                actual_price = result.get("price", entry)
                print(f"[Exec] ✅ Opened #{result.get('ticket')} @ {actual_price:.5f}")

                # Telegram alert on trade OPEN
                if self.notify:
                    broker = self.settings.broker.get("name", "")
                    msg = (
                        f"✅ *Trade Opened — AgenticCore Forex PLUS*\n"
                        f"`{direction}` `{pair}` on {broker}\n"
                        f"Entry: `{actual_price:.5f}` | Lot: `{lot}`\n"
                        f"SL: `{sl_price:.5f}` | TP: `{tp_price:.5f}`\n"
                        f"Confidence: `{trade['confidence']:.0f}%`\n"
                        f"_{trade.get('summary', '')[:100]}_"
                    )
                    try:
                        await self.notify(msg)
                    except Exception:
                        pass

                return {
                    **result, "pair": pair, "direction": direction,
                    "lot": lot, "sl": sl_price, "tp": tp_price,
                    "price": actual_price, "time": datetime.utcnow().isoformat(),
                }
            else:
                print(f"[Exec] ❌ Order failed: {result.get('error')}")
                return {**result, "pair": pair}
        except Exception as e:
            print(f"[Exec] Exception placing {pair}: {e}")
            return {"success": False, "error": str(e), "pair": pair}

    async def execute_all(self, approved_trades: list[dict], state) -> list[dict]:
        results = []
        for trade in approved_trades:
            result = await self.execute_trade(trade)
            if result.get("success"):
                results.append(result)
            await asyncio.sleep(0.5)
        return results

    async def close_position(self, ticket: int, state, reporter=None,
                             close_reason: str = "manual") -> dict:
        print(f"[Exec] Closing #{ticket}")
        position = next(
            (item for item in state.open_positions if item.get("ticket") == ticket),
            {"ticket": ticket},
        )
        if self.close_ledger:
            pending = self.close_ledger.get_pending(ticket)
            if pending is None:
                self.close_ledger.request(
                    ticket, position, close_reason,
                    session_key=getattr(state, "trading_session_key", None),
                )
            elif pending.get("status") != "prepared":
                return {"success": True, "ticket": ticket, "pending_confirmation": True}

        result = await asyncio.to_thread(self.bridge.close_position, ticket)
        if not result.get("success") and self.close_ledger:
            self.close_ledger.discard_request(ticket)
        if result.get("success"):
            if self.close_ledger:
                self.close_ledger.mark_submitted(ticket)
            pair      = result.get("pair", "")
            direction = result.get("direction", "")
            position = {**position, "pair": pair or position.get("pair", ""),
                        "direction": direction or position.get("direction", "")}
            state.closed_by_framework_tickets.add(ticket)
            state.pending_broker_closures.setdefault(
                ticket, {"position": position, "alerted": True}
            )
            print(f"[Exec] ✅ Close accepted for #{ticket} {pair}; awaiting broker confirmation")

            # Do not treat the pre-close position snapshot as realised P&L.
            # The portfolio monitor will query MT5 history and send the final
            # result once the broker confirms the exit deal.
            if self.notify:
                msg   = (
                    f"⏳ *Close Request Accepted — #{ticket}*\n"
                    f"`{pair}` ({direction})\n"
                    "Waiting for the broker-confirmed result."
                )
                try:
                    await self.notify(msg)
                except Exception:
                    pass

        return result

    async def close_all(self, state, close_reason: str = "manual") -> list[dict]:
        print("[Exec] ⛔ CLOSING ALL POSITIONS")
        positions = list(state.open_positions)
        results = []
        for position in positions:
            results.append(
                await self.close_position(
                    position["ticket"], state, close_reason=close_reason
                )
            )
        accepted = sum(1 for result in results if result.get("success"))
        print(f"[Exec] Close requests accepted: {accepted}/{len(results)}; awaiting MT5 confirmation.")
        return results

    async def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        return await asyncio.to_thread(self.bridge.modify_position, ticket, sl, tp)

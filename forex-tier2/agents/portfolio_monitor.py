"""
Agent 7 — Portfolio Monitor (Tier 2)
Upgrades over Tier 1:
  1. Trailing stop — moves SL to breakeven when trade is 50% to TP,
     then trails SL as price improves further.
  2. Telegram alert when SL is moved (breakeven or trail).
  3. Circuit breaker with Telegram alert (same as Tier 1).
"""
import asyncio
from datetime import datetime
from typing import Optional, Callable


def _pip_size(pair: str) -> float:
    if "JPY" in pair:
        return 0.01
    elif "XAU" in pair:
        return 0.10
    elif "XAG" in pair or "OIL" in pair:
        return 0.01
    return 0.0001


class PortfolioMonitorAgent:
    def __init__(self, bridge, settings, execution_agent, state,
                  notify_fn: Optional[Callable] = None, entry_guard=None, reporter=None,
                  close_ledger=None):
        self.bridge    = bridge
        self.settings  = settings
        self.execution = execution_agent
        self.state     = state
        self.notify    = notify_fn
        self.entry_guard = entry_guard
        self.reporter = reporter
        self.close_ledger = close_ledger
        self._running  = False
        self._refresh_lock = asyncio.Lock()
        # Track tickets with the 65%/35% profit lock applied.
        self._trail_locked_set: set[int] = set()

    async def _reconcile_broker_closures(self, previous: list[dict],
                                         current: list[dict]) -> None:
        """Track missing positions until MT5 history confirms their final P&L."""
        current_tickets = {p.get("ticket") for p in current}
        if self.close_ledger:
            for confirmed in self.close_ledger.unaccounted_confirmations():
                await self._account_confirmed_close(confirmed)

        for position in previous:
            ticket = position.get("ticket")
            if not ticket or ticket in current_tickets:
                continue
            # Framework-initiated closes are also reconciled against history.
            # Their direct close response is not a broker-confirmed result.
            self.state.closed_by_framework_tickets.discard(ticket)
            if self.close_ledger:
                self.close_ledger.request(
                    ticket, position, "broker_closed",
                    session_key=getattr(self.state, "trading_session_key", None),
                )
            self.state.pending_broker_closures.setdefault(ticket, {"position": position, "alerted": False})

        if self.close_ledger:
            for entry in self.close_ledger.pending():
                ticket = entry["ticket"]
                self.state.pending_broker_closures.setdefault(
                    ticket, {"position": entry["position"], "alerted": True}
                )
            for entry in self.close_ledger.prepared():
                if self.execution and entry["ticket"] in current_tickets:
                    await self.execution.close_position(
                        entry["ticket"], self.state,
                        close_reason=entry.get("reason", "manual"),
                    )

        for ticket, pending in list(self.state.pending_broker_closures.items()):
            if ticket in current_tickets:
                # A close requested before a restart may still be visible while
                # MT5 processes it. Keep the durable request and avoid resending.
                continue
            position = pending["position"]
            deal = {}
            if hasattr(self.bridge, "get_closed_deal"):
                deal = await asyncio.to_thread(self.bridge.get_closed_deal, ticket)
            profit = deal.get("profit")
            pair = deal.get("pair") or position.get("pair", "")
            direction = position.get("direction", "")
            if profit is None:
                if self.notify and not pending["alerted"]:
                    pending["alerted"] = True
                    await self.notify(
                        f"📉 *MT5 Position Closed*\n`{pair}` ({direction}) #{ticket}\n"
                        "Broker result is being confirmed."
                    )
                continue

            if self.close_ledger:
                confirmed = self.close_ledger.mark_confirmed(ticket, deal)
                if confirmed is None:
                    self.state.pending_broker_closures.pop(ticket, None)
                    continue
                await self._account_confirmed_close(confirmed)
            else:
                await self._account_confirmed_close({
                    "ticket": ticket, "position": position, "deal": deal,
                })
            self.state.pending_broker_closures.pop(ticket, None)

    async def _account_confirmed_close(self, confirmed: dict) -> None:
        """Apply a durable broker-confirmed exit, then mark it accounted."""
        ticket = confirmed["ticket"]
        position = confirmed.get("position", {})
        deal = confirmed.get("deal", {})
        profit = float(deal.get("profit", 0))
        pair = deal.get("pair") or position.get("pair", "")
        direction = position.get("direction", "")
        self.state.record_trade_result_once(ticket, profit)
        effect_key = f"broker-close:{ticket}"
        if self.reporter and (
            not self.close_ledger or not self.close_ledger.effect_done(ticket, "reporter")
        ):
            await self.reporter.log_trade({
                **position, "pair": pair, "direction": direction,
                "ticket": ticket, "profit": profit, "status": "closed",
            }, "broker-confirmed close", idempotency_key=effect_key)
            if self.close_ledger:
                self.close_ledger.mark_effect_done(ticket, "reporter")
        elif self.close_ledger:
            self.close_ledger.mark_effect_done(ticket, "reporter")
        if self.execution and getattr(self.execution, "memory", None) and pair and (
            not self.close_ledger or not self.close_ledger.effect_done(ticket, "memory")
        ):
            try:
                await self.execution.memory.record_trade(
                    pair, profit, idempotency_key=effect_key
                )
                if self.close_ledger:
                    self.close_ledger.mark_effect_done(ticket, "memory")
            except Exception as error:
                print(f"[Monitor] Could not update trade memory for {pair}: {error}")
        elif self.close_ledger:
            self.close_ledger.mark_effect_done(ticket, "memory")
        if self.notify and (
            not self.close_ledger or not self.close_ledger.effect_done(ticket, "notification")
        ):
            emoji = "🟢" if profit >= 0 else "🔴"
            await self.notify(
                f"{emoji} *MT5 Close Confirmed*\n`{pair}` ({direction}) #{ticket}\n"
                f"P&L: `${'+' if profit >= 0 else ''}{profit:.2f}`"
            )
            if self.close_ledger:
                self.close_ledger.mark_effect_done(ticket, "notification")
        elif self.close_ledger:
            self.close_ledger.mark_effect_done(ticket, "notification")
        if self.close_ledger and self.close_ledger.effects_complete(ticket):
            self.close_ledger.mark_accounted(ticket)

    async def refresh_from_mt5(self) -> tuple[dict, list[dict]]:
        """Refresh the shared snapshot without bypassing close reconciliation."""
        async with self._refresh_lock:
            account = await asyncio.to_thread(self.bridge.get_account_info)
            previous_positions = list(self.state.open_positions)
            positions = await asyncio.to_thread(self.bridge.get_positions)
            await self._reconcile_broker_closures(previous_positions, positions)
            self.state.open_positions = positions
            return account, positions

    async def _apply_trailing_stop(self, pos: dict):
        """
        At 65% progress to TP, lock 35% of the original TP distance.
        The original TP remains unchanged.
        """
        cfg = self.settings.get("trailing_stop", {})
        if not cfg.get("enabled", True):
            return

        ticket    = pos.get("ticket")
        pair      = pos.get("pair", pos.get("symbol", ""))
        direction = pos.get("direction", pos.get("type", ""))
        open_px   = float(pos.get("open_price", pos.get("openPrice", 0)))
        sl        = float(pos.get("sl", 0))
        tp        = float(pos.get("tp", 0))
        profit    = float(pos.get("profit", 0))

        if not ticket or not open_px or not sl or not tp:
            return
        if sl == 0 or tp == 0:
            return

        # Get current price from live tick
        try:
            tick    = await asyncio.to_thread(self.bridge.get_tick, pair)
            current = tick["bid"] if direction == "BUY" else tick["ask"]
        except Exception:
            return

        pip = _pip_size(pair)
        activation_pct = cfg.get("activation_at_pct", 65) / 100
        lock_pct = cfg.get("lock_profit_at_pct", 35) / 100

        if direction == "BUY":
            tp_dist      = tp - open_px
            if tp_dist <= 0:
                return
            progress     = (current - open_px) / tp_dist  # 0.0 – 1.0+
            new_sl       = sl

            if progress >= activation_pct - 1e-9 and ticket not in self._trail_locked_set:
                new_sl = round(open_px + lock_pct * tp_dist, 5)

            if new_sl > sl + pip:
                result = await self.execution.modify_position(ticket, new_sl, tp)
                if result.get("success"):
                    self._trail_locked_set.add(ticket)
                    if self.notify:
                        await self.notify(
                            f"🔒 *Profit lock activated (65% → 35%)*\n"
                            f"`{pair}` #{ticket}\n"
                            f"New SL: `{new_sl:.5f}` | TP unchanged: `{tp:.5f}`"
                        )

        else:  # SELL
            tp_dist  = open_px - tp
            if tp_dist <= 0:
                return
            progress = (open_px - current) / tp_dist
            new_sl   = sl

            if progress >= activation_pct - 1e-9 and ticket not in self._trail_locked_set:
                new_sl = round(open_px - lock_pct * tp_dist, 5)

            if new_sl < sl - pip:
                result = await self.execution.modify_position(ticket, new_sl, tp)
                if result.get("success"):
                    self._trail_locked_set.add(ticket)
                    if self.notify:
                        await self.notify(
                            f"🔒 *Profit lock activated (65% → 35%)*\n"
                            f"`{pair}` #{ticket}\n"
                            f"New SL: `{new_sl:.5f}` | TP unchanged: `{tp:.5f}`"
                        )

    async def check_once(self) -> dict:
        account, positions = await self.refresh_from_mt5()
        entry_permission = (
            self.entry_guard.evaluate(self.state, account) if self.entry_guard else None
        )

        balance          = account.get("balance", 0)
        equity           = account.get("equity", 0)
        unrealised_pnl   = equity - balance
        basket_profit    = sum(float(p.get("profit", 0) or 0) for p in positions)
        floating_drawdown = min(unrealised_pnl, 0)
        realised_loss    = self.state.daily_pnl_usd
        total_exposure   = realised_loss + floating_drawdown

        summary = {
            "balance": balance, "equity": equity,
            "open_positions": len(positions),
            "unrealised_pnl": unrealised_pnl,
            "basket_profit": basket_profit,
            "daily_pnl": realised_loss,
            "total_exposure": total_exposure,
            "entry_lock_reason": self.state.entry_block_reason,
        }

        if entry_permission and entry_permission.changed and self.notify:
            if entry_permission.allowed:
                message = "✅ *New Entries Open*\nDubai trading window is active."
            else:
                message = f"🛑 *New Entries Locked*\n{entry_permission.reason}\nOpen positions remain managed."
            await self.notify(message)

        # ── Session-profit realization ─────────────────────────────
        # A reached daily target locks entries in TradingGuard. When there is
        # an active profitable basket, realise it once instead of risking the
        # entire session milestone during a reversal.
        guard_cfg = self.settings.get("trading_guard", {}) or {}
        session_target = float(guard_cfg.get("daily_profit_limit_pct", 5))
        realize_session_basket = bool(
            guard_cfg.get("realize_basket_on_session_profit_target", True)
        )
        if (
            positions
            and realize_session_basket
            and state_is_session_profit_locked(self.state, session_target)
            and basket_profit > 0
            and not self.state.session_profit_basket_close_attempted
            and not self.state.basket_close_in_progress
            and not (
                self.close_ledger and self.close_ledger.has_pending_reason("session_profit")
            )
        ):
            self.state.session_profit_basket_close_requested = True
            self.state.basket_close_in_progress = True
            try:
                results = await self.execution.close_all(
                    self.state, close_reason="session_profit"
                )
                successful = [result for result in results if result.get("success")]
                if self.notify:
                    await self.notify(
                        f"🎯 *Dubai Session Profit Target Realized*\n"
                        f"Session equity change: `+{self.state.session_equity_pnl_pct:.2f}%`\n"
                        f"Close requests accepted: `{len(successful)}/{len(results)}`\n"
                        "Broker-confirmed results will follow. New entries remain locked "
                        "until the next Dubai session."
                    )
            finally:
                self.state.basket_close_in_progress = False

        if (
            self.state.session_profit_basket_close_requested
            and not positions
            and not (self.close_ledger and self.close_ledger.has_pending_reason("session_profit"))
        ):
            self.state.session_profit_basket_close_attempted = True

        # ── Basket profit protection ────────────────────────────────
        # Close all positions once the combined floating result reaches
        # the configured target, then let the next scan find fresh setups.
        basket_target = float(self.settings.get("basket_take_profit_usd", 0) or 0)
        if (
            positions
            and basket_target > 0
            and basket_profit >= basket_target
            and not self.state.basket_close_in_progress
            and not state_is_session_profit_locked(self.state, session_target)
        ):
            print(
                f"[Monitor] ✅ Basket target reached: "
                f"${basket_profit:.2f} >= ${basket_target:.2f}"
            )
            self.state.basket_close_in_progress = True
            try:
                results = await self.execution.close_all(self.state, close_reason="basket_profit")
                successful = [r for r in results if r.get("success")]
                failed = [r for r in results if not r.get("success")]
                if self.notify:
                    status = "All positions closed." if not failed else (
                        f"{len(failed)} position(s) could not be closed."
                    )
                    await self.notify(
                        f"💰 *Basket Profit Target Reached*\n"
                        f"Floating basket P&L: `+${basket_profit:.2f}`\n"
                        f"Close requests accepted: `{len(successful)}/{len(results)}`\n"
                        f"{status} Broker-confirmed results will follow.\n"
                        f"Fresh scan will seek the next setup."
                    )
            finally:
                self.state.basket_close_in_progress = False

        # ── Trailing stop for each open position ───────────────────
        if self.state.trading_active:
            for pos in positions:
                try:
                    await self._apply_trailing_stop(pos)
                except Exception as e:
                    print(f"[Monitor] Trailing stop error: {e}")

        if self.reporter:
            await self.reporter.check_and_send_reports(self.state)

        return summary

    async def run_loop(self):
        interval     = self.settings.monitor_interval_seconds
        self._running = True
        print(f"[Monitor] Started (interval={interval}s) | trailing_stop={self.settings.get('trailing_stop', {}).get('enabled', True)}")
        while self._running:
            try:
                await self.check_once()
            except Exception as e:
                print(f"[Monitor] Error: {e}")
            await asyncio.sleep(interval)

    def stop(self):
        self._running = False


def state_is_session_profit_locked(state, target_pct: float) -> bool:
    """Treat the persistent guard lock as authoritative across restarts."""
    return (
        state.session_equity_pnl_pct >= target_pct
        and "profit limit" in (state.daily_entry_lock_reason or "").lower()
    )

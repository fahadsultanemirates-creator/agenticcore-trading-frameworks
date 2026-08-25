"""
Manager — Tier 2 Orchestrator
Runs the full trading cycle: market data → TA (MTF) → session intelligence
→ news filter → signal generation (with session + memory boosts)
→ risk management → execution → trailing stop → reporting → state write.
"""
import asyncio
from datetime import datetime
from core.state import PendingTrade
from core.state_writer import write_state_async
from core.trading_guards import TradingGuard


class Manager:
    def __init__(self, bridge, state, settings,
                 market_data, ta_agent, session_agent, news_agent,
                 signal_agent, risk_agent, execution, memory_agent,
                 monitor, backtester, optimizer, reporter, entry_guard=None):
        self.bridge       = bridge
        self.state        = state
        self.settings     = settings
        self.market_data  = market_data
        self.ta           = ta_agent
        self.session      = session_agent
        self.news         = news_agent
        self.signal       = signal_agent
        self.risk         = risk_agent
        self.execution    = execution
        self.memory       = memory_agent
        self.monitor      = monitor
        self.backtester   = backtester
        self.optimizer    = optimizer
        self.reporter     = reporter
        self.entry_guard  = entry_guard or TradingGuard(settings)
        self._running     = False

    async def entry_permission(self, account=None, notify: bool = False):
        """Return current entry permission without affecting open positions."""
        if account is None:
            account = await asyncio.to_thread(self.bridge.get_account_info)
        decision = self.entry_guard.evaluate(self.state, account)
        if notify and decision.changed and self.reporter.notify:
            if decision.allowed:
                message = "✅ *New Entries Open*\nDubai trading window is active."
            else:
                message = f"🛑 *New Entries Locked*\n{decision.reason}\nOpen positions remain managed."
            await self.reporter.notify(message)
        return decision

    async def _trading_cycle(self):
        self.state.scan_count += 1
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        self.state.last_scan_time = now
        print(f"\n{'='*60}")
        print(f"[Manager] Scan #{self.state.scan_count} — {now}")
        print(f"[Manager] Mode: {self.state.mode.upper()} | Active: {self.state.trading_active}")
        print(f"{'='*60}")

        # ── 1. Market data ─────────────────────────────────────────
        ohlcv_data = await self.market_data.run()

        # ── 2. Technical analysis (multi-timeframe) ────────────────
        ta_data = await self.ta.run(ohlcv_data)

        # ── 3. Session intelligence ────────────────────────────────
        session_result = self.session.run()
        self.state.active_sessions = session_result.active_sessions
        self.state.session_bonus   = session_result.confidence_modifier

        if session_result.recommendation == "AVOID":
            print(f"[Manager] ⚠️  Dead zone — session penalty {session_result.confidence_modifier:+d} applied")

        # ── 4. News filter ─────────────────────────────────────────
        news_result   = await self.news.run()
        blocked_pairs = news_result.get("blocked_pairs", [])

        # ── 5. Trade memory adjustments ───────────────────────────
        memory_adj = self.memory.get_adjustments() if self.memory else {}

        # ── 6. Signal generation (with all Tier 2 boosts) ─────────
        signals = await self.signal.run(
            ta_data,
            blocked_pairs,
            session_bonus=session_result.confidence_modifier,
            memory_adjustments=memory_adj,
        )

        # ── 7. Account info + open positions ──────────────────────
        account, positions = await self.monitor.refresh_from_mt5()
        entry_permission = await self.entry_permission(account, notify=True)

        # ── 8. Write state.json for dashboard ─────────────────────
        await write_state_async(self.state, self.bridge, self.settings)

        if not self.state.trading_active or self.state.circuit_breaker_active or not entry_permission.allowed:
            if not entry_permission.allowed:
                self.state.pending_trade = None
                print(f"[Manager] New entries locked — {entry_permission.reason}")
            else:
                print("[Manager] Trading paused — skipping execution")
            return

        # ── 9. Risk management ────────────────────────────────────
        approved_trades = self.risk.run(signals, account, positions, self.state)

        if not approved_trades:
            print("[Manager] No trades approved this cycle")
            return

        # ── 10. Execute by mode ────────────────────────────────────
        mode = self.state.mode

        if mode == "signal":
            for t in approved_trades:
                msg = (
                    f"📡 *Signal — AgenticCore Forex PLUS*\n"
                    f"{t['direction']} `{t['pair']}` | conf=`{t['confidence']:.0f}%`\n"
                    f"SL=`${t['sl_usd']:.0f}` | TP=`${t['tp_usd']:.0f}` | Lot=`{t['lot']}`\n"
                    f"_{t.get('summary','')[:120]}_"
                )
                if self.reporter.notify:
                    await self.reporter.notify(msg)
            print(f"[Manager] Signal mode — sent {len(approved_trades)} signal(s)")

        elif mode == "semi":
            if self.state.pending_trade:
                print("[Manager] Semi-auto: pending approval waiting — skipping new signals")
            else:
                best = approved_trades[0]
                self.state.pending_trade = PendingTrade(
                    pair=best["pair"], direction=best["direction"],
                    confidence=best["confidence"],
                    lot_size=best["lot"],
                    sl_pips=best["sl_pips"], tp_pips=best["tp_pips"],
                    signal_summary=best["summary"],
                )
                msg = (
                    f"⚡ *Trade Signal — Awaiting Approval*\n"
                    f"{best['direction']} `{best['pair']}` | conf=`{best['confidence']:.0f}%`\n"
                    f"Lot: `{best['lot']}` | SL: `${best['sl_usd']:.0f}` | "
                    f"TP: `${best['tp_usd']:.0f}`\n\n"
                    f"_{best['summary'][:120]}_\n\n"
                    f"Reply /approve or /reject"
                )
                if self.reporter.notify:
                    await self.reporter.notify(msg)

        else:  # auto
            results = await self.execution.execute_all(approved_trades, self.state)
            for result in results:
                if result.get("success"):
                    matching = next((t for t in approved_trades if t["pair"] == result.get("pair")), {})
                    await self.reporter.log_trade(result, matching.get("summary", ""))

        # ── 11. Final state write ──────────────────────────────────
        await write_state_async(self.state, self.bridge, self.settings)

    async def run_loop(self):
        interval     = self.settings.scan_interval_minutes * 60
        self._running = True
        print(f"[Manager] Trading loop started — scan every {self.settings.scan_interval_minutes} min")
        try:
            await self._trading_cycle()
        except Exception as e:
            print(f"[Manager] Cycle error: {e}")
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._trading_cycle()
            except Exception as e:
                print(f"[Manager] Cycle error: {e}")

    def stop(self):
        self._running = False

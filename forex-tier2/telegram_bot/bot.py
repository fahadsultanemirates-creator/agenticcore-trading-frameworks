"""
AgenticCore Forex PLUS — Telegram Bot (Tier 2)
All Tier 1 commands plus new Tier 2 commands:
  /session  — show current active trading sessions
  /memory   — show per-pair win-rate performance from trade memory
  /broker   — show current broker profile

Setup:
  1. Create bot via @BotFather → FOREX_TELEGRAM_BOT_TOKEN env var
  2. Send /start → copy your Chat ID → FOREX_ADMIN_CHAT_ID env var
  3. Send /help for full command list
"""
import asyncio
import logging
from pathlib import Path
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, Application
)
from telegram.constants import ParseMode
from core.trading_guards import position_capacity_block_reason
from telegram_bot.notification_outbox import NotificationOutbox

logger = logging.getLogger(__name__)


def _admin_only(fn):
    async def wrapper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        admin_id = str(ctx.bot_data.get("admin_chat_id", ""))
        user_id  = str(update.effective_chat.id)
        if admin_id and user_id != admin_id:
            await update.message.reply_text("⛔ Unauthorised.")
            return
        return await fn(self, update, ctx)
    return wrapper


class ForexPlusBot:
    def __init__(self, manager, state, settings, memory_agent=None):
        self.manager  = manager
        self.state    = state
        self.settings = settings
        self.memory   = memory_agent
        self._app: Application | None = None
        self.outbox = NotificationOutbox(
            Path(__file__).resolve().parent.parent / "logs" / "telegram_outbox.json"
        )

    # ── Notification helper ────────────────────────────────────────
    async def notify(self, message: str):
        """Persist immediately; the outbox delivers independently of trading."""
        queued = self.outbox.enqueue(message)
        if not queued:
            logger.debug("Telegram notification already queued or delivered.")

    async def _send_outbox_message(self, message: str):
        if not self._app:
            raise RuntimeError("Telegram application is not ready")
        admin_id = self.settings.telegram.get("admin_chat_id", "")
        if not admin_id:
            raise RuntimeError("FOREX_PLUS_ADMIN_CHAT_ID is not configured")
        try:
            await self._app.bot.send_message(
                chat_id=admin_id, text=message, parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning("Telegram Markdown send failed, retrying as plain text: %s", e)
            await self._app.bot.send_message(chat_id=admin_id, text=message)

    # ── Command Handlers ───────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        await update.message.reply_text(
            f"👋 *AgenticCore Forex PLUS* — Tier 2 Framework\n\n"
            f"Your Chat ID: `{chat_id}`\n"
            f"Set `FOREX_ADMIN_CHAT_ID={chat_id}` to authorise commands.\n\n"
            f"Send /help for the full command list.",
            parse_mode=ParseMode.MARKDOWN
        )

    @_admin_only
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "*AgenticCore Forex PLUS — Commands*\n\n"
            "📊 *Status & Reports*\n"
            "/status — current state, P&L, sessions, open positions\n"
            "/report — today's P&L report\n"
            "/report week — this week's report\n"
            "/report all — all-time report\n\n"
            "🧠 *Tier 2 Intelligence*\n"
            "/session — active trading sessions + bonus/penalty\n"
            "/memory — per-pair win-rate performance history\n"
            "/broker — current broker profile (FBS / JustMarkets)\n\n"
            "⚙️ *Control*\n"
            "/stop — pause all trading\n"
            "/resume — resume trading\n"
            "/closeall — emergency: close all positions\n"
            "/mode auto|semi|signal — switch trading mode\n"
            "/reload — reload config.yaml without restart\n\n"
            "🎯 *Settings*\n"
            "/settp 40 — set take-profit in USD\n"
            "/setsl 15 — set stop-loss in USD\n"
            "/setrisk 1.0 — set risk % per trade\n"
            "/maxpositions — show fixed safety capacity\n"
            "/setconfidence 65 — set min signal confidence\n"
            "/pairs EURUSD GBPUSD — set active pairs\n\n"
            "📈 *Analysis*\n"
            "/backtest EURUSD — run backtest on a pair\n"
            "/optimize EURUSD — run parameter optimiser\n\n"
            "✅ *Approvals (semi-auto mode)*\n"
            "/approve — approve pending trade\n"
            "/reject — reject pending trade",
            parse_mode=ParseMode.MARKDOWN
        )

    @_admin_only
    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            account, positions = await self.manager.monitor.refresh_from_mt5()
            lines = [
                self.state.status_summary(),
                "\n*Live MT5 Account*",
                f"Balance: `${account.get('balance', 0):.2f}` | Equity: `${account.get('equity', 0):.2f}`",
                f"Free margin: `${account.get('free_margin', 0):.2f}`",
            ]
            if positions:
                lines.append("\n*Open positions*")
                for position in positions[:10]:
                    lines.append(
                        f"• `{position.get('pair')}` {position.get('direction')} "
                        f"#{position.get('ticket')} | P&L: `${position.get('profit', 0):+.2f}`"
                    )
            else:
                lines.append("\nNo open MT5 positions.")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        except Exception as error:
            await update.message.reply_text(f"❌ Could not refresh MT5 status: {error}")

    @_admin_only
    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.state.trading_active = False
        await update.message.reply_text("⏸ Trading paused. Send /resume to restart.")

    @_admin_only
    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.state.trading_active        = True
        self.state.circuit_breaker_active = False
        await update.message.reply_text("▶️ Trading resumed.")

    @_admin_only
    async def cmd_closeall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await self.manager.monitor.refresh_from_mt5()
        except Exception as error:
            await update.message.reply_text(f"❌ Could not refresh MT5 positions: {error}")
            return
        await update.message.reply_text("⛔ Closing all positions...")
        results = await self.manager.execution.close_all(self.state)
        closed  = sum(1 for r in results if r.get("success"))
        await update.message.reply_text(f"✅ Closed {closed}/{len(results)} positions.")

    @_admin_only
    async def cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if not args or args[0] not in ("auto", "semi", "signal"):
            await update.message.reply_text("Usage: /mode auto | semi | signal")
            return
        self.state.mode = args[0]
        await update.message.reply_text(
            f"✅ Mode set to `{args[0].upper()}`", parse_mode=ParseMode.MARKDOWN
        )

    @_admin_only
    async def cmd_settp(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            val = float(ctx.args[0])
            self.settings._cfg["default_tp_usd"] = val
            await update.message.reply_text(f"✅ TP set to `${val}`", parse_mode=ParseMode.MARKDOWN)
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /settp 40")

    @_admin_only
    async def cmd_setsl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            val = float(ctx.args[0])
            self.settings._cfg["default_sl_usd"] = val
            await update.message.reply_text(f"✅ SL set to `${val}`", parse_mode=ParseMode.MARKDOWN)
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /setsl 15")

    @_admin_only
    async def cmd_setrisk(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            val = float(ctx.args[0])
            if val <= 0 or val > 10:
                raise ValueError
            self.settings._cfg["risk_per_trade_pct"] = val
            await update.message.reply_text(f"✅ Risk set to `{val}%`", parse_mode=ParseMode.MARKDOWN)
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /setrisk 1.0  (0.1–10)")

    @_admin_only
    async def cmd_setconfidence(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            val = int(ctx.args[0])
            if val < 0 or val > 100:
                raise ValueError
            self.settings._cfg["min_signal_confidence"] = val
            await update.message.reply_text(
                f"✅ Min confidence set to `{val}%`", parse_mode=ParseMode.MARKDOWN
            )
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /setconfidence 65  (0–100)")

    @_admin_only
    async def cmd_maxpositions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🔒 Position capacity is fixed for safety: `5` normal positions, "
            "plus `2` reserved for signals strictly above `80%` confidence.",
            parse_mode=ParseMode.MARKDOWN,
        )

    @_admin_only
    async def cmd_pairs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            pairs = self.settings.pairs_with_suffix
            await update.message.reply_text(
                f"Active pairs: `{', '.join(pairs)}`", parse_mode=ParseMode.MARKDOWN
            )
            return
        # Store without suffix — settings.apply_suffix adds it
        self.settings._cfg["pairs"] = [p.upper() for p in ctx.args]
        await update.message.reply_text(
            f"✅ Pairs set to: `{', '.join(ctx.args)}`", parse_mode=ParseMode.MARKDOWN
        )

    @_admin_only
    async def cmd_reload(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self.settings.reload()
        await update.message.reply_text("✅ config.yaml reloaded.")

    @_admin_only
    async def cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        period = ctx.args[0] if ctx.args else "today"
        try:
            await self.manager.monitor.refresh_from_mt5()
        except Exception:
            pass
        msg    = await self.manager.reporter.get_report_message(period, self.state)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # ── Tier 2 commands ─────────────────────────────────────────────

    @_admin_only
    async def cmd_session(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show current active sessions and confidence modifier."""
        result = self.manager.session.run()
        sessions = ", ".join(result.active_sessions) if result.active_sessions else "None (dead zone)"
        overlap  = f" — *{result.overlap_label}* overlap" if result.overlap_label else ""
        await update.message.reply_text(
            f"🌍 *Active Sessions*{overlap}\n"
            f"Sessions: `{sessions}`\n"
            f"Confidence modifier: `{result.confidence_modifier:+d}` pts\n"
            f"Recommendation: `{result.recommendation}`",
            parse_mode=ParseMode.MARKDOWN
        )

    @_admin_only
    async def cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show trade memory per-pair win-rate stats."""
        if not self.memory:
            await update.message.reply_text("Trade memory is not enabled.")
            return
        msg = self.memory.get_summary()
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @_admin_only
    async def cmd_broker(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show current broker profile."""
        broker  = self.settings.broker
        pairs   = self.settings.pairs_with_suffix
        filling = broker.get("filling_mode", "auto")
        suffix  = broker.get("symbol_suffix", "") or "(none)"
        await update.message.reply_text(
            f"🏦 *Broker Profile*\n"
            f"Name: `{broker.get('name', 'Unknown')}`\n"
            f"Symbol suffix: `{suffix}`\n"
            f"Filling mode: `{filling}`\n"
            f"Active pairs: `{', '.join(pairs)}`",
            parse_mode=ParseMode.MARKDOWN
        )

    # ── Analysis commands ───────────────────────────────────────────

    @_admin_only
    async def cmd_backtest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pair = self.settings.apply_suffix(ctx.args[0].upper()) if ctx.args else self.settings.pairs_with_suffix[0]
        await update.message.reply_text(f"⏳ Running backtest for {pair}...")
        result = await self.manager.backtester.run(pair=pair)
        if "error" in result:
            await update.message.reply_text(f"❌ {result['error']}")
            return
        msg = (
            f"*Backtest — {pair}*\n"
            f"Period: `{result['lookback_days']} days`\n"
            f"Trades: `{result['total_trades']}`\n"
            f"Win Rate: `{result['win_rate_pct']}%`\n"
            f"Net P&L: `${result['total_profit_usd']}`\n"
            f"Profit Factor: `{result['profit_factor']}`\n"
            f"Max Drawdown: `{result['max_drawdown_pct']}%`\n"
            f"Sharpe: `{result['sharpe_ratio']}`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    @_admin_only
    async def cmd_optimize(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pair = self.settings.apply_suffix(ctx.args[0].upper()) if ctx.args else self.settings.pairs_with_suffix[0]
        await update.message.reply_text(f"⏳ Optimising {pair}... (takes ~1 min)")
        result = await self.manager.optimizer.run(pair=pair)
        if "error" in result:
            await update.message.reply_text(f"❌ {result['error']}")
            return
        p = result["best_params"]
        m = result["best_metrics"]
        msg = (
            f"*Optimizer — {pair}*\n"
            f"Tested `{result['combinations_tested']}` combinations\n\n"
            f"*Best Parameters:*\n"
            f"SL: `${p.get('sl_usd')}` | TP: `${p.get('tp_usd')}`\n\n"
            f"*Best Metrics:*\n"
            f"Win Rate: `{m.get('win_rate_pct')}%` | P&L: `${m.get('total_profit_usd')}`\n"
            f"Profit Factor: `{m.get('profit_factor')}`\n\n"
            f"✅ Config updated with best SL/TP."
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # ── Semi-auto approvals ─────────────────────────────────────────

    @_admin_only
    async def cmd_approve(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if self.state.pending_trade is None:
            await update.message.reply_text("No pending trade.")
            return
        entry_permission = await self.manager.entry_permission()
        if not entry_permission.allowed:
            self.state.pending_trade = None
            await update.message.reply_text(
                f"🛑 New trade not placed: {entry_permission.reason}. "
                "Open positions remain managed."
            )
            return
        trade = self.state.pending_trade
        positions = await asyncio.to_thread(self.manager.bridge.get_positions)
        capacity_reason = position_capacity_block_reason(
            self.settings, positions, trade.pair, trade.confidence
        )
        if capacity_reason:
            self.state.pending_trade = None
            await update.message.reply_text(f"🛑 New trade not placed: {capacity_reason}.")
            return
        self.state.pending_trade = None
        result = await self.manager.execution.execute_trade({
            "pair":       trade.pair,
            "direction":  trade.direction,
            "lot":        trade.lot_size,
            "sl_pips":    trade.sl_pips,
            "tp_pips":    trade.tp_pips,
            "confidence": trade.confidence,
            "summary":    trade.signal_summary,
        })
        if result.get("success"):
            await update.message.reply_text(
                f"✅ Executed: {trade.direction} {trade.pair} @ ticket #{result.get('ticket')}"
            )
        else:
            await update.message.reply_text(f"❌ Execution failed: {result.get('error')}")

    @_admin_only
    async def cmd_reject(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if self.state.pending_trade is None:
            await update.message.reply_text("No pending trade.")
            return
        pair = self.state.pending_trade.pair
        self.state.pending_trade = None
        await update.message.reply_text(f"🚫 Trade rejected: {pair}")

    # ── Bot lifecycle ───────────────────────────────────────────────

    def build(self) -> Application:
        token = self.settings.telegram.get("token", "")
        if not token:
            raise ValueError("FOREX_TELEGRAM_BOT_TOKEN not set. Create a bot via @BotFather.")

        admin_id = self.settings.telegram.get("admin_chat_id", "")
        app      = ApplicationBuilder().token(token).build()
        app.bot_data["admin_chat_id"] = admin_id
        self._app = app

        cmds = [
            ("start",          self.cmd_start),
            ("help",           self.cmd_help),
            ("status",         self.cmd_status),
            ("stop",           self.cmd_stop),
            ("resume",         self.cmd_resume),
            ("closeall",       self.cmd_closeall),
            ("mode",           self.cmd_mode),
            ("settp",          self.cmd_settp),
            ("setsl",          self.cmd_setsl),
            ("setrisk",        self.cmd_setrisk),
            ("setconfidence",  self.cmd_setconfidence),
            ("maxpositions",   self.cmd_maxpositions),
            ("pairs",          self.cmd_pairs),
            ("reload",         self.cmd_reload),
            ("report",         self.cmd_report),
            ("session",        self.cmd_session),
            ("memory",         self.cmd_memory),
            ("broker",         self.cmd_broker),
            ("backtest",       self.cmd_backtest),
            ("optimize",       self.cmd_optimize),
            ("approve",        self.cmd_approve),
            ("reject",         self.cmd_reject),
        ]
        for name, handler in cmds:
            app.add_handler(CommandHandler(name, handler))
        return app

    async def run(self):
        from telegram.error import Conflict
        app = self.build()
        print("[Bot] Telegram bot starting (polling)...")
        await app.initialize()
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("[Bot] Webhook cleared.")
        except Exception as e:
            print(f"[Bot] Webhook clear skipped: {e}")

        for attempt in range(10):
            try:
                await asyncio.sleep(5)
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                await self.outbox.start(self._send_outbox_message)
                print("[Bot] ✅ Telegram bot online — AgenticCore Forex PLUS")
                await self.notify("🟢 *AgenticCore Forex PLUS Telegram online*\nMT5 activity, live commands, and the 23:45 Dubai summary are enabled.")
                await asyncio.Event().wait()
                break
            except Conflict:
                print(f"[Bot] ⚠️ Conflict (attempt {attempt+1}/10) — waiting 15s...")
                try:
                    await app.updater.stop()
                    await app.stop()
                except Exception:
                    pass
                await asyncio.sleep(15)
            except Exception as e:
                print(f"[Bot] Error: {e}")
                raise

    async def stop(self):
        await self.outbox.stop()
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass

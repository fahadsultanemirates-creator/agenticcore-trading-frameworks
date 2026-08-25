"""
AgenticCore Forex PLUS — Tier 2 Framework Entry Point
Run this on your laptop (dev) or Windows VPS (live):

  cd artifacts/agenticcore-forex
  python3 v2/main.py

Environment variables:
  FOREX_PLUS_TELEGRAM_BOT_TOKEN  — dedicated Tier 2 bot token
  FOREX_PLUS_ADMIN_CHAT_ID       — Telegram chat ID (send /start first)
  GEMINI_API_KEY            — for LLM-assisted signal validation
  MT5_LOGIN / MT5_PASSWORD / MT5_SERVER — when dev_mode: false

To switch to FBS broker, ensure in config.yaml:
  broker:
    name: "FBS"
    symbol_suffix: ""
   MT5_SERVER="FBS-Demo"   # or FBS-Real for live

For JustMarkets:
  broker:
    name: "JustMarkets"
    symbol_suffix: ".m"
   MT5_SERVER="JustMarkets-Demo2"
"""
import asyncio
import sys
import os

# ── Path setup ──────────────────────────────────────────────────────
# Allow running as: python3 v2/main.py  (from artifacts/agenticcore-forex/)
# or:               python3 main.py     (from inside v2/)
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

# ── Replit guard — prevents conflict with laptop instance ───────────
if os.environ.get("REPL_ID") and not os.environ.get("FOREX_ALLOW_REPLIT"):
    print("ℹ️  Running in Replit environment — Replit is dev/edit only.")
    print("   Set FOREX_ALLOW_REPLIT=1 to force-run (e.g. for testing).")
    print("   The live instance runs on your laptop or VPS.")
    sys.exit(0)

from config.settings import settings
from core.state import SharedState
from core.trading_guards import TradingGuard
from core.close_ledger import CloseReconciliationLedger
from mt5_bridge.bridge import get_bridge

from agents.market_data          import MarketDataAgent
from agents.technical_analysis   import TechnicalAnalysisAgent
from agents.session_intelligence import SessionIntelligenceAgent
from agents.news_sentiment       import NewsSentimentAgent
from agents.signal_generator     import SignalGeneratorAgent
from agents.risk_management      import RiskManagementAgent
from agents.trade_execution      import TradeExecutionAgent
from agents.trade_memory         import TradeMemoryAgent
from agents.portfolio_monitor    import PortfolioMonitorAgent
from agents.backtester           import BacktestingAgent
from agents.optimizer            import StrategyOptimizerAgent
from agents.reporter             import ReportingAgent
from core.manager                import Manager
from telegram_bot.bot            import ForexPlusBot


async def main():
    broker_name = settings.broker.get("name", "")
    suffix      = settings.broker.get("symbol_suffix", "")
    pairs       = settings.pairs_with_suffix

    print("=" * 65)
    print("  AgenticCore Forex PLUS — Tier 2 AI Trading Framework")
    print(f"  Broker: {broker_name}  |  Suffix: '{suffix}' | Mode: {'DEV' if settings.dev_mode else 'LIVE'}")
    print(f"  Pairs:  {', '.join(pairs)}")
    print(f"  Scan interval: {settings.scan_interval_minutes} min  |  Min confidence: {settings.min_signal_confidence}%")
    print(f"  MTF confluence: {'ON' if settings.mtf.get('enabled') else 'OFF'}  |  "
          f"Session intelligence: {'ON' if settings.session.get('enabled') else 'OFF'}  |  "
          f"Trade memory: {'ON' if settings.trade_memory.get('enabled') else 'OFF'}  |  "
          f"Trailing stop: {'ON' if settings.trailing_stop.get('enabled') else 'OFF'}")
    print("=" * 65)

    # ── Shared state ───────────────────────────────────────────────
    state      = SharedState()
    state.mode = settings.mode
    entry_guard = TradingGuard(settings)
    close_ledger = CloseReconciliationLedger()

    # ── MT5 bridge ─────────────────────────────────────────────────
    bridge = get_bridge(settings)

    # ── Agents ─────────────────────────────────────────────────────
    market_data   = MarketDataAgent(bridge, settings)
    ta_agent      = TechnicalAnalysisAgent(settings)
    session_agent = SessionIntelligenceAgent(settings)
    news_agent    = NewsSentimentAgent(settings)
    signal_agent  = SignalGeneratorAgent(settings)
    risk_agent    = RiskManagementAgent(settings)
    memory_agent  = TradeMemoryAgent(settings)
    reporter      = ReportingAgent(settings, notify_fn=None)

    # TradeExecutionAgent needs reporter and memory (wired after bot notify is set)
    execution     = TradeExecutionAgent(
        bridge, settings, notify_fn=None, memory_agent=memory_agent, reporter=reporter,
        close_ledger=close_ledger,
    )

    monitor       = PortfolioMonitorAgent(
        bridge, settings, execution, state, notify_fn=None, entry_guard=entry_guard,
        reporter=reporter, close_ledger=close_ledger,
    )
    backtester    = BacktestingAgent(bridge, settings)
    optimizer     = StrategyOptimizerAgent(bridge, settings)

    # ── Manager ────────────────────────────────────────────────────
    manager = Manager(
        bridge=bridge, state=state, settings=settings,
        market_data=market_data, ta_agent=ta_agent,
        session_agent=session_agent, news_agent=news_agent,
        signal_agent=signal_agent, risk_agent=risk_agent,
        execution=execution, memory_agent=memory_agent,
        monitor=monitor, backtester=backtester,
        optimizer=optimizer, reporter=reporter, entry_guard=entry_guard,
    )

    # ── Telegram bot ───────────────────────────────────────────────
    bot = ForexPlusBot(manager, state, settings, memory_agent=memory_agent)

    # Wire notify function into reporter, execution, monitor
    reporter.notify  = bot.notify
    execution.notify = bot.notify
    monitor.notify   = bot.notify

    # ── Launch tasks ───────────────────────────────────────────────
    telegram_token = settings.telegram.get("token", "")
    tasks = [
        asyncio.create_task(manager.run_loop(),  name="trading-loop"),
        asyncio.create_task(monitor.run_loop(),  name="portfolio-monitor"),
    ]
    if telegram_token:
        tasks.append(asyncio.create_task(bot.run(), name="telegram-bot"))
        print("[Main] Telegram bot task created.")
    else:
        print("[Main] ⚠ No FOREX_TELEGRAM_BOT_TOKEN set — Telegram bot disabled.")
        print("[Main]   Create a bot via @BotFather, then set FOREX_PLUS_TELEGRAM_BOT_TOKEN.")

    print("[Main] All systems online. Press Ctrl+C to stop.\n")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        print("\n[Main] Shutting down...")
    finally:
        manager.stop()
        monitor.stop()
        if telegram_token:
            await bot.stop()
        if hasattr(bridge, "shutdown"):
            bridge.shutdown()
        print("[Main] AgenticCore Forex PLUS stopped.")


if __name__ == "__main__":
    asyncio.run(main())

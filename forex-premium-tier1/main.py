"""
Premium Tier 1 — entry point.

Usage:
    cd artifacts/agenticcore-forex/premium
    python main.py                  # mock + signal-only (safe default)
    PREMIUM_MODE=signal python main.py   # real data, signal-only
    PREMIUM_MODE=demo PREMIUM_SIGNAL_ONLY=false python main.py   # demo (requires MT5)
"""
import logging
import os
import sys

# Add premium package to path
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import PremiumSettings
from runtime.manager import PremiumManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("premium.main")


def main():
    logger.info("AgenticCore Forex Premium Tier 1 starting...")
    settings = PremiumSettings.from_env()
    logger.info(
        f"Mode: {settings.mode} | Signal-only: {settings.signal_only} | "
        f"Worker: {settings.worker_name}"
    )

    manager = PremiumManager(settings)

    run_forever = os.environ.get("PREMIUM_RUN_FOREVER", "").lower() in ("1", "true", "yes")
    if run_forever:
        manager.run_loop()
        return

    result = manager.run_one_cycle()
    logger.info(f"Cycle complete: {result}")


if __name__ == "__main__":
    main()

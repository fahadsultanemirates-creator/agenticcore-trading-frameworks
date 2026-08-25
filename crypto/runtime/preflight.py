"""No-network readiness report for a laptop or Windows VPS handoff."""

from __future__ import annotations

import os
from typing import Any, Dict

from config.settings import CryptoSettings


def run_preflight(settings: CryptoSettings) -> Dict[str, Any]:
    """
    Verify local configuration without sending a trade, Telegram message, or API call.

    Secrets are intentionally represented only as configured/unavailable flags.
    """
    paths = {
        "runtime_dir": settings.runtime_dir,
        "log_dir": settings.log_dir,
        "memory_db_parent": os.path.dirname(settings.memory_db_path),
    }
    writable: Dict[str, bool] = {}
    for name, path in paths.items():
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".agenticcore_write_probe")
            with open(probe, "w", encoding="utf-8") as file:
                file.write("ok")
            os.remove(probe)
            writable[name] = True
        except OSError:
            writable[name] = False
    return {
        "ready": all(writable.values()) and settings.signal_mode,
        "mode": "signal_only",
        "live_execution": "not_implemented",
        "paper_trading_enabled": settings.paper_trading_enabled,
        "basket_profit_target_usdt": settings.basket_profit_target_usdt,
        "continuous_mode_configured": settings.run_forever,
        "paths_writable": writable,
        "telegram": {
            "configured": bool(settings.telegram_bot_token and settings.telegram_chat_id),
            "outbound_only": True,
            "reporting_schedule_dubai": {
                "daily": settings.telegram_daily_summary_time,
                "weekly_sunday": settings.telegram_weekly_summary_time,
                "monthly_last_day": settings.telegram_monthly_summary_time,
            },
        },
        "ai_commentary": {
            "enabled": settings.ai_explanations_enabled,
            "openai_key_configured": bool(settings.openai_api_key),
        },
        "coinmarketcap": {
            "key_configured": bool(
                os.environ.get("COINMARKETCAP_API_KEY", "").strip()
                or os.environ.get("CMC_API_KEY", "").strip()
            ),
            "optional": True,
        },
        "private_mexc_readonly": settings.private_readonly_enabled,
        "notes": [
            "No exchange order, leverage, cancellation, withdrawal, Telegram message, or AI request was sent.",
            "MEXC public prices remain authoritative for local paper-price handling.",
            "Owner reports are scheduled in Dubai time: daily 23:59, Sunday 23:30, and month-end 23:45.",
        ],
    }
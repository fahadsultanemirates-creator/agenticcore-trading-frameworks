"""
AgenticCore Forex — Premium Tier 1 Package

JustMarkets-first premium trading framework.
Default mode: mock + signal-only. No trades placed by default.

Environment variables:
  PREMIUM_MODE            mock (default) | signal | demo | auto
  PREMIUM_SIGNAL_ONLY     true (default; set false only with demo/auto)
  PREMIUM_WORKER_NAME     worker identifier
  PREMIUM_MT5_*           MT5 connection settings (see config/settings.py)
  PREMIUM_RISK_*          risk limits (see config/settings.py)
  PREMIUM_VOL_WINDOW      volume sense window (default 50)
  PREMIUM_WATCHLIST       comma-separated pairs
  PREMIUM_STATE_PATH      runtime state JSON path
  PREMIUM_AUDIT_PATH      audit JSONL path

Magic number: 20260101 (exclusive to Premium Tier 1)
"""
__version__ = "0.1.0"
__worker_id__ = "premium-tier1"

# AgenticCore Clean Project Export

Prepared: 25 August 2026

This archive contains the latest source export of three independent frameworks:

- `crypto/` - AgenticCore Crypto Standard Tier 1 for MEXC Futures
- `forex-tier2/` - AgenticCore Forex Plus Tier 2
- `forex-premium-tier1/` - AgenticCore Forex Premium Tier 1

## Included

- Python source code
- tests
- configuration templates
- setup and architecture documentation
- safe example environment files

## Intentionally excluded

- `.env` files and credential values
- Python `__pycache__` and compiled bytecode
- runtime state, paper-position state, and live-cycle state
- SQLite databases and audit/log data
- machine-specific caches
- deployment secrets

Copy the appropriate example environment file on the target machine and add
credentials through the target machine's protected environment mechanism. Never
place MEXC, MT5, Telegram, or LLM credentials in this archive or in GitHub.

## Source status at export

- Crypto: 201 tests passed in Replit
- Forex Tier 2: 24 tests passed in Replit
- Premium Forex Tier 1: 71 tests passed in Replit
- Forex Tier 2 and Premium compilation: passed

The VPS is a separate runtime copy. This export is the clean Replit source
release and does not claim that a VPS has been synchronized until its files are
verified against the manifest.

## Suggested entry points

```text
crypto/main.py
forex-tier2/main.py
forex-premium-tier1/main.py
```

Review each project's README and configuration before running it. Trading
frameworks should begin in signal, mock, paper, or demo mode and should not be
started in live mode without the required account, broker, exchange, and
operator checks.
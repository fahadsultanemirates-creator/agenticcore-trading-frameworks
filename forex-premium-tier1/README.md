# AgenticCore Forex — Premium Tier 1

JustMarkets-first premium trading worker.  
**Default mode: mock + signal-only. No trades can be placed by default.**

---

## Directory Layout

```
premium/
├── config/          # Settings loader; PREMIUM_* env vars
├── domain/          # Core models: Signal, ConfidenceBreakdown, WorkerState, etc.
├── adapters/mt5/    # MT5 bridge, mock bridge, and factory
├── analysis/        # Indicators, multi-TF pipeline, VolumeSense, confidence
├── risk/            # Deterministic entry guards
├── storage/         # Atomic state writer + JSONL audit logger
├── runtime/         # Manager / orchestrator
├── tests/           # pytest test suite
├── runtime/         # Runtime state JSON + durable session baseline
├── logs/            # Log files (auto-created)
├── main.py          # Entry point
└── README.md
```

---

## Environment Variables

**Never put credential values in code or version control.**  
Set these in a `.env` file or as system environment variables.

| Variable | Default | Description |
|---|---|---|
| `PREMIUM_MODE` | `mock` | `mock` \| `signal` \| `demo` \| `auto` |
| `PREMIUM_SIGNAL_ONLY` | `true` | Set `false` only with `demo`/`auto` when ready |
| `PREMIUM_WORKER_NAME` | `premium-tier1-worker-1` | Worker identifier |
| `PREMIUM_MT5_TERMINAL_PATH` | *(empty)* | Full path to `terminal64.exe` |
| `PREMIUM_MT5_LOGIN` | *(empty)* | MT5 account number |
| `PREMIUM_MT5_SERVER` | *(empty)* | MT5 server name (e.g. `JustMarkets-Demo`) |
| `PREMIUM_MT5_EXPECTED_BROKER` | *(empty)* | Expected broker name for identity check |
| `PREMIUM_MT5_ACCOUNT_TYPE` | `DEMO` | `DEMO` or `LIVE` |
| `PREMIUM_MT5_SYMBOL_SUFFIX` | *(empty)* | Broker symbol suffix (e.g. `.m`) |
| `PREMIUM_RISK_ENTRY_START` | `05:00` | Dubai time entry window start |
| `PREMIUM_RISK_ENTRY_STOP` | `23:29` | Dubai time entry window stop |
| `PREMIUM_RISK_MAX_POSITIONS` | `7` | Max total portfolio positions |
| `PREMIUM_RISK_MAX_PER_PAIR` | `3` | Max positions per pair |
| `PREMIUM_RISK_MAX_CURRENCY_EXPOSURE` | `3` | Max concurrent exposure per currency |
| `PREMIUM_RISK_MAX_CORRELATED_POSITIONS` | `2` | Max positions in a configured correlated asset group |
| `PREMIUM_RISK_DAILY_LOSS_PCT` | `15.0` | Daily loss lock (% of session equity) |
| `PREMIUM_RISK_DAILY_PROFIT_PCT` | `20.0` | Daily profit lock (% of session equity) |
| `PREMIUM_RISK_MAX_SPREAD_PIPS` | `5.0` | Max spread in pips |
| `PREMIUM_RISK_MAX_DATA_AGE_S` | `300.0` | Max data age in seconds |
| `PREMIUM_RISK_MIN_CONFIDENCE` | `60.0` | Minimum confidence for entry |
| `PREMIUM_RISK_GATE_FOREX_LOW_VOL` | `true` | Gate forex entries on LOW volume |
| `PREMIUM_RISK_GATE_METALS_LOW_VOL` | `false` | Gate metals on LOW volume (default off) |
| `PREMIUM_VOL_WINDOW` | `50` | VolumeSense trailing window (bars) |
| `PREMIUM_WATCHLIST` | *(9 pairs)* | Comma-separated pair list |
| `PREMIUM_STATE_PATH` | `premium/runtime/state.json` | State JSON output path |
| `PREMIUM_AUDIT_PATH` | `premium/logs/audit.jsonl` | Audit JSONL path |
| `PREMIUM_SCAN_INTERVAL_S` | `60.0` | Scan loop interval (seconds) |
| `PREMIUM_RUN_FOREVER` | `false` | Set `true` for the continuous scan loop |

---

## Mock / Signal / Demo Safety Progression

```
STEP 1 — MOCK (default, safe everywhere including Linux)
  PREMIUM_MODE=mock
  No MT5 connection. Synthetic data. No real orders. Tests pass here.

STEP 2 — SIGNAL (real data, no execution, Windows + MT5 required)
  PREMIUM_MODE=signal
  Connects to running MT5 terminal. Reads live data.
  Signals logged to audit JSONL. No orders placed.
  Identity still validated before connecting.

STEP 3 — DEMO DATA (real demo account data, still signal-only)
  PREMIUM_MODE=demo
   PREMIUM_SIGNAL_ONLY=true
  PREMIUM_MT5_TERMINAL_PATH=C:\path\to\terminal64.exe
  PREMIUM_MT5_LOGIN=<demo_account_number>
  PREMIUM_MT5_SERVER=JustMarkets-Demo
  PREMIUM_MT5_EXPECTED_BROKER=JustMarkets
  PREMIUM_MT5_ACCOUNT_TYPE=DEMO
   Identity validation and all risk guards remain active. This first runtime
   does not submit orders yet.

STEP 4 — DEMO EXECUTION (future phase)
   This requires a separately tested position-sizing and SL/TP execution module.
   It is intentionally unavailable in this first runtime.
```

---

## Windows VPS / Laptop Setup

### Prerequisites

1. Install Python 3.11+ from python.org
2. Install MetaTrader 5 terminal from JustMarkets
3. Log in to your MT5 account in the terminal and leave it running
4. Clone or copy this repository

### Installation

```powershell
# From the project root (agenticcore-forex/)
pip install -r requirements.txt

# Navigate to premium directory
cd artifacts\agenticcore-forex\premium
```

### Create a .env file (NEVER commit this)

```
PREMIUM_MODE=signal
PREMIUM_WORKER_NAME=justmarkets-worker-1
PREMIUM_MT5_TERMINAL_PATH=C:\Program Files\JustMarkets MetaTrader 5\terminal64.exe
PREMIUM_MT5_LOGIN=<your_account_number>
PREMIUM_MT5_SERVER=JustMarkets-Demo
PREMIUM_MT5_EXPECTED_BROKER=JustMarkets
PREMIUM_MT5_ACCOUNT_TYPE=DEMO
PREMIUM_MT5_SYMBOL_SUFFIX=
```

### Run in signal mode

```powershell
# Load .env if using python-dotenv, or set vars manually
python main.py
```

### Run tests (any platform, mock mode)

```bash
cd artifacts/agenticcore-forex/premium
python -m pytest tests/ -v
```

### Compile check (any platform)

```bash
python -m compileall . -q
```

---

## Architecture Summary

- **VolumeSense**: classifies completed-candle tick volume as LOW/NORMAL/HIGH using trailing median. Metals use volume as context; forex can be gated. No LLM in this path.
- **ConfidenceCalibrator**: 4-component bounded score (base_technical 40 + timeframe_agreement 25 + volume_participation 20 + context_quality 15 = 100 max). Policy: `v1_fixed_table`.
- **RiskGuard**: deterministic, fast, auditable. Checks: kill switch, stale data, spread, entry window, durable session baseline, daily P&L, pair/currency/correlation exposure, confidence, and volume gate.
- **PremiumManager**: orchestrates one scan or continuous loop. Writes atomic state JSON + JSONL audit. Never places orders in signal mode.
- **Magic number**: `20260101` (unique to Premium Tier 1; never shared with Tier 2).

---

## Current Limitations

- **No economic calendar / news provider**: High-impact event blackouts not yet implemented.
- **No Telegram execution control**: Commands (pause, resume, kill) not yet wired.
- **No lot-size calculation**: The execution path (demo/auto) is scaffolded but order sizing is not implemented in this slice.
- **No SL/TP calculation**: Requires lot-size + ATR-based policy (Phase 4).
- **No position management engine**: Open-trade monitoring (profit lock, invalidation exit) not yet implemented.
- **No persistent trade memory**: Signals are logged to JSONL; full trade-memory DB (PostgreSQL) is Phase 6.
- **No cross-asset context**: DXY, yields, gold correlation not yet integrated.
- **Independent confirmation is fail-closed**: 2nd/3rd same-pair entries remain blocked until a future module can provide verifiable independent confirmation.

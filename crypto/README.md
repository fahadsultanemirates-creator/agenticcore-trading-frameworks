# Crypto Standard Tier 1 – MEXC Futures Signal Framework

## What is implemented

- **Public MEXC Futures data adapter** (`adapters/mexc_public.py`): fetches contract details, 24h tickers, 15-minute/1-hour/4-hour OHLCV candles, funding rates, open interest, public order-book depth, and recent public trades. No API key required.
- **Universe scanner** (`analysis/scanner.py`): filters hundreds of USDT perpetual contracts down to a liquidity-qualified shortlist using spread, turnover, and activity thresholds, then deeply probes the strongest set.
- **Signal engine** (`analysis/signals.py`): deterministic quality scoring (0–100) based on completed 15-minute, 1-hour, and 4-hour candles, clustered multi-touch structure zones, false-breakout rejection, directional retest/reclaim confirmation, relative volume, funding, turnover/OI, spread, and public order-flow confirmation. Candidates stay visible as pending until a nearby entry zone confirms, then expire rather than becoming stale trade plans.
- **Risk planner** (`risk/sizing.py`, `risk/trade_levels.py`, `risk/profit_lock.py`): uses completed-candle ATR to set a bounded coin-specific stop, sizes contracts to the fixed $2 maximum loss, calculates the exact gross $3 target price and isolated-margin estimate, and plans a 65%-progress / 35%-protected-profit lock. It rejects any missing or invalid input — never guesses. The whole remaining paper basket closes when marked net profit reaches the fixed $5 basket target; if any open position lacks a current mark, this basket control does not trigger.
- **Orchestrator** (`runtime/orchestrator.py`): runs a full cycle, selects up to 5 candidates, and writes `runtime/state.json` + `logs/audit.jsonl` atomically.
- **Portfolio guard**: removes local paper plans that would exceed the basket risk cap or concentrate same-direction risk in highly correlated completed-candle returns.
- **Paper lifecycle** (`runtime/paper_positions.py`): optional, local-only entries/exits with conservative fee assumptions, targets, stops, 65%-progress / 35%-protected profit locks, and audit events. It does not call an exchange.
- **Telegram operator reporting** (`runtime/notifications.py`): optional outbound-only updates for worker startup, qualified signals, pending/blocked/expired setups, local paper openings, profit locks, complete close results, risk-guard transitions, heartbeats, and complete Dubai-time daily, weekly, and monthly paper reports. It does not accept commands or control MEXC.
- **Independent-market confirmation** (`adapters/market_context.py`, `analysis/cross_market.py`): public Binance Futures and Bybit flow/depth/funding/OI evidence is compared source-by-source for the deeply probed shortlist. Agreement can add at most 6 confidence points and disagreement can remove at most 6; it can never create a direction, retest, size, or live action. CoinGecko remains public market-cap/supply context; CoinMarketCap is optional when its secret is configured.
- **AI operator commentary** (`runtime/trade_intelligence.py`): OpenAI can explain every recorded paper lifecycle event and scheduled owner report from its recorded facts. It cannot select, size, change, close, or submit a trade. Missing keys, rate limits, and provider failures are stored as unavailable without disrupting the worker.
- **Private read-only adapter** (`adapters/mexc_private.py`): signed GET-only inspection for assets, positions, and open orders. It is disabled by default and cannot submit, cancel, or mutate anything.
- **State persistence** (`storage/state.py`): atomic write-rename JSON and append-only JSONL.
- **Dashboard API endpoint** (`/api/markets/futures-status`): reads `runtime/state.json` if present; returns a safe pending default if not.

## What is deliberately NOT implemented

- **No default private MEXC access** — private credentials are disabled by default and normal cycles do not read them.
- **No order submission** — no buy/sell/cancel calls anywhere in the codebase.
- **No leverage mutation** — leverage configuration is read-only.
- **No live execution path** — the private adapter has no order, cancellation, leverage, margin, or withdrawal method.
- **No cross margin** — isolated margin only, enforced at the code level.
- **No live execution path** — `signal_mode=True` is hardcoded and validated at startup.
- **No third-party packages** — Python standard library only.

---

## Windows PowerShell setup (VPS or laptop)

### Prerequisites

- Python 3.10 or later (`python --version`)
- Internet access to `contract.mexc.com` (public, no auth)

### Steps

```powershell
# 1. Clone the repo or copy the crypto folder to your machine
cd C:\agenticcore\artifacts\agenticcore-markets\crypto

# 2. Copy the example env file
Copy-Item .env.example .env

# 3. Edit .env if you want to change any defaults (optional)
notepad .env

# 3a. Verify local configuration first. This makes no network calls and prints
# only configured/unavailable secret status, never secret values.
python main.py --preflight

# 4. Run a single cycle (default mode)
python main.py

# 5. Run continuously
python main.py --forever
# Or set CRYPTO_RUN_FOREVER=true in .env and run: python main.py
```

### Output files

After a successful cycle:

```
runtime\state.json     ← full framework state (read by dashboard API)
runtime\paper_positions.json ← optional local paper records only
logs\audit.jsonl       ← append-only audit log
```

---

## Running tests

```powershell
cd C:\agenticcore\artifacts\agenticcore-markets\crypto
python -m pytest tests/ -v
```

Or with the standard library only:

```powershell
python -m unittest discover -s tests -v
```

All tests must pass. Tests never call the real network — all transport is dependency-injected.

---

## Signal / paper run

By default, `main.py` runs one cycle and exits:

```powershell
python main.py
```

To run every 60 seconds indefinitely:

```powershell
# Option A: command-line flag
python main.py --forever

# Option B: environment variable
$env:CRYPTO_RUN_FOREVER = "true"
python main.py
```

### Optional Telegram operator updates

Telegram is disabled unless both values are set in the local `.env` file:

```ini
CRYPTO_TELEGRAM_BOT_TOKEN=your_bot_token
CRYPTO_TELEGRAM_CHAT_ID=your_chat_id
CRYPTO_TELEGRAM_DAILY_SUMMARY_TIME=23:59
CRYPTO_TELEGRAM_WEEKLY_SUMMARY_TIME=23:30
CRYPTO_TELEGRAM_MONTHLY_SUMMARY_TIME=23:45
```

All report times use Dubai time and are fixed: a complete daily report at 23:59,
a weekly report every Sunday at 23:30, and a month-end report at 23:45 on the
last calendar day. Reports include each closed trade's entry/exit, result,
reason, fees, net P&L, current open-paper exposure, and guard state. Long
reports are delivered in retry-safe Telegram parts. All messages clearly
identify signals and trades as local paper-only; the worker has no Telegram
command handler and never sends a live MEXC order.

### OpenAI operator commentary

Copy the existing key to the laptop's local `.env` file only; do not commit or
send that file. When `OPENAI_API_KEY` is present, OpenAI commentary is enabled
automatically for paper lifecycle events and scheduled owner reports:

```ini
OPENAI_API_KEY=...
```

OpenAI receives a short redacted evidence packet and returns an operator
explanation. AI is never called for direction selection or risk sizing. If it
times out, the scan, local paper ledger, and deterministic report continue
normally with the provider marked unavailable.

### Optional CoinMarketCap metadata

CoinMarketCap is optional; public Binance/Bybit and CoinGecko still work
without it. If you have a CoinMarketCap credential, store it only in the local
`.env` file:

```ini
COINMARKETCAP_API_KEY=...
```

---

## Progressing to private account verification (future Tier 2)

When you are ready to connect a real MEXC account:

1. Create a MEXC key that has no withdrawal permission and restrict it to the laptop IP where available.
2. Store `MEXC_API_KEY` and `MEXC_API_SECRET` on the laptop only (never commit them).
3. Set `CRYPTO_PRIVATE_READONLY_ENABLED=true` and run the explicit check:
   ```powershell
   python main.py --account-check
   ```
   It reads assets, positions, and open orders through signed GET requests only. It is never called by a normal scan.
4. Keep `signal_mode` as the default. Any future live execution needs a separate, reviewed execution phase with testnet validation; this framework contains no such path.

---

## Architecture notes

```
crypto/
├── main.py                  entry point (one-shot or forever loop)
├── config/settings.py       env-driven config, validated at startup
├── domain/models.py         normalized data models, dashboard-safe JSON
├── adapters/mexc_public.py  public API client (DI transport, no network in tests)
├── analysis/scanner.py      universe filter + turnover ranking
├── analysis/signals.py      completed-candle quality gate + local plan sizing
├── analysis/quality.py      RVOL, support/resistance, retest, correlation math
├── risk/sizing.py           contract quantity calculator (no Forex lots)
├── runtime/orchestrator.py  public cycle runner → state.json + audit.jsonl
├── runtime/paper_positions.py  optional local paper lifecycle only
├── storage/state.py         atomic write helpers
└── tests/                   pure-stdlib tests, no network
```

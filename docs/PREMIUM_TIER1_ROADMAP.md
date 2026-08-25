# AgenticCore Forex Premium Tier 1 Roadmap

This is the living checklist for the next-generation premium trading framework.
The deployed Tier 2 remains the stable baseline and is not broadly refactored while
this system is being designed.

## Product boundary

- [ ] Keep current Tier 2 strategy and configuration stable
- [ ] Build Premium Tier 1 as a new generation on the Tier 2 foundation
- [ ] Keep research, backtesting, and paper trading isolated from live trading
- [ ] Define success metrics: expectancy, profit factor, drawdown, stability, and calibrated precision

## Execution and broker architecture

- [ ] Add explicit MT5 terminal-path selection
- [ ] Verify broker, server, account number, and account mode before trading
- [ ] Use a unique magic number and state namespace per strategy/account
- [ ] Support broker-specific symbols, suffixes, contract sizes, and filling modes
- [ ] Add spread, slippage, liquidity, and abnormal-tick protection
- [ ] Add completed-candle tick-volume features and asset-specific liquidity quality checks
- [ ] Make orders idempotent and reconcile broker results after every action
- [ ] Add disconnect, stale-data, and process-heartbeat protection
- [ ] Separate client accounts into isolated workers and credentials

## Market data and intelligence

- [ ] Keep MT5 as execution-price truth
- [ ] Add a reliable economic-calendar API
- [ ] Store expected, previous, and actual event values with timestamps
- [ ] Add high-impact event blackout and post-event stabilization rules
- [ ] Add geopolitical/news source classification and confidence decay
- [ ] Add cross-asset context: DXY, yields, oil, gold, volatility, and currency strength
- [ ] Add a second market-data source for anomaly and spread comparison
- [ ] Avoid website scraping and unsupported data redistribution

### External API decision matrix

The first implementation should use adapters, so providers can be replaced without
changing signal or risk logic.

| Capability | Initial role | Preferred starting direction | Required before live use |
|---|---|---|---|
| MT5 Python API | Execution, broker ticks, positions, history | Existing MetaTrader5 package | Terminal path, account identity, symbol/contract validation |
| Telegram Bot API | Commands, alerts, reports, emergency controls | Existing dedicated bot per worker | Polling conflict protection and chat authorization |
| LLM | Explanations, research, trade diary, approved recommendations | OpenAI primary; Gemini fallback/evaluation | Tool allow-list, deterministic risk boundary, cost/latency limits |
| Economic calendar | Forecast/previous/actual events and historical surprise | Evaluate Trading Economics or an equivalent licensed calendar API | Timestamps, timezone, event impact, rate limits, historical access |
| Financial news | Market and company/commodity headlines | Licensed finance-news provider | Source timestamp, entities, sentiment/event metadata, redistribution rights |
| Geopolitical discovery | Broad event detection and corroboration | GDELT or equivalent plus a licensed finance feed | Deduplication, source confidence, event severity, false-positive handling |
| Cross-asset data | DXY, yields, gold, silver, oil, volatility, currency strength | Evaluate Twelve Data, Polygon, or equivalent based on JustMarkets coverage | Real-time entitlement, symbol mapping, latency, cost, licensing |
| Macro history | Backtest context and long-run regime labels | FRED or equivalent public macro source | Publication timestamps and revision handling |
| Persistence | Trade memory, research data, client isolation | PostgreSQL for production; local files only for prototypes | Account/worker isolation, backups, retention, migrations |

The calendar, news, and secondary market-data providers are decisions to validate
with a small comparison test. We should not purchase or connect several providers
until response quality, coverage, latency, licensing, and price are compared.

### First-release API count

The first production design does not need dozens of services:

- **1 local trading interface:** MetaTrader 5 Python API
- **5 outside service interfaces:** Telegram, one LLM provider, one economic-calendar provider, one financial/news provider, and one secondary market-data provider
- **Optional additions:** GDELT-style geopolitical discovery, FRED-style macro history, and a second LLM for fallback/evaluation

The expected first-release cost split is:

- **Free or locally available:** MT5 Python interface, Telegram Bot API, and usually one public geopolitical/macro source
- **Likely usage-based or licensed:** OpenAI, a production economic calendar, a reliable financial-news feed, and real-time secondary market data

We will choose providers after a coverage/latency/licensing test. A free endpoint is not automatically suitable for live trading.

## Premium project structure

- [ ] Keep the current Tier 2 runtime isolated and unchanged
- [ ] Create a separate Premium Tier 1 package/worktree
- [ ] Add `domain/` for trades, signals, regimes, events, memory, and risk models
- [ ] Add `adapters/mt5/` for broker and terminal-specific execution
- [ ] Add `adapters/data/` for calendar, news, cross-asset, and macro providers
- [ ] Add `agents/` for gold, silver, oil, regime, reversal, memory, and research
- [ ] Add `strategy/` for entry, exit, scoring, and portfolio policies
- [ ] Add `risk/` for account, pair, currency, correlation, and session limits
- [ ] Add `storage/` for trades, signals, events, features, and audit history
- [ ] Add `telegram/` for commands, notifications, and the AI copilot
- [ ] Add `research/` for datasets, backtests, walk-forward tests, and reports
- [ ] Add `config/` for environment-specific broker and account settings
- [ ] Add `tests/` for unit, integration, replay, and risk-boundary tests

## Recommended build order

### Phase 0 — Freeze and baseline

- [ ] Freeze Tier 2 strategy and record its current configuration
- [ ] Export existing Tier 2 trade and signal history
- [ ] Define the baseline metrics and comparison period
- [ ] Confirm the Premium Tier 1 scope and broker choice

### Phase 1 — Safe runtime

- [ ] Implement explicit MT5 terminal and account selection
- [ ] Add broker/account identity gate
- [ ] Add worker isolation, magic numbers, and state namespaces
- [ ] Add deterministic risk, exposure, and emergency controls
- [ ] Add structured logs and heartbeat monitoring

### Phase 2 — Data adapters

- [ ] Build provider interfaces and mock responses
- [ ] Compare calendar providers
- [ ] Compare news/geopolitical providers
- [ ] Compare cross-asset data providers
- [ ] Add caching, rate limits, retries, timestamps, and stale-data checks
- [ ] Persist normalized events without coupling strategy code to a vendor

### Phase 3 — Gold and silver

- [ ] Implement gold specialist
- [ ] Implement silver specialist
- [ ] Validate contract and spread behavior on JustMarkets
- [ ] Backtest metals separately from currency pairs
- [ ] Add metal-specific risk and session policies

### Phase 4 — Core strategy

- [ ] Add regime classifier
- [ ] Add entry-location and structure analysis
- [ ] Add reversal and thesis-invalidation detection
- [ ] Add calibrated confidence and meta-labeling
- [ ] Add dynamic, bounded SL/TP policies
- [ ] Add controlled same-pair scale-in policy

### Phase 5 — Oil event module

- [ ] Keep oil disabled by default
- [ ] Add event qualification and multi-source confirmation
- [ ] Add long-horizon oil management
- [ ] Add separate oil risk budget
- [ ] Validate whether oil creates positive expectancy before enabling it

### Phase 6 — Memory and copilot

- [ ] Record every candidate and executed trade
- [ ] Classify losses and reversal failures
- [ ] Add pair/session/regime performance analysis
- [ ] Add OpenAI Telegram copilot with allow-listed tools
- [ ] Add explanations, reports, and approved recommendations

### Phase 7 — Validation

- [ ] Run historical replay with realistic costs
- [ ] Run walk-forward and out-of-sample testing
- [ ] Run Monte Carlo and stress tests
- [ ] Run signal-only mode
- [ ] Run demo/paper mode
- [ ] Compare against frozen Tier 2
- [ ] Define and verify the agreed improvement metric

### Phase 8 — Deployment

- [ ] Install isolated JustMarkets MT5 terminal on the VPS
- [ ] Start one Premium Tier 1 demo worker
- [ ] Verify account, server, symbols, spread, and Telegram identity
- [ ] Observe before enabling autonomous execution
- [ ] Add more accounts only through isolated workers
- [ ] Connect the website through a separate read/reporting API

## Gold specialist

- [x] Treat gold as a core asset and separate asset class, not just another forex pair
- [ ] Create a dedicated gold specialist agent that evaluates gold on every market cycle
- [ ] Validate contract size, tick value, spread, trading hours, and volatility
- [ ] Use gold-specific ATR, session, news, and position-size rules
- [ ] Monitor USD, yields, central-bank news, and geopolitical risk
- [ ] Test gold separately from currency-pair performance
- [ ] Keep silver available as a related specialist asset with its own risk rules

## Oil specialist

- [ ] Keep oil disabled by default until its broker symbol and contract rules are verified
- [ ] Trade oil only as an event-driven, longer-horizon opportunity after major geopolitical or supply news
- [ ] Limit oil to rare, high-conviction opportunities rather than normal day trading
- [ ] Add inventory, OPEC, supply, and geopolitical event handling
- [ ] Use separate oil risk and position-size limits
- [ ] Validate whether oil is an opportunity asset or only an external context signal

## Signal quality and market regime

- [ ] Preserve M15/H1/H4 confluence
- [ ] Add trend, range, volatility, and transition regime classification
- [ ] Add structure, support/resistance, breakout, and failed-breakout analysis
- [ ] Add reversal and thesis-invalidation detection
- [ ] Add liquidity-sweep, divergence, volatility-spike, and spread-shock detection
- [ ] Calibrate confidence from real outcomes instead of relying only on fixed bonuses
- [ ] Add pair/session/regime-specific thresholds
- [ ] Add meta-labeling for trade versus no-trade decisions
- [ ] Make confidence a calibrated outcome estimate, not an inflated additive score
- [ ] Use volume participation and liquidity quality as explicit signal inputs
- [ ] Keep Gold volume handling supportive rather than overly restrictive

## Trade management

- [ ] Keep deterministic maximum-loss and portfolio-risk controls
- [x] Make intraday multi-trade execution the primary style
- [ ] Keep longer-horizon trades limited to specific event modules such as oil
- [ ] Compare short targets against wider, structure/ATR-based targets
- [ ] Support dynamic SL/TP within hard dollar and risk limits
- [ ] Preserve the approved initial profit-lock behavior as a baseline
- [ ] Give the trading engine authority to manage entries and exits from its validated analysis
- [ ] Allow early exit only when a validated thesis-invalidation rule fires
- [ ] Decide whether a strong trend may remove or extend TP under strict rules
- [ ] Define whether multiple trades on one pair are allowed
- [ ] If multiple same-pair trades are allowed, enforce aggregate pair and currency exposure
- [ ] Replace any unconditional basket close with a validated portfolio-management policy
- [ ] Monitor open trades more frequently than the entry scan

### Level-based decision framework

- [ ] Define a pre-entry time/session gate before confidence is evaluated
- [ ] Define entry levels: initial setup, confirmed add-on, and exceptional third entry
- [x] Limit the same pair to a maximum of three trades in one cycle only when independent analysis justifies it
- [ ] Enforce aggregate pair, currency, and portfolio exposure across those trades
- [ ] Define profit-management levels before live testing
- [ ] Level 1 profit: exceptional trend may remove/extend TP and use controlled trailing
- [x] Level 2 profit: preserve the original TP and protect 35% of the original TP distance
- [ ] Level 3 profit: detect exhaustion/reversal risk and close or tighten protection before a full retracement
- [ ] Define an emergency profit level for abnormal reversal/news conditions
- [ ] Define loss-management levels before live testing
- [ ] Level 1 loss: thesis weakening may trigger an early small loss
- [ ] Level 2 loss: confirmed invalidation exits without waiting for the hard stop
- [ ] Level 3 loss: hard SL remains the final deterministic fallback
- [ ] Never widen a live stop merely to avoid realizing a loss
- [ ] Permit a wider structure-based stop only when approved before entry and total risk remains capped
- [ ] Make the 05:00–23:29 Dubai entry window a hard pre-entry gate
- [ ] Base daily/session limits on the live MT5 balance baseline, not a fixed dollar account assumption
- [ ] Use equity and floating drawdown for immediate open-risk protection
- [ ] Keep the 15% loss lock and 20% profit lock adjustable through authorized Telegram commands

## Trade memory and learning

- [ ] Record every candidate signal, including rejected signals
- [ ] Store the full feature snapshot and original trade thesis
- [ ] Store regime, session, spread, news, MFE, MAE, and exit reason
- [ ] Classify losses: bad signal, late entry, news shock, reversal, execution, spread, or risk
- [ ] Compare accepted trades against rejected opportunities
- [ ] Build pair/session/regime performance reports
- [ ] Require out-of-sample validation before changing live rules
- [ ] Never let automatic memory updates bypass risk controls

## Telegram and AI copilot

- [ ] Keep Telegram commands separate for each strategy/account
- [ ] Add live status, reports, position explanations, and risk summaries
- [ ] Add close-reason and loss-analysis messages
- [ ] Add heartbeat, MT5 disconnect, data-stale, and strategy-pause alerts
- [ ] Add an OpenAI copilot for explanations, research, summaries, and approved recommendations
- [ ] Restrict the copilot to allow-listed tools
- [ ] Require confirmation for risk, lot, account, and execution changes
- [ ] Never allow the LLM to bypass deterministic guards or expose credentials

## Agent topology

The premium system should use approximately **12–15 logical modules**, but only
around **3–5 need genuine AI/LLM reasoning**. The rest should be deterministic,
fast, auditable services:

### Deterministic or statistical modules

- [ ] Orchestrator and scheduling manager
- [ ] MT5 broker/execution adapter
- [ ] Market-data normalizer and data-quality monitor
- [ ] Multi-timeframe technical-analysis engine
- [ ] Gold specialist
- [ ] Silver specialist
- [ ] Oil event module
- [ ] Economic-calendar adapter and event gate
- [ ] Regime and reversal detector
- [ ] Signal/confluence scorer
- [ ] Portfolio risk and exposure manager
- [ ] Position/trade-management engine
- [ ] Trade-memory and research store
- [ ] Health monitor and audit logger

### AI/LLM-assisted modules

- [ ] News and geopolitical interpretation agent
- [ ] Trade-review and loss-analysis agent
- [ ] Telegram OpenAI copilot
- [ ] Optional research/calibration assistant

No LLM should be called on every tick. Live risk and stop protection must remain
deterministic and low-latency.

## Validation and deployment

- [ ] Build a reproducible historical dataset
- [ ] Backtest with spread, commission, slippage, and realistic execution
- [ ] Run walk-forward and out-of-sample tests
- [ ] Run Monte Carlo and drawdown stress tests
- [ ] Run signal-only and paper/demo phases
- [ ] Shadow Premium Tier 1 against the stable Tier 2 baseline
- [ ] Deploy to a dedicated VPS worker only after validation
- [ ] Start with one broker account and one Telegram bot
- [ ] Add additional accounts only through isolated workers
- [ ] Expose client services through a separate API/reporting layer

## Decisions still required

- [x] Initial premium broker: JustMarkets
- [x] Primary style: intraday multi-trade, with limited event-driven exceptions
- [x] Gold: core asset with a dedicated specialist module
- [x] Silver: included as a related specialist asset
- [x] Oil: rare, event-driven longer-horizon trading only
- [x] Bot authority: engine may manage trades autonomously within deterministic risk limits
- [x] Maximum same-pair positions: up to three in one cycle only with independent confirmation
- [x] Daily loss/profit policy: 15% loss lock and 20% profit lock based on the MT5 session balance baseline
- [x] Primary objective: target approximately 30% better accuracy/management, subject to measured validation
- [ ] Dynamic TP extension policy
- [ ] Approved economic-calendar and news providers
- [ ] Finalize the exact level thresholds and exposure limits
# Guarded MEXC Live Canary

This worker does **not** submit an order in its normal scan or paper mode. The
only live path is a separately confirmed, one-position canary.

## VPS setup

1. Replace the old worker folder with this package, then copy your existing
   `.env` back into the folder.
2. Create a new MEXC Futures API key for the VPS only:
   - enable **View Order Details** and **Order Placing**;
   - keep withdrawals disabled;
   - allowlist the VPS public IP if MEXC offers that option.
3. In the VPS `.env`, set:

   ```dotenv
   CRYPTO_LIVE_CANARY_ENABLED=true
   MEXC_TRADING_API_KEY=...
   MEXC_TRADING_API_SECRET=...
   ```

   Never send either value in chat or add them to a zip file.
4. Close or reconcile every manual MEXC position, ordinary order, and TP/SL
   plan. The canary refuses to run if any of these are present.

## Required no-trade validation

Run this from the worker folder:

```powershell
python main.py --live-preflight
```

It verifies the dedicated key against MEXC's current Futures execution host
and confirms the account is clean. It does not submit, cancel, or change an
order.

## One supervised live trade

Only after preflight reports `ready=True`, and only while watching MEXC:

```powershell
python main.py --live-canary --confirm-live
```

The worker accepts only a fully qualified strategy candidate, only at the
fixed `$50` notional / `20x` isolated profile, and only if planned initial
margin is at most `$2.50`. It includes stop-loss and take-profit in the MEXC
entry request. It then verifies the actual active exchange TP/SL plan. If that
verification fails, it persists the incident and submits an emergency close.

## Profit-lock monitor

While the canary is open, run:

```powershell
python main.py --live-monitor
```

When price reaches 65% of the fixed `$3` target, the worker moves the exchange
stop to preserve 35% of that target. It re-reads the MEXC plan before marking
the profit lock as applied.

## Recovering an unconfirmed submission safely

If a previous entry request returned an uncertain result, never delete
`runtime/live_canary.json`. First confirm MEXC displays no open position, normal
order, or TP/SL plan, then run:

```powershell
python main.py --live-reconcile
```

This command re-reads all three exchange surfaces itself. It clears the local
entry lock only when all are empty; it never submits, cancels, or changes an
exchange order.

## Never do this

- Do not run a second worker, laptop copy, or second `--live-canary` command.
- Do not remove `runtime/live_canary.json` to bypass a pending or failed state.
- Do not run a canary beside manual MEXC positions.
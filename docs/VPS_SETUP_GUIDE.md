# AgenticCore Forex PLUS — VPS Setup Guide
## Vultr Windows VPS + FBS Demo + Telegram

---

## STEP 1 — Create a New Windows VPS on Vultr

1. Go to **console.vultr.com** → click **"Create Instance"** (top right)
2. Choose **"Cloud Compute — Shared CPU"**
3. **Location:** Frankfurt (EMEA) — same region as your payram server, low latency
4. **Image:** Click the **Windows** tab → select **Windows Server 2022**
5. **Plan:** Choose **25 GB NVMe** → **$16/month** (1 vCPU, 2GB RAM — enough for MT5 + Python)
6. **Server Label:** `forex-bot` (or anything you like)
7. **SSH Keys:** Skip (Windows uses RDP password)
8. Click **Deploy Now**

⏳ Wait 3–5 minutes for the server to start. Status will change to **Running**.

---

## STEP 2 — Connect to Your VPS (RDP)

1. In Vultr, click your new server → you'll see **Username** and **Password**
   - Username: `Administrator`
   - Password: auto-generated (click the eye icon to reveal it)

2. **On your laptop (Windows):**
   - Press `Win + R` → type `mstsc` → Enter
   - Enter your VPS IP address → Connect
   - Username: `Administrator` | Password: from Vultr

3. **On your phone (if needed):**
   - Download **Microsoft Remote Desktop** app
   - Add your VPS IP, username, password

You're now inside the VPS — it looks like a Windows 11 desktop.

---

## STEP 3 — Install Python 3.11

1. Open **Microsoft Edge** on the VPS
2. Go to: **python.org/downloads**
3. Download **Python 3.11.x** (not 3.12/3.13 — MetaTrader5 package works best on 3.11)
4. Run the installer:
   - ✅ Tick **"Add Python to PATH"** (important!)
   - Click **Install Now**

5. Open **Command Prompt** (search "cmd" in Start)
6. Verify: `python --version` → should show Python 3.11.x

---

## STEP 4 — Open an FBS Demo Account

1. Go to **fbs.com** on the VPS browser
2. Click **Open Account** → choose **Demo Account**
3. Choose account type: **Standard** (ECN if available)
4. Fill in email, password
5. Note down:
   - **Login number** (e.g. 123456789)
   - **Password**
   - **Server name** (e.g. `FBS-Demo` or `FBSMarts-Demo`)

---

## STEP 5 — Install MetaTrader 5 (FBS version)

1. Log into your FBS account at **fbs.com**
2. Go to **Trading → MetaTrader 5 → Download MT5 for Windows**
3. Install it on the VPS
4. Open MT5 → it asks for a server
5. Type `FBS` in the search box → select your server (e.g. `FBS-Demo`)
6. Log in with your demo account number + password
7. **CRITICAL:** Click the **AutoTrading button** (top toolbar) → make sure it's **green**
   - If it's red/grey, the bot cannot place trades
8. Minimise MT5 (don't close it — it must stay open while the bot runs)

---

## STEP 6 — Copy the Bot Code to VPS

### Option A — Download from Replit (easiest, no Git needed)

1. In Replit, click the **three-dot menu** on the project → **Download as ZIP**
2. On your local laptop, extract the ZIP
3. Copy the folder `artifacts/agenticcore-forex` to your VPS via RDP:
   - In the RDP window, just drag and drop the folder to the VPS desktop
   - Or: open the VPS C: drive in RDP file explorer and paste it there

4. On the VPS, place it at: `C:\forex-bot\`
   - Final structure: `C:\forex-bot\v2\main.py`

### Option B — Git (if you have GitHub set up)

```bash
# In VPS Command Prompt:
cd C:\
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git forex-bot
```

---

## STEP 7 — Install Python Dependencies

Open **Command Prompt** on VPS and run:

```bash
cd C:\forex-bot
pip install MetaTrader5 python-telegram-bot feedparser google-genai pyyaml pandas numpy python-dotenv
```

This takes 2–3 minutes. You should see all packages install without errors.

Verify MT5 library works:
```bash
python -c "import MetaTrader5 as mt5; print('MT5 OK')"
```
Should print `MT5 OK`. If it errors, make sure you're on Python 3.11.

---

## STEP 8 — Configure the Bot

Open `C:\forex-bot\v2\config\config.yaml` in Notepad (or Notepad++) and update:

```yaml
# Change these:
dev_mode: false          # ← MUST be false on VPS (false = real MT5)

broker:
  name: "FBS"
  symbol_suffix: ""      # FBS uses standard names — no suffix needed
  filling_mode: auto

mt5:
  login: "YOUR_FBS_ACCOUNT_NUMBER"    # e.g. 123456789
  password: "YOUR_FBS_PASSWORD"
  server: "FBS-Demo"                  # exact server name from MT5 login screen

telegram:
  token: ""              # fill in Step 9
  admin_chat_id: ""      # fill in Step 9
```

Everything else (pairs, SL/TP, confidence, sessions) can stay as default for now.

---

## STEP 9 — Set Up Telegram Bot

### Create the bot (one-time):
1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Name: `AgenticCore Forex` (or anything)
4. Username: `agenticcore_forex_bot` (must be unique, end in _bot)
5. BotFather gives you a **token** like: `7123456789:AAFxxx...`
6. Copy this token into `config.yaml` → `telegram: token:`

### Get your Chat ID:
1. Start a chat with your new bot → send `/start`
2. The bot replies with your Chat ID (e.g. `1234567890`)
3. Copy it into `config.yaml` → `telegram: admin_chat_id:`

---

## STEP 10 — First Run

In Command Prompt on VPS:

```bash
cd C:\forex-bot
python v2/main.py
```

You should see:
```
AgenticCore Forex PLUS — Tier 2 AI Trading Framework
Broker: FBS  |  Mode: LIVE
[Bridge] Connecting: login=123456789 server=FBS-Demo
[Bridge] ✅ MT5Bridge connected — live trading active.
[Bot] ✅ Telegram bot online
[Manager] Scan #1 — 2026-08-19 03:00 UTC
[MarketData] Fetched 6/6 pairs (M15, H1, H4)
```

Then on Telegram, send `/status` — you should get a reply with your account balance.

---

## STEP 11 — Auto-Start on VPS Reboot (Windows Task Scheduler)

So the bot restarts automatically if the VPS reboots:

1. Press `Win + R` → type `taskschd.msc` → Enter (opens Task Scheduler)
2. Click **"Create Basic Task"** (right panel)
3. Name: `ForexBot`
4. Trigger: **When the computer starts**
5. Action: **Start a program**
6. Program: `python`
7. Arguments: `v2/main.py`
8. Start in: `C:\forex-bot`
9. ✅ Tick **"Run whether user is logged on or not"**
10. Click Finish

Now the bot will restart automatically after any VPS reboot.

---

## STEP 12 — Test Everything via Telegram

Send these commands from your phone to confirm everything works:

```
/status      → should show balance, mode, active sessions
/session     → should show which forex session is active
/memory      → shows "No data yet" (normal — no trades yet)
/broker      → confirms FBS, no suffix
/mode semi   → switch to semi-auto (you approve first trade manually)
/approve     → approve a trade when one appears
/mode auto   → switch back to auto once you're confident
```

---

## Common Issues & Fixes

| Problem | Fix |
|---|---|
| `MT5 init failed` | MT5 terminal must be **open and logged in** before running the bot |
| `AutoTrading disabled` | Click the AutoTrading button in MT5 — must be green |
| `No tick for EURUSD` | Right-click EURUSD in Market Watch → "Show" |
| Bot not responding on Telegram | Check token in config.yaml — no spaces, no quotes |
| `TRADE_RETCODE_REJECT` | Check MT5 → Tools → Options → Expert Advisors → Allow automated trading |
| Bot crashes on start | Run `python -m pip install --upgrade python-telegram-bot` |

---

## Daily Routine

- **Morning check:** Send `/status` on Telegram → see overnight P&L
- **If something looks wrong:** Send `/stop` → check logs → `/resume`
- **Change settings live:** `/settp 40` `/setsl 15` `/setconfidence 70` etc.
- **Emergency:** `/closeall` closes everything instantly

The VPS runs 24/7 — you never need to RDP in unless you're doing a code update.

---

## Updating the Bot Code (when we improve it here in Replit)

```bash
# RDP into VPS → open Command Prompt → run:
cd C:\forex-bot
git pull           # if using Git

# Or: download ZIP from Replit again, replace only the v2/ folder

# Then restart the bot:
# Press Ctrl+C in the running bot window → then:
python v2/main.py
```

---

*Guide version: August 2026 | AgenticCore Forex PLUS v2*

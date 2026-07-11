# NSE Momentum Stock Telegram Bot

Sends you the top 20 momentum stocks (NSE) every weekday morning at 8:30 AM IST via Telegram — free, no server required.

## How the ranking works
- Universe: Nifty 500 (configurable to Nifty 200 / Nifty 50)
- Filters: price ≥ ₹20, 20-day avg volume ≥ 100,000, price above 50-day moving average (trend confirmation)
- Composite momentum score (0-100), weighted:
  - 35% — 3-month return rank
  - 30% — 1-month return rank
  - 20% — volume surge rank (5-day avg vol / 20-day avg vol)
  - 15% — RSI(14) rank
- Top 20 by score are sent, with LTP, 1D/1M/3M returns, RSI, and volume ratio.

This is a **rules-based screener**, not investment advice — always do your own due diligence before acting on any list like this.

## Setup (about 10 minutes)

### 1. Create your Telegram bot
1. Open Telegram, search for **@BotFather**, send `/newbot`
2. Follow the prompts, give it a name — BotFather gives you a **token** like `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`. Save it.
3. Search for your new bot by its username and send it any message (e.g. "hi") so it can message you back.

### 2. Get your chat ID
1. Visit this URL in your browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
2. Look for `"chat":{"id":123456789,...}` in the response — that number is your **chat ID**.

### 3. Push this code to a GitHub repo
1. Create a new **private** GitHub repo (private keeps your token safer, though it's stored as a secret either way)
2. Push all these files (`momentum_stock_bot.py`, `requirements.txt`, `.github/workflows/momentum_report.yml`, this README) to it

### 4. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**
- `TELEGRAM_BOT_TOKEN` = the token from step 1
- `TELEGRAM_CHAT_ID` = the chat ID from step 2

### 5. Test it
Go to the **Actions** tab → **Daily Momentum Stock Report** → **Run workflow** (this triggers it manually via `workflow_dispatch`). You should get a Telegram message within a couple of minutes.

Once that works, it will run automatically every weekday at 8:30 AM IST — no laptop or server needed, GitHub runs it for free.

## Running locally (optional, for testing/tweaking)
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python momentum_stock_bot.py
```

## Tuning it
Edit the top of `momentum_stock_bot.py` or set these as env vars / GitHub Actions env:
- `UNIVERSE`: `nifty50`, `nifty200`, or `nifty500` (default)
- `TOP_N`: how many stocks to report (default 20)
- `MIN_PRICE`, `MIN_AVG_VOLUME`: liquidity filters
- Change the scoring weights directly in `fetch_and_score()` if you want to favor volume surges over longer-term trend, etc.

## Notes & limitations
- Uses free Yahoo Finance data via `yfinance` — occasionally rate-limited or briefly unavailable; the script has basic retry-friendly batching but isn't bulletproof.
- NSE's official CSV list can occasionally be unreachable from GitHub's servers; the script falls back to a smaller static list of ~50 liquid large-caps if so.
- This is not investment advice. Momentum screens can whipsaw in choppy markets — treat this as a research shortlist, not a buy signal.

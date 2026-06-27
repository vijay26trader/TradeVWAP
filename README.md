# 📊 VWAP + RSI Signal Scanner

An automated intraday stock signal scanner that runs every **~2 minutes** between **9 AM – 6 PM EST** on weekdays via GitHub Actions.

## What It Checks

| Condition | Threshold |
|---|---|
| VWAP Deviation | ≥ 2% from session VWAP |
| RSI (14-period) | ≤ 35 (oversold) or ≥ 65 (overbought) |
| RSI Divergence | Bullish (LONG) or Bearish (SHORT) |
| Volume | ≥ 1.2× the 20-bar average |

A **LONG** signal requires: price stretched below VWAP + RSI oversold + Bullish divergence  
A **SHORT** signal requires: price stretched above VWAP + RSI overbought + Bearish divergence

## Signal Columns

| Column | Description |
|---|---|
| Timestamp | Time the signal was detected (EST) |
| Symbol | Stock ticker |
| Price | Current price at scan time |
| VWAP | Session VWAP value |
| % from VWAP | Price deviation from VWAP |
| RSI (14) | RSI reading |
| RSI Divergence | Bullish / Bearish / None |
| Volume | Volume on that 15-min bar |
| Vol Ratio | Volume ÷ 20-bar average |
| Signal | LONG / SHORT |
| Strength | ★ to ★★★ (confluence of conditions met) |
| Stop Loss | 1 ATR beyond the signal bar |
| Target (VWAP) | VWAP reversion target |
| Status | New / Active / Expired |

## Default Tickers

SPY, AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, JPM, V

## Setup Instructions

### 1. Fork / Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/vwap-scanner.git
cd vwap-scanner
```

### 2. Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Value |
|---|---|
| `EMAIL_SENDER` | Your Gmail address (e.g. `you@gmail.com`) |
| `EMAIL_PASSWORD` | Gmail App Password (NOT your regular password) |
| `EMAIL_RECEIVER` | Where to receive alerts |
| `SMTP_HOST` | `smtp.gmail.com` (default) |
| `SMTP_PORT` | `587` (default) |
| `EXTRA_TICKERS` | Optional: comma-separated extra tickers e.g. `NFLX,AMD,PLTR` |

> **Gmail App Password**: Go to myaccount.google.com → Security → 2-Step Verification → App Passwords → generate one for "Mail".

### 3. Add Custom Tickers (optional)

Edit `data/custom_tickers.txt` and add tickers one per line:

```
NFLX
AMD
PLTR
```

Or set the `EXTRA_TICKERS` GitHub Secret: `NFLX,AMD,PLTR`

### 4. Enable GitHub Actions

- Go to your repo → **Actions** tab
- Click **"I understand my workflows, go ahead and enable them"**
- The scanner will automatically run every 5 minutes during market hours (two passes per cycle for ~2-min cadence)

### 5. Enable GitHub Pages (for HTML dashboard)

- Go to **Settings → Pages**
- Source: **Deploy from a branch**
- Branch: `main` / folder: `/output`
- Your dashboard will be live at: `https://YOUR_USERNAME.github.io/vwap-scanner/`

### 6. Run Locally (optional)

```bash
pip install -r requirements.txt

# Set environment variables
export EMAIL_SENDER="you@gmail.com"
export EMAIL_PASSWORD="your-app-password"
export EMAIL_RECEIVER="alerts@youremail.com"

python scanner.py
```

## Project Structure

```
vwap-scanner/
├── .github/
│   └── workflows/
│       └── scanner.yml       # GitHub Actions workflow
├── data/
│   └── custom_tickers.txt    # Add your custom tickers here
├── output/
│   ├── index.html            # Live HTML dashboard (auto-generated)
│   └── signals.json          # Raw signal data (auto-generated)
├── scanner.py                # Main scanner script
├── requirements.txt
└── README.md
```

## Disclaimer

This tool is for **educational and informational purposes only**.  
It does **not** constitute financial advice. Always do your own research before trading.

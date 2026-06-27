#!/usr/bin/env python3
"""
VWAP Mean Reversion + RSI Divergence Scanner
Runs every 2 minutes between 9AM - 6PM EST
Checks: VWAP deviation, RSI, RSI divergence, Volume vs avg
Saves results to HTML and sends email alerts on new signals
"""

import os
import json
import smtplib
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd
import numpy as np

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
EST = ZoneInfo("America/New_York")
MARKET_OPEN  = 9   # 9 AM EST
MARKET_CLOSE = 18  # 6 PM EST

DEFAULT_TICKERS = [
    "SPY", "AAPL", "MSFT", "NVDA", "AMZN",
    "GOOGL", "META", "TSLA", "JPM", "V"
]

# Load extra tickers from env or file
EXTRA_TICKERS_ENV  = os.environ.get("EXTRA_TICKERS", "")
EXTRA_TICKERS_FILE = "data/custom_tickers.txt"

# Email config — set these as GitHub Secrets
EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT") or "587")

# Signal thresholds
VWAP_DEV_LARGE_CAP  = 0.02   # 2% for large-caps (SPY, AAPL, MSFT …)
VWAP_DEV_MID_CAP    = 0.015  # 1.5% for mid-caps
RSI_OVERSOLD        = 35
RSI_OVERBOUGHT      = 65
RSI_PERIOD          = 14
VOLUME_MULTIPLIER   = 1.2    # volume must be 1.2× the 20-bar average
LOOKBACK_BARS       = 60     # how many 15-min bars to fetch (~1 trading day)
DIVERGENCE_WINDOW   = 5      # bars to look back for divergence

RESULTS_FILE  = "docs/signals.json"
HTML_FILE     = "docs/index.html"

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    now = datetime.now(EST)
    return now.weekday() < 5 and MARKET_OPEN <= now.hour < MARKET_CLOSE


def load_tickers() -> list[str]:
    tickers = list(DEFAULT_TICKERS)
    if EXTRA_TICKERS_ENV:
        tickers += [t.strip().upper() for t in EXTRA_TICKERS_ENV.split(",") if t.strip()]
    if os.path.exists(EXTRA_TICKERS_FILE):
        with open(EXTRA_TICKERS_FILE) as f:
            tickers += [t.strip().upper() for t in f if t.strip() and not t.startswith("#")]
    return list(dict.fromkeys(tickers))  # deduplicate, preserve order


def compute_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets each trading day."""
    df = df.copy()
    df["date"] = df.index.date
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    df["tpv"] = typical * df["Volume"]
    vwap_vals = []
    for date, grp in df.groupby("date"):
        cum_tpv = grp["tpv"].cumsum()
        cum_vol = grp["Volume"].cumsum()
        vwap_vals.append(cum_tpv / cum_vol)
    return pd.concat(vwap_vals).reindex(df.index)


def detect_divergence(price: pd.Series, rsi: pd.Series, window: int = DIVERGENCE_WINDOW) -> str:
    """
    Bullish divergence : price makes lower low, RSI makes higher low
    Bearish divergence : price makes higher high, RSI makes lower high
    """
    if len(price) < window + 1:
        return "None"
    p = price.iloc[-window:]
    r = rsi.iloc[-window:]
    if p.iloc[-1] < p.min() and r.iloc[-1] > r.min():
        return "Bullish"
    if p.iloc[-1] > p.max() and r.iloc[-1] < r.max():
        return "Bearish"
    return "None"


def analyse_ticker(symbol: str) -> dict | None:
    try:
        df = yf.download(symbol, period="5d", interval="15m", progress=False, auto_adjust=True)
        if df.empty or len(df) < RSI_PERIOD + DIVERGENCE_WINDOW + 5:
            log.warning(f"{symbol}: insufficient data")
            return None

        # Flatten multi-index columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.index = pd.to_datetime(df.index)

        vwap = compute_vwap(df)
        rsi  = compute_rsi(df["Close"])
        df["VWAP"] = vwap
        df["RSI"]  = rsi

        # ── Latest values ─────────────────────────────────────────────────
        last        = df.iloc[-1]
        price       = float(last["Close"])
        vwap_val    = float(last["VWAP"])
        rsi_val     = float(last["RSI"])
        volume      = float(last["Volume"])
        vol_avg     = float(df["Volume"].iloc[-20:].mean())
        pct_from_vwap = (price - vwap_val) / vwap_val

        # ── Conditions ────────────────────────────────────────────────────
        threshold       = VWAP_DEV_LARGE_CAP
        vwap_stretched  = abs(pct_from_vwap) >= threshold
        volume_ok       = volume >= vol_avg * VOLUME_MULTIPLIER
        divergence      = detect_divergence(df["Close"], df["RSI"])

        # Signal direction
        long_signal  = (pct_from_vwap <= -threshold and
                        rsi_val <= RSI_OVERSOLD and
                        divergence == "Bullish")
        short_signal = (pct_from_vwap >= threshold and
                        rsi_val >= RSI_OVERBOUGHT and
                        divergence == "Bearish")

        if not (long_signal or short_signal):
            return None

        signal_type = "LONG" if long_signal else "SHORT"

        # ── Confluence score (0-3) ─────────────────────────────────────────
        score = sum([vwap_stretched, volume_ok, divergence != "None"])

        # ── Risk levels ───────────────────────────────────────────────────
        atr  = float((df["High"] - df["Low"]).iloc[-14:].mean())
        stop = round(price - atr if long_signal else price + atr, 2)
        tgt  = round(vwap_val, 2)

        return {
            "timestamp":      datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST"),
            "symbol":         symbol,
            "price":          round(price, 2),
            "vwap":           round(vwap_val, 2),
            "pct_from_vwap":  round(pct_from_vwap * 100, 2),
            "rsi":            round(rsi_val, 1),
            "divergence":     divergence,
            "volume":         int(volume),
            "vol_avg":        int(vol_avg),
            "vol_ratio":      round(volume / vol_avg, 2),
            "signal":         signal_type,
            "strength":       score,
            "stop_loss":      stop,
            "target":         tgt,
            "status":         "New",
        }

    except Exception as e:
        log.error(f"{symbol}: {e}")
        return None


# ── Persistence ───────────────────────────────────────────────────────────────

def load_existing() -> list[dict]:
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            return json.load(f)
    return []


def save_results(results: list[dict]) -> None:
    os.makedirs("docs", exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


# ── HTML ──────────────────────────────────────────────────────────────────────

def generate_html(results: list[dict]) -> None:
    now = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")
    rows = ""
    for r in reversed(results):  # newest first
        sig_class = "long" if r["signal"] == "LONG" else "short"
        div_class = "bullish" if r["divergence"] == "Bullish" else ("bearish" if r["divergence"] == "Bearish" else "")
        strength_icons = "★" * r["strength"] + "☆" * (3 - r["strength"])
        vol_flag = "🔥" if r["vol_ratio"] >= VOLUME_MULTIPLIER else ""
        rows += f"""
        <tr class="{sig_class}">
            <td>{r['timestamp']}</td>
            <td><strong>{r['symbol']}</strong></td>
            <td>${r['price']}</td>
            <td>${r['vwap']}</td>
            <td class="{'neg' if r['pct_from_vwap'] < 0 else 'pos'}">{r['pct_from_vwap']:+.2f}%</td>
            <td>{r['rsi']}</td>
            <td class="{div_class}">{r['divergence']}</td>
            <td>{r['volume']:,} {vol_flag}</td>
            <td>{r['vol_ratio']}×</td>
            <td class="signal-badge {sig_class}">{r['signal']}</td>
            <td class="strength">{strength_icons}</td>
            <td>${r['stop_loss']}</td>
            <td>${r['target']}</td>
            <td><span class="status">{r['status']}</span></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="120">
<title>VWAP + RSI Signal Scanner</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; min-height: 100vh; }}

  header {{ background: linear-gradient(135deg, #1f2937, #111827); padding: 20px 32px;
            border-bottom: 1px solid #30363d; display: flex; align-items: center; justify-content: space-between; }}
  header h1 {{ font-size: 1.4rem; color: #f0f6fc; }}
  header .meta {{ font-size: 0.8rem; color: #8b949e; }}
  .badge {{ background: #238636; color: #fff; border-radius: 12px; padding: 2px 10px; font-size: 0.75rem; margin-left: 10px; }}
  .badge.live {{ background: #da3633; animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.5}} }}

  .stats {{ display: flex; gap: 20px; padding: 16px 32px; background: #161b22; border-bottom: 1px solid #30363d; flex-wrap: wrap; }}
  .stat {{ background: #1f2937; border-radius: 8px; padding: 12px 20px; min-width: 130px; }}
  .stat .label {{ font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: .05em; }}
  .stat .value {{ font-size: 1.4rem; font-weight: 700; color: #f0f6fc; margin-top: 4px; }}
  .stat .value.green {{ color: #3fb950; }}
  .stat .value.red {{ color: #f85149; }}

  .toolbar {{ padding: 14px 32px; display: flex; gap: 12px; align-items: center; background: #0d1117; flex-wrap: wrap; }}
  .toolbar input {{ background: #161b22; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px;
                    padding: 6px 12px; font-size: 0.85rem; width: 260px; }}
  .toolbar input:focus {{ outline: none; border-color: #58a6ff; }}
  .toolbar button {{ background: #238636; color: #fff; border: none; border-radius: 6px;
                     padding: 7px 16px; cursor: pointer; font-size: 0.85rem; }}
  .toolbar button:hover {{ background: #2ea043; }}
  .toolbar select {{ background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
                     border-radius: 6px; padding: 6px 10px; font-size: 0.85rem; }}
  .count {{ margin-left: auto; font-size: 0.8rem; color: #8b949e; }}

  .table-wrap {{ overflow-x: auto; padding: 0 32px 32px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-top: 16px; }}
  thead tr {{ background: #161b22; position: sticky; top: 0; z-index: 1; }}
  thead th {{ padding: 10px 12px; text-align: left; color: #8b949e; font-weight: 600;
              border-bottom: 2px solid #30363d; white-space: nowrap; cursor: pointer; user-select: none; }}
  thead th:hover {{ color: #58a6ff; }}
  tbody tr {{ border-bottom: 1px solid #21262d; transition: background .15s; }}
  tbody tr:hover {{ background: #1c2128; }}
  tbody td {{ padding: 9px 12px; white-space: nowrap; }}

  tr.long  {{ border-left: 3px solid #3fb950; }}
  tr.short {{ border-left: 3px solid #f85149; }}

  .signal-badge {{ font-weight: 700; font-size: 0.75rem; padding: 3px 8px; border-radius: 4px; display: inline-block; }}
  .signal-badge.long  {{ background: #1a4731; color: #3fb950; }}
  .signal-badge.short {{ background: #4d1d1d; color: #f85149; }}

  .bullish {{ color: #3fb950; font-weight: 600; }}
  .bearish {{ color: #f85149; font-weight: 600; }}
  .pos {{ color: #3fb950; }}
  .neg {{ color: #f85149; }}
  .strength {{ letter-spacing: 2px; color: #e3b341; }}

  .status {{ background: #1f3a5f; color: #58a6ff; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; }}

  .empty {{ text-align: center; padding: 60px; color: #8b949e; font-size: 1rem; }}
  .refresh-note {{ text-align: center; font-size: 0.75rem; color: #8b949e; padding: 10px; }}
</style>
</head>
<body>

<header>
  <div>
    <h1>📊 VWAP + RSI Signal Scanner <span class="badge live">● LIVE</span></h1>
    <div class="meta">Checks every 2 mins · 9:00 AM – 6:00 PM EST · Auto-refreshes every 2 mins</div>
  </div>
  <div class="meta">Last scan: <strong>{now}</strong></div>
</header>

<div class="stats">
  <div class="stat"><div class="label">Total Signals</div><div class="value">{len(results)}</div></div>
  <div class="stat"><div class="label">Long Signals</div><div class="value green">{sum(1 for r in results if r['signal']=='LONG')}</div></div>
  <div class="stat"><div class="label">Short Signals</div><div class="value red">{sum(1 for r in results if r['signal']=='SHORT')}</div></div>
  <div class="stat"><div class="label">High Strength (★★★)</div><div class="value">{sum(1 for r in results if r['strength']==3)}</div></div>
  <div class="stat"><div class="label">Symbols Watched</div><div class="value">{len(load_tickers())}</div></div>
</div>

<div class="toolbar">
  <input type="text" id="filterInput" placeholder="🔍 Filter by symbol, signal, divergence…" oninput="filterTable()">
  <select id="signalFilter" onchange="filterTable()">
    <option value="">All Signals</option>
    <option value="LONG">LONG only</option>
    <option value="SHORT">SHORT only</option>
  </select>
  <select id="strengthFilter" onchange="filterTable()">
    <option value="">All Strengths</option>
    <option value="3">★★★ High</option>
    <option value="2">★★ Medium</option>
    <option value="1">★ Low</option>
  </select>
  <button onclick="window.location.reload()">↻ Refresh</button>
  <span class="count" id="rowCount"></span>
</div>

<div class="table-wrap">
  {'<p class="empty">⏳ No signals detected yet. Scanner is running…</p>' if not results else f"""
  <table id="signalTable">
    <thead>
      <tr>
        <th>⏱ Timestamp</th>
        <th>Symbol</th>
        <th>Price</th>
        <th>VWAP</th>
        <th>% from VWAP</th>
        <th>RSI ({RSI_PERIOD})</th>
        <th>RSI Divergence</th>
        <th>Volume</th>
        <th>Vol Ratio</th>
        <th>Signal</th>
        <th>Strength</th>
        <th>Stop Loss</th>
        <th>Target (VWAP)</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody id="tableBody">
      {rows}
    </tbody>
  </table>"""}
</div>

<div class="refresh-note">Page auto-refreshes every 2 minutes · Scanner runs 9 AM – 6 PM EST on weekdays</div>

<script>
function filterTable() {{
  const text   = document.getElementById('filterInput').value.toLowerCase();
  const signal = document.getElementById('signalFilter').value;
  const str    = document.getElementById('strengthFilter').value;
  const rows   = document.querySelectorAll('#tableBody tr');
  let visible  = 0;
  rows.forEach(row => {{
    const rowText = row.innerText.toLowerCase();
    const sigCell = row.querySelector('.signal-badge')?.innerText || '';
    const strCell = row.querySelector('.strength')?.innerText || '';
    const strCount = (strCell.match(/★/g) || []).length.toString();
    const show = rowText.includes(text) &&
                 (!signal || sigCell.includes(signal)) &&
                 (!str    || strCount === str);
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  const el = document.getElementById('rowCount');
  if (el) el.innerText = visible + ' signal(s) shown';
}}
window.onload = filterTable;
</script>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(HTML_FILE, "w") as f:
        f.write(html)
    log.info(f"HTML saved → {HTML_FILE}")


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(new_signals: list[dict]) -> None:
    if not all([EMAIL_SENDER, EMAIL_PASSWORD, EMAIL_RECEIVER]):
        log.warning("Email not configured — skipping notification")
        return
    if not new_signals:
        return

    subject = f"🚨 VWAP Scanner: {len(new_signals)} New Signal(s) — {datetime.now(EST).strftime('%H:%M EST')}"

    rows = ""
    for r in new_signals:
        color = "#3fb950" if r["signal"] == "LONG" else "#f85149"
        rows += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #30363d">{r['timestamp']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d"><strong>{r['symbol']}</strong></td>
          <td style="padding:8px;border-bottom:1px solid #30363d">${r['price']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">${r['vwap']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">{r['pct_from_vwap']:+.2f}%</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">{r['rsi']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">{r['divergence']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d;color:{color}"><strong>{r['signal']}</strong></td>
          <td style="padding:8px;border-bottom:1px solid #30363d">{"★"*r['strength']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">${r['stop_loss']}</td>
          <td style="padding:8px;border-bottom:1px solid #30363d">${r['target']}</td>
        </tr>"""

    body = f"""
    <html><body style="font-family:Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px">
    <h2 style="color:#f0f6fc">📊 VWAP + RSI Scanner Alert</h2>
    <p style="color:#8b949e">{len(new_signals)} new signal(s) detected at {datetime.now(EST).strftime('%Y-%m-%d %H:%M EST')}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:16px">
      <thead>
        <tr style="background:#161b22">
          <th style="padding:8px;text-align:left;color:#8b949e">Time</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Symbol</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Price</th>
          <th style="padding:8px;text-align:left;color:#8b949e">VWAP</th>
          <th style="padding:8px;text-align:left;color:#8b949e">% Dev</th>
          <th style="padding:8px;text-align:left;color:#8b949e">RSI</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Divergence</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Signal</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Strength</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Stop</th>
          <th style="padding:8px;text-align:left;color:#8b949e">Target</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="margin-top:20px;font-size:12px;color:#8b949e">
      ⚠️ This is for informational purposes only. Always do your own analysis before trading.
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        log.info(f"Email sent to {EMAIL_RECEIVER}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("VWAP + RSI Scanner starting")

    if not is_market_hours():
        now = datetime.now(EST)
        log.info(f"Outside market hours ({now.strftime('%H:%M EST')} on {now.strftime('%A')}). Exiting.")
        # Still regenerate HTML so the page stays fresh
        existing = load_existing()
        generate_html(existing)
        return

    tickers = load_tickers()
    log.info(f"Scanning {len(tickers)} tickers: {', '.join(tickers)}")

    new_signals = []
    for symbol in tickers:
        log.info(f"Analysing {symbol}…")
        result = analyse_ticker(symbol)
        if result:
            log.info(f"  ✅ Signal: {result['signal']} | RSI={result['rsi']} | "
                     f"VWAP dev={result['pct_from_vwap']}% | Strength={result['strength']}")
            new_signals.append(result)
        else:
            log.info(f"  — No signal")

    # Merge with history (keep today's signals + new ones, avoid duplicates)
    existing = load_existing()
    today    = datetime.now(EST).strftime("%Y-%m-%d")
    existing_today = [r for r in existing if r["timestamp"].startswith(today)]

    # Deduplicate: same symbol + signal within the same 2-min window
    seen = {(r["symbol"], r["signal"], r["timestamp"][:16]) for r in existing_today}
    truly_new = [r for r in new_signals
                 if (r["symbol"], r["signal"], r["timestamp"][:16]) not in seen]

    all_results = existing_today + truly_new
    # Keep last 500 records
    all_results = all_results[-500:]

    save_results(all_results)
    generate_html(all_results)

    if truly_new:
        log.info(f"📧 Sending email for {len(truly_new)} new signal(s)")
        send_email(truly_new)
    else:
        log.info("No new unique signals this run.")

    log.info("Scan complete.")


if __name__ == "__main__":
    main()

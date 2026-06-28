#!/usr/bin/env python3
"""
VWAP Mean Reversion + RSI Divergence Scanner
- SQLite database for persistent signal storage
- Runs every 2 minutes between 9AM - 6PM EST
- Checks: VWAP deviation, RSI, RSI divergence, Volume vs avg
- Saves results to SQLite + HTML dashboard + email alerts
"""

import os
import sqlite3
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
from contextlib import contextmanager

import yfinance as yf
import pandas as pd
import numpy as np

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
EST = ZoneInfo("America/New_York")
MARKET_OPEN  = 9
MARKET_CLOSE = 18

DEFAULT_TICKERS = [
    "SPY", "AAPL", "MSFT", "NVDA", "AMZN",
    "GOOGL", "META", "TSLA", "JPM", "V"
]

EXTRA_TICKERS_ENV  = os.environ.get("EXTRA_TICKERS", "")
EXTRA_TICKERS_FILE = "data/custom_tickers.txt"

EMAIL_SENDER   = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT") or "587")

VWAP_DEV_THRESHOLD = 0.02   # 2% deviation
RSI_OVERSOLD       = 35
RSI_OVERBOUGHT     = 65
RSI_PERIOD         = 14
VOLUME_MULTIPLIER  = 1.2
DIVERGENCE_WINDOW  = 5

DB_FILE   = "docs/signals.db"
HTML_FILE = "docs/index.html"

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs("docs", exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                scan_date     TEXT    NOT NULL,
                symbol        TEXT    NOT NULL,
                price         REAL    NOT NULL,
                vwap          REAL    NOT NULL,
                pct_from_vwap REAL    NOT NULL,
                rsi           REAL    NOT NULL,
                divergence    TEXT    NOT NULL,
                volume        INTEGER NOT NULL,
                vol_avg       INTEGER NOT NULL,
                vol_ratio     REAL    NOT NULL,
                signal        TEXT    NOT NULL,
                strength      INTEGER NOT NULL,
                stop_loss     REAL    NOT NULL,
                target        REAL    NOT NULL,
                status        TEXT    NOT NULL DEFAULT 'New'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_date ON signals(scan_date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_symbol ON signals(symbol)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal ON signals(signal)
        """)
        conn.commit()
    log.info(f"Database ready → {DB_FILE}")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def is_duplicate(conn, symbol: str, signal: str, window_start: str) -> bool:
    """Check if same symbol+signal already recorded in the last 2-min window."""
    row = conn.execute("""
        SELECT id FROM signals
        WHERE symbol = ? AND signal = ? AND timestamp >= ?
        LIMIT 1
    """, (symbol, signal, window_start)).fetchone()
    return row is not None


def insert_signal(conn, r: dict) -> int:
    cur = conn.execute("""
        INSERT INTO signals
            (timestamp, scan_date, symbol, price, vwap, pct_from_vwap,
             rsi, divergence, volume, vol_avg, vol_ratio,
             signal, strength, stop_loss, target, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["timestamp"], r["scan_date"], r["symbol"],
        r["price"], r["vwap"], r["pct_from_vwap"],
        r["rsi"], r["divergence"], r["volume"], r["vol_avg"], r["vol_ratio"],
        r["signal"], r["strength"], r["stop_loss"], r["target"], r["status"]
    ))
    conn.commit()
    return cur.lastrowid


def fetch_signals(conn, days: int = 30) -> list[dict]:
    """Fetch signals from the last N days, newest first."""
    since = (datetime.now(EST) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""
        SELECT * FROM signals
        WHERE scan_date >= ?
        ORDER BY id DESC
        LIMIT 1000
    """, (since,)).fetchall()
    return [dict(r) for r in rows]


def fetch_stats(conn) -> dict:
    """Aggregate stats for the dashboard header."""
    today = datetime.now(EST).strftime("%Y-%m-%d")
    stats = {}

    row = conn.execute("SELECT COUNT(*) as cnt FROM signals WHERE scan_date = ?", (today,)).fetchone()
    stats["today"] = row["cnt"]

    row = conn.execute("SELECT COUNT(*) as cnt FROM signals", ).fetchone()
    stats["total"] = row["cnt"]

    row = conn.execute("SELECT COUNT(*) as cnt FROM signals WHERE signal='LONG' AND scan_date=?", (today,)).fetchone()
    stats["long_today"] = row["cnt"]

    row = conn.execute("SELECT COUNT(*) as cnt FROM signals WHERE signal='SHORT' AND scan_date=?", (today,)).fetchone()
    stats["short_today"] = row["cnt"]

    row = conn.execute("SELECT COUNT(*) as cnt FROM signals WHERE strength=3 AND scan_date=?", (today,)).fetchone()
    stats["high_strength"] = row["cnt"]

    row = conn.execute("""
        SELECT symbol, COUNT(*) as cnt FROM signals
        WHERE scan_date >= date('now','-7 days')
        GROUP BY symbol ORDER BY cnt DESC LIMIT 1
    """).fetchone()
    stats["top_symbol"] = f"{row['symbol']} ({row['cnt']})" if row else "—"

    return stats


# ── Market helpers ────────────────────────────────────────────────────────────

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
    return list(dict.fromkeys(tickers))


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    df = df.copy()
    df["date"] = df.index.date
    typical    = (df["High"] + df["Low"] + df["Close"]) / 3
    df["tpv"]  = typical * df["Volume"]
    vwap_vals  = []
    for _, grp in df.groupby("date"):
        vwap_vals.append(grp["tpv"].cumsum() / grp["Volume"].cumsum())
    return pd.concat(vwap_vals).reindex(df.index)


def detect_divergence(price: pd.Series, rsi: pd.Series, window: int = DIVERGENCE_WINDOW) -> str:
    if len(price) < window + 1:
        return "None"
    p = price.iloc[-window:]
    r = rsi.iloc[-window:]
    if p.iloc[-1] < p.min() and r.iloc[-1] > r.min():
        return "Bullish"
    if p.iloc[-1] > p.max() and r.iloc[-1] < r.max():
        return "Bearish"
    return "None"


# ── Scanner ───────────────────────────────────────────────────────────────────

def analyse_ticker(symbol: str) -> dict | None:
    try:
        df = yf.download(symbol, period="5d", interval="15m", progress=False, auto_adjust=True)
        if df.empty or len(df) < RSI_PERIOD + DIVERGENCE_WINDOW + 5:
            log.warning(f"{symbol}: insufficient data")
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.index     = pd.to_datetime(df.index)
        df["VWAP"]   = compute_vwap(df)
        df["RSI"]    = compute_rsi(df["Close"])

        last          = df.iloc[-1]
        price         = float(last["Close"])
        vwap_val      = float(last["VWAP"])
        rsi_val       = float(last["RSI"])
        volume        = float(last["Volume"])
        vol_avg       = float(df["Volume"].iloc[-20:].mean())
        pct_from_vwap = (price - vwap_val) / vwap_val
        divergence    = detect_divergence(df["Close"], df["RSI"])

        vwap_stretched = abs(pct_from_vwap) >= VWAP_DEV_THRESHOLD
        volume_ok      = volume >= vol_avg * VOLUME_MULTIPLIER

        long_signal  = pct_from_vwap <= -VWAP_DEV_THRESHOLD and rsi_val <= RSI_OVERSOLD  and divergence == "Bullish"
        short_signal = pct_from_vwap >=  VWAP_DEV_THRESHOLD and rsi_val >= RSI_OVERBOUGHT and divergence == "Bearish"

        if not (long_signal or short_signal):
            return None

        signal_type = "LONG" if long_signal else "SHORT"
        score       = sum([vwap_stretched, volume_ok, divergence != "None"])
        atr         = float((df["High"] - df["Low"]).iloc[-14:].mean())
        stop        = round(price - atr if long_signal else price + atr, 2)
        now         = datetime.now(EST)

        return {
            "timestamp":      now.strftime("%Y-%m-%d %H:%M:%S EST"),
            "scan_date":      now.strftime("%Y-%m-%d"),
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
            "target":         round(vwap_val, 2),
            "status":         "New",
        }

    except Exception as e:
        log.error(f"{symbol}: {e}")
        return None


# ── HTML ──────────────────────────────────────────────────────────────────────

def generate_html(results: list[dict], stats: dict) -> None:
    now  = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")
    rows = ""
    for r in results:
        sig_class  = "long" if r["signal"] == "LONG" else "short"
        div_class  = "bullish" if r["divergence"] == "Bullish" else ("bearish" if r["divergence"] == "Bearish" else "")
        stars      = "★" * r["strength"] + "☆" * (3 - r["strength"])
        vol_flag   = "🔥" if r["vol_ratio"] >= VOLUME_MULTIPLIER else ""
        pct_class  = "neg" if r["pct_from_vwap"] < 0 else "pos"
        rows += f"""
        <tr class="{sig_class}" data-signal="{r['signal']}" data-strength="{r['strength']}" data-symbol="{r['symbol']}">
            <td>{r['timestamp']}</td>
            <td><strong>{r['symbol']}</strong></td>
            <td>${r['price']}</td>
            <td>${r['vwap']}</td>
            <td class="{pct_class}">{r['pct_from_vwap']:+.2f}%</td>
            <td>{r['rsi']}</td>
            <td class="{div_class}">{r['divergence']}</td>
            <td>{r['volume']:,} {vol_flag}</td>
            <td>{r['vol_ratio']}×</td>
            <td><span class="signal-badge {sig_class}">{r['signal']}</span></td>
            <td class="strength">{stars}</td>
            <td>${r['stop_loss']}</td>
            <td>${r['target']}</td>
            <td><span class="status-badge">{r['status']}</span></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="120">
<title>VWAP + RSI Signal Scanner</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}}
  header{{background:linear-gradient(135deg,#1f2937,#111827);padding:18px 28px;border-bottom:1px solid #30363d;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}}
  header h1{{font-size:1.3rem;color:#f0f6fc}}
  .live-dot{{display:inline-block;width:8px;height:8px;background:#3fb950;border-radius:50%;margin-right:6px;animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
  .meta{{font-size:0.78rem;color:#8b949e}}

  .stats{{display:flex;gap:14px;padding:14px 28px;background:#161b22;border-bottom:1px solid #30363d;flex-wrap:wrap}}
  .stat{{background:#1f2937;border-radius:8px;padding:10px 18px;min-width:120px}}
  .stat .label{{font-size:0.68rem;color:#8b949e;text-transform:uppercase;letter-spacing:.05em}}
  .stat .value{{font-size:1.3rem;font-weight:700;color:#f0f6fc;margin-top:3px}}
  .stat .value.green{{color:#3fb950}}.stat .value.red{{color:#f85149}}.stat .value.gold{{color:#e3b341}}

  .toolbar{{padding:12px 28px;display:flex;gap:10px;align-items:center;background:#0d1117;flex-wrap:wrap;border-bottom:1px solid #21262d}}
  .toolbar input,.toolbar select{{background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-radius:6px;padding:6px 10px;font-size:0.82rem}}
  .toolbar input{{width:220px}}.toolbar input:focus,.toolbar select:focus{{outline:none;border-color:#58a6ff}}
  .toolbar button{{background:#238636;color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:0.82rem}}
  .toolbar button:hover{{background:#2ea043}}
  .toolbar button.sec{{background:#1f2937;border:1px solid #30363d}}
  .toolbar button.sec:hover{{background:#30363d}}
  .count{{margin-left:auto;font-size:0.78rem;color:#8b949e}}

  .table-wrap{{overflow-x:auto;padding:0 28px 28px}}
  table{{width:100%;border-collapse:collapse;font-size:0.8rem;margin-top:14px}}
  thead tr{{background:#161b22;position:sticky;top:0;z-index:1}}
  thead th{{padding:9px 10px;text-align:left;color:#8b949e;font-weight:600;border-bottom:2px solid #30363d;white-space:nowrap;cursor:pointer;user-select:none}}
  thead th:hover{{color:#58a6ff}}
  tbody tr{{border-bottom:1px solid #21262d;transition:background .12s}}
  tbody tr:hover{{background:#1c2128}}
  tbody td{{padding:8px 10px;white-space:nowrap}}
  tr.long{{border-left:3px solid #3fb950}}tr.short{{border-left:3px solid #f85149}}
  .signal-badge{{font-weight:700;font-size:0.72rem;padding:2px 7px;border-radius:4px}}
  .signal-badge.long{{background:#1a4731;color:#3fb950}}.signal-badge.short{{background:#4d1d1d;color:#f85149}}
  .status-badge{{background:#1f3a5f;color:#58a6ff;border-radius:4px;padding:2px 7px;font-size:0.72rem}}
  .bullish{{color:#3fb950;font-weight:600}}.bearish{{color:#f85149;font-weight:600}}
  .pos{{color:#3fb950}}.neg{{color:#f85149}}
  .strength{{letter-spacing:2px;color:#e3b341}}
  .empty{{text-align:center;padding:60px;color:#8b949e}}
  footer{{text-align:center;font-size:0.72rem;color:#8b949e;padding:12px;border-top:1px solid #21262d}}

  .db-badge{{background:#1f3a5f;color:#58a6ff;border-radius:4px;padding:2px 8px;font-size:0.7rem;margin-left:8px}}
</style>
</head>
<body>

<header>
  <div>
    <h1><span class="live-dot"></span>VWAP + RSI Signal Scanner <span class="db-badge">SQLite</span></h1>
    <div class="meta">Checks every ~2 mins · 9 AM – 6 PM EST · Auto-refreshes every 2 mins</div>
  </div>
  <div class="meta">Last scan: <strong>{now}</strong></div>
</header>

<div class="stats">
  <div class="stat"><div class="label">Today's Signals</div><div class="value">{stats['today']}</div></div>
  <div class="stat"><div class="label">Today Long</div><div class="value green">{stats['long_today']}</div></div>
  <div class="stat"><div class="label">Today Short</div><div class="value red">{stats['short_today']}</div></div>
  <div class="stat"><div class="label">High Strength ★★★</div><div class="value gold">{stats['high_strength']}</div></div>
  <div class="stat"><div class="label">All-Time Total</div><div class="value">{stats['total']}</div></div>
  <div class="stat"><div class="label">Top Symbol (7d)</div><div class="value" style="font-size:0.95rem">{stats['top_symbol']}</div></div>
  <div class="stat"><div class="label">Symbols Watched</div><div class="value">{len(load_tickers())}</div></div>
</div>

<div class="toolbar">
  <input type="text" id="filterInput" placeholder="🔍 Filter symbol…" oninput="filterTable()">
  <select id="signalFilter" onchange="filterTable()">
    <option value="">All Signals</option>
    <option value="LONG">LONG</option>
    <option value="SHORT">SHORT</option>
  </select>
  <select id="strengthFilter" onchange="filterTable()">
    <option value="">All Strengths</option>
    <option value="3">★★★ High</option>
    <option value="2">★★ Medium</option>
    <option value="1">★ Low</option>
  </select>
  <button onclick="window.location.reload()">↻ Refresh</button>
  <button class="sec" onclick="exportCSV()">⬇ Export CSV</button>
  <span class="count" id="rowCount"></span>
</div>

<div class="table-wrap">
  {'<p class="empty">⏳ No signals yet. Scanner will populate this table during market hours.</p>' if not results else f"""
  <table id="signalTable">
    <thead><tr>
      <th>⏱ Timestamp</th><th>Symbol</th><th>Price</th><th>VWAP</th>
      <th>% from VWAP</th><th>RSI({RSI_PERIOD})</th><th>RSI Divergence</th>
      <th>Volume</th><th>Vol Ratio</th><th>Signal</th><th>Strength</th>
      <th>Stop Loss</th><th>Target</th><th>Status</th>
    </tr></thead>
    <tbody id="tableBody">{rows}</tbody>
  </table>"""}
</div>

<footer>
  Data stored in SQLite · Page auto-refreshes every 2 min · Scanner runs Mon–Fri 9 AM – 6 PM EST ·
  ⚠️ For informational purposes only — not financial advice
</footer>

<script>
function filterTable(){{
  const text = document.getElementById('filterInput').value.toLowerCase();
  const sig  = document.getElementById('signalFilter').value;
  const str  = document.getElementById('strengthFilter').value;
  const rows = document.querySelectorAll('#tableBody tr');
  let vis = 0;
  rows.forEach(r=>{{
    const sym    = (r.dataset.symbol||'').toLowerCase();
    const signal = r.dataset.signal||'';
    const st     = r.dataset.strength||'';
    const show   = sym.includes(text) && (!sig||signal===sig) && (!str||st===str);
    r.style.display = show?'':'none';
    if(show) vis++;
  }});
  const el = document.getElementById('rowCount');
  if(el) el.innerText = vis+' signal(s) shown';
}}

function exportCSV(){{
  const rows = [...document.querySelectorAll('#signalTable tr')].filter(r=>r.style.display!=='none');
  const csv  = rows.map(r=>[...r.querySelectorAll('th,td')].map(c=>'"'+c.innerText.replace(/"/g,'""')+'"').join(',')).join('\\n');
  const a    = document.createElement('a');
  a.href     = 'data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download = 'vwap_signals_'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
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
        log.warning("Email not configured — skipping")
        return
    if not new_signals:
        return

    subject = f"🚨 VWAP Scanner: {len(new_signals)} New Signal(s) — {datetime.now(EST).strftime('%H:%M EST')}"
    rows = ""
    for r in new_signals:
        color = "#3fb950" if r["signal"] == "LONG" else "#f85149"
        rows += f"""<tr>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['timestamp']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d"><b>{r['symbol']}</b></td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['price']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['vwap']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['pct_from_vwap']:+.2f}%</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['rsi']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{r['divergence']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d;color:{color}"><b>{r['signal']}</b></td>
          <td style="padding:7px;border-bottom:1px solid #30363d">{"★"*r['strength']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['stop_loss']}</td>
          <td style="padding:7px;border-bottom:1px solid #30363d">${r['target']}</td>
        </tr>"""

    body = f"""<html><body style="font-family:Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px">
    <h2 style="color:#f0f6fc">📊 VWAP + RSI Scanner Alert</h2>
    <p style="color:#8b949e">{len(new_signals)} new signal(s) — {datetime.now(EST).strftime('%Y-%m-%d %H:%M EST')}</p>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:14px">
      <thead><tr style="background:#161b22">
        <th style="padding:7px;text-align:left;color:#8b949e">Time</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Symbol</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Price</th>
        <th style="padding:7px;text-align:left;color:#8b949e">VWAP</th>
        <th style="padding:7px;text-align:left;color:#8b949e">% Dev</th>
        <th style="padding:7px;text-align:left;color:#8b949e">RSI</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Divergence</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Signal</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Strength</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Stop</th>
        <th style="padding:7px;text-align:left;color:#8b949e">Target</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="margin-top:16px;font-size:11px;color:#8b949e">⚠️ Informational only. Not financial advice.</p>
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
        log.info(f"Email sent → {EMAIL_RECEIVER}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("VWAP + RSI Scanner starting")

    init_db()

    if not is_market_hours():
        now = datetime.now(EST)
        log.info(f"Outside market hours ({now.strftime('%H:%M EST')} {now.strftime('%A')}). Regenerating HTML only.")
        with get_db() as conn:
            results = fetch_signals(conn)
            stats   = fetch_stats(conn)
        generate_html(results, stats)
        return

    tickers = load_tickers()
    log.info(f"Scanning {len(tickers)} tickers: {', '.join(tickers)}")

    truly_new = []
    window_start = (datetime.now(EST) - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S EST")

    with get_db() as conn:
        for symbol in tickers:
            log.info(f"Analysing {symbol}…")
            result = analyse_ticker(symbol)
            if not result:
                log.info(f"  — No signal")
                continue

            if is_duplicate(conn, symbol, result["signal"], window_start):
                log.info(f"  ⏭ Duplicate skipped: {symbol} {result['signal']}")
                continue

            insert_signal(conn, result)
            truly_new.append(result)
            log.info(f"  ✅ {result['signal']} | RSI={result['rsi']} | "
                     f"VWAP dev={result['pct_from_vwap']}% | Strength={result['strength']} | DB id saved")

        results = fetch_signals(conn)
        stats   = fetch_stats(conn)

    generate_html(results, stats)

    if truly_new:
        log.info(f"📧 Sending email for {len(truly_new)} new signal(s)")
        send_email(truly_new)
    else:
        log.info("No new unique signals this run.")

    log.info("Scan complete.")


if __name__ == "__main__":
    main()

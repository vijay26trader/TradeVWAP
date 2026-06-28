#!/usr/bin/env python3
"""
TradeVWAP - Flask Dashboard
Serves live signal data directly from SQLite.
No redeploy needed — reads DB on every request.
"""

import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

EST      = ZoneInfo("America/New_York")
DB_FILE  = os.environ.get("DB_PATH", "docs/signals.db")
RSI_PERIOD = 14

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def db_exists() -> bool:
    return os.path.exists(DB_FILE)


def fetch_signals(days: int = 30, symbol: str = "", signal: str = "", strength: str = "") -> list[dict]:
    if not db_exists():
        return []
    since = (datetime.now(EST) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = "SELECT * FROM signals WHERE scan_date >= ?"
    params = [since]
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol.upper())
    if signal:
        query += " AND signal = ?"
        params.append(signal.upper())
    if strength:
        query += " AND strength = ?"
        params.append(int(strength))
    query += " ORDER BY id DESC LIMIT 1000"
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fetch_stats() -> dict:
    if not db_exists():
        return {k: 0 for k in ["today", "total", "long_today", "short_today", "high_strength", "top_symbol", "watched"]}
    today = datetime.now(EST).strftime("%Y-%m-%d")
    conn  = get_db()

    def scalar(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    stats = {
        "today":         scalar("SELECT COUNT(*) FROM signals WHERE scan_date=?", (today,)),
        "total":         scalar("SELECT COUNT(*) FROM signals"),
        "long_today":    scalar("SELECT COUNT(*) FROM signals WHERE signal='LONG'  AND scan_date=?", (today,)),
        "short_today":   scalar("SELECT COUNT(*) FROM signals WHERE signal='SHORT' AND scan_date=?", (today,)),
        "high_strength": scalar("SELECT COUNT(*) FROM signals WHERE strength=3 AND scan_date=?", (today,)),
    }
    row = conn.execute("""
        SELECT symbol, COUNT(*) as cnt FROM signals
        WHERE scan_date >= date('now','-7 days')
        GROUP BY symbol ORDER BY cnt DESC LIMIT 1
    """).fetchone()
    stats["top_symbol"] = f"{row['symbol']} ({row['cnt']})" if row else "—"

    last = conn.execute("SELECT timestamp FROM signals ORDER BY id DESC LIMIT 1").fetchone()
    stats["last_scan"] = last["timestamp"] if last else "No scans yet"

    conn.close()
    return stats


def fetch_symbols() -> list[str]:
    if not db_exists():
        return []
    conn  = get_db()
    rows  = conn.execute("SELECT DISTINCT symbol FROM signals ORDER BY symbol").fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    symbol   = request.args.get("symbol", "")
    signal   = request.args.get("signal", "")
    strength = request.args.get("strength", "")
    days     = int(request.args.get("days", 30))

    results  = fetch_signals(days=days, symbol=symbol, signal=signal, strength=strength)
    stats    = fetch_stats()
    symbols  = fetch_symbols()
    now      = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")

    return render_template("dashboard.html",
        results=results,
        stats=stats,
        symbols=symbols,
        now=now,
        rsi_period=RSI_PERIOD,
        filters={"symbol": symbol, "signal": signal, "strength": strength, "days": days}
    )


@app.route("/api/signals")
def api_signals():
    """JSON endpoint for raw signal data."""
    symbol   = request.args.get("symbol", "")
    signal   = request.args.get("signal", "")
    strength = request.args.get("strength", "")
    days     = int(request.args.get("days", 7))
    results  = fetch_signals(days=days, symbol=symbol, signal=signal, strength=strength)
    return jsonify({"count": len(results), "signals": results})


@app.route("/api/stats")
def api_stats():
    """JSON endpoint for dashboard stats."""
    return jsonify(fetch_stats())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "db": db_exists(), "time": datetime.now(EST).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

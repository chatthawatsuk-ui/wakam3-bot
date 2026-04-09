"""
🔍 Signal Scanner — หัวหน้าที่รับ reports จาก 3 Pine Specialists
แล้วชั่งน้ำหนัก → SIGNAL / WATCH / IDLE → ส่งต่อให้ Paper Trader

Flow:
  🎯 agent_trend  ──┐
  🏦 agent_smc    ──┼──▶ 🔍 Signal Scanner ──▶ 🤖 Paper Trader
  📈 agent_osc    ──┘
"""
import os, json, sqlite3
from datetime import datetime, timezone

import agent_trend as TREND
import agent_smc   as SMC
import agent_osc   as OSC

WEIGHTS_PATH = "weights.json"
DB_PATH      = "paper_trades.db"
MIN_SCORE    = 8
TP1_R        = 1.2
TP2_R        = 2.0


# ══════════════════════════════════════════════════════════════
# DYNAMIC WEIGHTS — อ่านจาก weights.json ที่ generate_dashboard สร้าง
# ══════════════════════════════════════════════════════════════
def load_weights():
    """อ่าน dynamic weights — fallback equal ถ้าไม่มีหรือยัง locked"""
    try:
        if os.path.exists(WEIGHTS_PATH):
            with open(WEIGHTS_PATH) as f:
                w = json.load(f)
            if not w.get("locked", True):
                return float(w["trend"]), float(w["smc"]), float(w["osc"])
    except Exception:
        pass
    return 1/3, 1/3, 1/3


W_TREND, W_SMC, W_OSC = load_weights()


def _weighted_score(trend_s, smc_s, osc_s, kz=False):
    """
    normalize แต่ละ specialist (÷ max) → weighted sum → ×30 + KZ
    max score = 31 เสมอ ไม่ว่า weights จะเป็นเท่าไหร่
    """
    combined = (trend_s / 11) * W_TREND + \
               (smc_s   / 10) * W_SMC   + \
               (osc_s   /  9) * W_OSC
    return round(combined * 30) + (1 if kz else 0)


# ══════════════════════════════════════════════════════════════
# SCAN SINGLE SYMBOL
# ══════════════════════════════════════════════════════════════
def scan_symbol(sym, df_1h, df_4h, market_type="FUTURES"):
    """
    เรียก 3 specialists → รวม weighted score → คืน (signal|None, scan_result)

    Trend Agent   → HTF bias + CDC score
    SMC Agent     → BOS/CHoCH/QM/Zone score + swing levels สำหรับ SL
    Osc Agent     → RSI Div/Stoch/MACD score + Kill Zone + RSI value
    """
    ts = datetime.now(timezone.utc).isoformat()

    _err = {"symbol": sym, "status": "ERROR", "market_type": market_type,
            "best_score": 0, "score_long": 0, "score_short": 0,
            "score_trend": 0, "score_smc": 0, "score_osc": 0,
            "rsi": 0, "price": 0, "htf_bull": False, "in_kz": False,
            "in_discount": False, "trail_bull": False, "ts": ts}

    # ── รับ reports จาก 3 specialists ─────────────────────────
    try: t_rep = TREND.run(df_1h, df_4h)
    except Exception: t_rep = None

    try: s_rep = SMC.run(df_1h, df_4h)
    except Exception: s_rep = None

    try: o_rep = OSC.run(df_1h, df_4h)
    except Exception: o_rep = None

    if not t_rep or not s_rep or not o_rep:
        return None, _err

    # ── HTF filter — Trend Agent ตัดสิน ──────────────────────
    htf_bull = t_rep["htf_bull"]
    htf_sma  = t_rep["htf_sma"]
    kz       = o_rep["kz"]

    # ── คะแนนแต่ละ specialist (Trend Agent กรอง HTF เองแล้ว) ──
    trend_l = t_rep["score_long"]
    trend_s = t_rep["score_short"]
    smc_l   = s_rep["score_long"]
    smc_s_  = s_rep["score_short"]
    osc_l   = o_rep["score_long"]
    osc_s_  = o_rep["score_short"]

    # ── Signal Scanner ชั่งน้ำหนัก → total score ─────────────
    sl = _weighted_score(trend_l, smc_l,  osc_l,  kz)
    ss = _weighted_score(trend_s, smc_s_, osc_s_, kz)

    best      = max(sl, ss)
    best_side = "LONG" if sl >= ss else "SHORT"
    b_trend   = trend_l if best_side == "LONG" else trend_s
    b_smc     = smc_l   if best_side == "LONG" else smc_s_
    b_osc     = osc_l   if best_side == "LONG" else osc_s_

    status = "SIGNAL" if best >= MIN_SCORE else ("WATCH" if best >= 5 else "IDLE")
    px     = float(df_1h.iloc[-1]["close"])

    scan_result = {
        "symbol":      sym,
        "market_type": market_type,
        "status":      status,
        "side":        best_side,
        "score_long":  sl,
        "score_short": ss,
        "best_score":  best,
        "score_pct":   round(best / 31 * 100, 1),
        "price":       round(px, 4),
        "rsi":         o_rep["rsi"],
        "htf_bull":    htf_bull,
        "in_kz":       kz,
        "score_trend": b_trend,
        "score_smc":   b_smc,
        "score_osc":   b_osc,
        "in_discount": s_rep["details"].get("in_discount", False),
        "trail_bull":  t_rep["details"].get("trail_slow_bull", False),
        "ts":          ts,
    }

    if best < MIN_SCORE:
        return None, scan_result

    # ── คำนวณ SL/TP (SMC swing levels + Trend ATR) ────────────
    atr  = t_rep["atr"]
    ep   = px
    side = best_side

    sl_p = min(s_rep["swing_low"],  ep - atr) if side == "LONG" \
      else max(s_rep["swing_high"], ep + atr)

    dist = abs(ep - sl_p)
    if dist < ep * 0.001:
        return None, scan_result   # SL ใกล้เกินไป

    tp1 = ep + dist * TP1_R if side == "LONG" else ep - dist * TP1_R
    tp2 = ep + dist * TP2_R if side == "LONG" else ep - dist * TP2_R

    signal = {
        "symbol":      sym,
        "side":        side,
        "score":       sl if side == "LONG" else ss,
        "price":       round(ep,   4),
        "sl":          round(sl_p, 4),
        "tp1":         round(tp1,  4),
        "tp2":         round(tp2,  4),
        "sl_pct":      round(dist / ep * 100, 3),
        "rsi":         o_rep["rsi"],
        "in_kz":       kz,
        "score_trend": b_trend,
        "score_smc":   b_smc,
        "score_osc":   b_osc,
        "ts":          ts,
    }
    return signal, scan_result


# ══════════════════════════════════════════════════════════════
# SPECIALIST HISTORY — บันทึกลง SQLite ทุก scan (Level 1)
# ══════════════════════════════════════════════════════════════
def save_specialist_history(scan_results):
    """เก็บ specialist scores ทุก scan — rolling 7 วัน"""
    if not scan_results:
        return
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS specialist_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_ts     TEXT    NOT NULL,
                symbol      TEXT    NOT NULL,
                status      TEXT,
                side        TEXT,
                best_score  INTEGER DEFAULT 0,
                score_trend INTEGER DEFAULT 0,
                score_smc   INTEGER DEFAULT 0,
                score_osc   INTEGER DEFAULT 0,
                rsi         REAL    DEFAULT 0,
                htf_bull    INTEGER DEFAULT 0,
                in_discount INTEGER DEFAULT 0,
                trail_bull  INTEGER DEFAULT 0
            )
        """)
        scan_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        rows = [
            (scan_ts, r.get("symbol",""), r.get("status",""), r.get("side",""),
             r.get("best_score",0), r.get("score_trend",0), r.get("score_smc",0),
             r.get("score_osc",0),  r.get("rsi",0),
             int(r.get("htf_bull",False)), int(r.get("in_discount",False)),
             int(r.get("trail_bull",False)))
            for r in scan_results
        ]
        cur.executemany("""
            INSERT INTO specialist_history
              (scan_ts,symbol,status,side,best_score,
               score_trend,score_smc,score_osc,
               rsi,htf_bull,in_discount,trail_bull)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        cur.execute("DELETE FROM specialist_history WHERE scan_ts < datetime('now','-7 days')")
        con.commit()
        con.close()
    except Exception as e:
        print(f"[ERR] save_specialist_history: {e}")

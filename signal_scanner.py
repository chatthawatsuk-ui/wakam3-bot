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

import numpy as np
from ta.trend      import ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange

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

    # ── Level 5: ตรวจ Market Regime ──────────────────────────
    regime = _detect_regime(df_1h)

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
        "regime":      regime,
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
        "regime":      regime,
        "score_trend": b_trend,
        "score_smc":   b_smc,
        "score_osc":   b_osc,
        "ts":          ts,
    }

    # ── Level 4: บันทึก conditions ที่ active ตอน signal ยิง ──
    save_condition_snapshot(sym, side, regime, ts, t_rep, s_rep, o_rep)

    return signal, scan_result


# ══════════════════════════════════════════════════════════════
# LEVEL 5 — MARKET REGIME DETECTION
# ⚠️  READ-ONLY — ใช้บันทึกข้อมูลและ Report เท่านั้น
#     การปรับ weights ตาม regime จะไม่เกิดอัตโนมัติ
#     ต้องรอ Weekly Report (ทุกวันจันทร์) + คอนเฟิมก่อนทุกครั้ง
# ══════════════════════════════════════════════════════════════
def _detect_regime(df_1h):
    """
    TRENDING : ADX(14) > 25
    VOLATILE : ATR ปัจจุบัน > 1.5× ค่าเฉลี่ย 48 แท่ง
    RANGING  : อื่นๆ (ADX ต่ำ + ATR ปกติ)
    """
    try:
        h, l, c = df_1h["high"], df_1h["low"], df_1h["close"]
        adx_val  = ADXIndicator(h, l, c, 14).adx().iloc[-1]
        atr_s    = AverageTrueRange(h, l, c, 14).average_true_range()
        atr_now  = atr_s.iloc[-1]
        atr_avg  = atr_s.rolling(48).mean().iloc[-1]
        atr_ratio = atr_now / atr_avg if atr_avg > 0 else 1.0
        if atr_ratio > 1.5:
            return "VOLATILE"
        if adx_val > 25:
            return "TRENDING"
        return "RANGING"
    except Exception:
        return "UNKNOWN"


# ══════════════════════════════════════════════════════════════
# LEVEL 4 — CONDITION SNAPSHOT (บันทึกทุก condition ที่ fire พร้อม regime)
# ⚠️  READ-ONLY — ใช้บันทึกข้อมูลและ Report เท่านั้น
#     การปรับ point ต่อ condition จะไม่เกิดอัตโนมัติ
#     ต้องรอ Weekly Report (ทุกวันจันทร์) + คอนเฟิมก่อนทุกครั้ง
# ══════════════════════════════════════════════════════════════
def save_condition_snapshot(sym, side, regime, signal_ts, t_rep, s_rep, o_rep):
    """
    บันทึก conditions ทุกตัวที่ active ตอน signal ยิง
    พร้อม regime → ใช้ใน weekly_report.py เพื่อคำนวณ win rate ต่อ condition
    """
    try:
        td = t_rep.get("details", {})
        sd = s_rep.get("details", {})
        od = o_rep.get("details", {})

        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS condition_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_ts     TEXT    NOT NULL,
                symbol        TEXT    NOT NULL,
                side          TEXT    NOT NULL,
                regime        TEXT    NOT NULL,
                -- 🎯 Trend Agent conditions
                cdc_bull        INTEGER DEFAULT 0,
                cross_up        INTEGER DEFAULT 0,
                cross_dn        INTEGER DEFAULT 0,
                touch_bull      INTEGER DEFAULT 0,
                touch_bear      INTEGER DEFAULT 0,
                above_sma50     INTEGER DEFAULT 0,
                above_sma200    INTEGER DEFAULT 0,
                sma50_gt_200    INTEGER DEFAULT 0,
                trail_slow_bull INTEGER DEFAULT 0,
                -- 🏦 SMC Agent conditions
                bos_bull        INTEGER DEFAULT 0,
                bos_bear        INTEGER DEFAULT 0,
                choch_bull      INTEGER DEFAULT 0,
                choch_bear      INTEGER DEFAULT 0,
                qm_bull         INTEGER DEFAULT 0,
                qm_bear         INTEGER DEFAULT 0,
                in_discount     INTEGER DEFAULT 0,
                in_premium      INTEGER DEFAULT 0,
                in_eq           INTEGER DEFAULT 0,
                -- 📈 Osc Agent conditions
                rsi_bull_div    INTEGER DEFAULT 0,
                rsi_bear_div    INTEGER DEFAULT 0,
                rsi_os          INTEGER DEFAULT 0,
                rsi_ob          INTEGER DEFAULT 0,
                st_up           INTEGER DEFAULT 0,
                st_dn           INTEGER DEFAULT 0,
                macd_up         INTEGER DEFAULT 0,
                macd_dn         INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            INSERT INTO condition_snapshots (
                signal_ts, symbol, side, regime,
                cdc_bull, cross_up, cross_dn, touch_bull, touch_bear,
                above_sma50, above_sma200, sma50_gt_200, trail_slow_bull,
                bos_bull, bos_bear, choch_bull, choch_bear,
                qm_bull, qm_bear, in_discount, in_premium, in_eq,
                rsi_bull_div, rsi_bear_div, rsi_os, rsi_ob,
                st_up, st_dn, macd_up, macd_dn
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            signal_ts, sym, side, regime,
            int(td.get("cdc_bull",        False)),
            int(td.get("cross_up",         False)),
            int(td.get("cross_dn",         False)),
            int(td.get("touch_bull",       False)),
            int(td.get("touch_bear",       False)),
            int(td.get("above_sma50",      False)),
            int(td.get("above_sma200",     False)),
            int(td.get("sma50_gt_200",     False)),
            int(td.get("trail_slow_bull",  False)),
            int(sd.get("bos_bull",         False)),
            int(sd.get("bos_bear",         False)),
            int(sd.get("choch_bull",       False)),
            int(sd.get("choch_bear",       False)),
            int(sd.get("qm_bull",          False)),
            int(sd.get("qm_bear",          False)),
            int(sd.get("in_discount",      False)),
            int(sd.get("in_premium",       False)),
            int(sd.get("in_eq",            False)),
            int(od.get("rsi_bull_div",     False)),
            int(od.get("rsi_bear_div",     False)),
            int(od.get("rsi_os",           False)),
            int(od.get("rsi_ob",           False)),
            int(od.get("st_up",            False)),
            int(od.get("st_dn",            False)),
            int(od.get("macd_up",          False)),
            int(od.get("macd_dn",          False)),
        ))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[ERR] save_condition_snapshot: {e}")


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

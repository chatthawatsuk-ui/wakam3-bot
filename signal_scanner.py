"""
🔍 Signal Scanner — หัวหน้าที่รับ reports จาก 5 Specialists
แล้วชั่งน้ำหนัก → SIGNAL / WATCH / IDLE → ส่งต่อให้ Paper Trader

Flow:
  🎯 agent_trend     ──┐
  🏦 agent_smc       ──┤
  📈 agent_osc       ──┼──▶ 🔍 Signal Scanner ──▶ 🤖 Paper Trader
  💧 agent_liquidity ──┤
  💰 agent_funding   ──┘

Score System:
  Core (Trend+SMC+Osc) normalized → /31
  Liquidity bonus: up to +8 direct pts
  Funding bonus:   up to +6 direct pts
  Total MAX = 45

DISABLE_FUNDING = True  → ข้าม agent_funding (ใช้ใน backtest ที่ไม่มี live funding data)
"""
import os, json, sqlite3
from datetime import datetime, timezone

import numpy as np
from ta.trend      import ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange

import agent_trend     as TREND
import agent_smc       as SMC
import agent_osc       as OSC
import agent_liquidity as LIQUIDITY
import agent_funding   as FUNDING

WEIGHTS_PATH    = "weights.json"
DB_PATH         = "paper_trades.db"
MIN_SCORE       = 9    # total score (core + liq bonus + fund bonus)
TP1_R           = 1.2
TP2_R           = 2.0
DISABLE_FUNDING = False  # set True ใน backtest เพื่อ skip live API call
SCORE_MAX       = 45    # Trend(13)+SMC(10)+Osc(11) normalized→31 + Liq(8) + Fund(6)


# ══════════════════════════════════════════════════════════════
# DYNAMIC WEIGHTS — อ่านจาก weights.json ที่ generate_dashboard สร้าง
# ══════════════════════════════════════════════════════════════
def load_weights():
    """
    อ่าน dynamic weights — fallback equal ถ้าไม่มีหรือยัง locked
    คืน (W_TREND, W_SMC, W_OSC) สำหรับ core normalization
    Liquidity และ Funding ใช้ direct bonus (ไม่ normalize)
    """
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


def _fmt_price(px):
    """
    round ราคาให้เหมาะกับขนาด — ป้องกัน PEPE/SHIB/FLR กลายเป็น 0.0
    """
    if px <= 0:
        return 0.0
    if px < 0.0001:
        return round(px, 10)
    if px < 0.001:
        return round(px, 8)
    if px < 0.1:
        return round(px, 6)
    if px < 10:
        return round(px, 4)
    return round(px, 2)


def _weighted_score(trend_s, smc_s, osc_s, liq_s=0, fund_s=0):
    """
    Core 3 agents: normalize (÷ new MAX_SCORE) → weighted sum → ×31
      Trend MAX = 13 (ปรับจาก 11 → เพิ่ม ADX + BB squeeze)
      SMC   MAX = 10 (เดิม)
      Osc   MAX = 11 (ปรับจาก 9 → เพิ่ม OBV SMA20 + SMA50)

    Bonus agents (direct add, ไม่ normalize):
      Liquidity: up to +8  → total MAX = 31 + 8 = 39
      Funding:   up to +6  → total MAX = 45

    KZ ไม่ใช่ bonus แล้ว — ใช้เป็น context ใน claude_filter แทน
    """
    combined = (trend_s / TREND.MAX_SCORE) * W_TREND + \
               (smc_s   / SMC.MAX_SCORE)   * W_SMC   + \
               (osc_s   / OSC.MAX_SCORE)   * W_OSC
    core = round(combined * 31)
    return core + int(liq_s) + int(fund_s)


# ══════════════════════════════════════════════════════════════
# SCAN SINGLE SYMBOL
# ══════════════════════════════════════════════════════════════
def scan_symbol(sym, df_1h, df_4h, market_type="FUTURES", df_1d=None):
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

    # ── รับ reports จาก 5 specialists ─────────────────────────
    try: t_rep = TREND.run(df_1h, df_4h, df_1d)
    except Exception: t_rep = None

    try: s_rep = SMC.run(df_1h, df_4h)
    except Exception: s_rep = None

    try: o_rep = OSC.run(df_1h, df_4h)
    except Exception: o_rep = None

    try: l_rep = LIQUIDITY.run(df_1h, df_4h)
    except Exception: l_rep = None

    # Funding: live API — ข้ามถ้า DISABLE_FUNDING หรือ error
    f_rep = None
    if not DISABLE_FUNDING:
        try: f_rep = FUNDING.run(sym)
        except Exception: f_rep = None

    if not t_rep or not s_rep or not o_rep:
        return None, _err

    # ── Level 5: ตรวจ Market Regime ──────────────────────────
    regime = _detect_regime(df_1h)

    # ── HTF filter — Trend Agent ตัดสิน ──────────────────────
    htf_bull = t_rep["htf_bull"]
    htf_sma  = t_rep["htf_sma"]
    kz       = o_rep["kz"]

    # ── คะแนนแต่ละ specialist ──────────────────────────────────
    trend_l = t_rep["score_long"]
    trend_s = t_rep["score_short"]
    smc_l   = s_rep["score_long"]
    smc_s_  = s_rep["score_short"]
    osc_l   = o_rep["score_long"]
    osc_s_  = o_rep["score_short"]
    liq_l   = l_rep["score_long"]  if l_rep else 0
    liq_s_  = l_rep["score_short"] if l_rep else 0
    fund_l  = f_rep["score_long"]  if f_rep else 0
    fund_s_ = f_rep["score_short"] if f_rep else 0

    # ── Signal Scanner ชั่งน้ำหนัก → total score (max 45) ───────
    sl = _weighted_score(trend_l, smc_l,  osc_l,  liq_l,  fund_l)
    ss = _weighted_score(trend_s, smc_s_, osc_s_, liq_s_, fund_s_)

    best      = max(sl, ss)
    best_side = "LONG" if sl >= ss else "SHORT"
    b_trend   = trend_l if best_side == "LONG" else trend_s
    b_smc     = smc_l   if best_side == "LONG" else smc_s_
    b_osc     = osc_l   if best_side == "LONG" else osc_s_
    b_liq     = liq_l   if best_side == "LONG" else liq_s_
    b_fund    = fund_l  if best_side == "LONG" else fund_s_

    status = "SIGNAL" if best >= MIN_SCORE else ("WATCH" if best >= 5 else "IDLE")
    px     = float(df_1h.iloc[-1]["close"])

    # Funding rate สำหรับ display / claude_filter
    funding_rate = f_rep.get("funding_rate") if f_rep else None

    scan_result = {
        "symbol":       sym,
        "market_type":  market_type,
        "status":       status,
        "side":         best_side,
        "score_long":   sl,
        "score_short":  ss,
        "best_score":   best,
        "score_pct":    round(best / SCORE_MAX * 100, 1),
        "price":        _fmt_price(px),
        "rsi":          o_rep["rsi"],
        "htf_bull":     htf_bull,
        "in_kz":        kz,
        "score_trend":  b_trend,
        "score_smc":    b_smc,
        "score_osc":    b_osc,
        "score_liq":    b_liq,
        "score_fund":   b_fund,
        "in_discount":  s_rep["details"].get("in_discount", False),
        "trail_bull":   t_rep["details"].get("trail_slow_bull", False),
        "bull_sweep":   l_rep["bull_sweep"] if l_rep else False,
        "bear_sweep":   l_rep["bear_sweep"] if l_rep else False,
        "funding_rate": funding_rate,
        "regime":       regime,
        "ts":           ts,
    }

    if best < MIN_SCORE:
        return None, scan_result

    # ── Funding Hard Reject (ก่อนส่ง Claude) ─────────────────
    if f_rep and f_rep.get("available"):
        if best_side == "LONG" and f_rep.get("hard_reject_long"):
            fr = f_rep.get("funding_rate", "?")
            scan_result["status"]        = "FUNDING_REJECT"
            scan_result["claude_reason"] = f"Funding {fr}% > +0.15% — LONG over-crowded"
            print(f"  🚫 [FUNDING] REJECT {sym} LONG — funding {fr}%")
            return None, scan_result
        if best_side == "SHORT" and f_rep.get("hard_reject_short"):
            fr = f_rep.get("funding_rate", "?")
            scan_result["status"]        = "FUNDING_REJECT"
            scan_result["claude_reason"] = f"Funding {fr}% < -0.05% — SHORT over-crowded"
            print(f"  🚫 [FUNDING] REJECT {sym} SHORT — funding {fr}%")
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
        "symbol":       sym,
        "side":         side,
        "score":        sl if side == "LONG" else ss,
        "price":        _fmt_price(ep),
        "sl":           _fmt_price(sl_p),
        "tp1":          _fmt_price(tp1),
        "tp2":          _fmt_price(tp2),
        "sl_pct":       round(dist / ep * 100, 3),
        "rsi":          o_rep["rsi"],
        "in_kz":        kz,
        "regime":       regime,
        "score_trend":  b_trend,
        "score_smc":    b_smc,
        "score_osc":    b_osc,
        "score_liq":    b_liq,
        "score_fund":   b_fund,
        "funding_rate": funding_rate,
        "bull_sweep":   l_rep["bull_sweep"] if l_rep else False,
        "ts":           ts,
    }

    # ── Claude Final Filter — ตัวกรองสุดท้ายก่อน signal ยิง ──────────────────
    try:
        import claude_filter
        approved, claude_reason = claude_filter.ask(signal, scan_result)
    except Exception as _fe:
        approved, claude_reason = True, f"filter_err:{str(_fe)[:40]}"

    if not approved:
        scan_result["status"]       = "REJECTED"
        scan_result["claude_reason"] = claude_reason
        print(f"  🚫 [CLAUDE] REJECT {sym} — {claude_reason}")
        save_shadow_signal({**signal, "claude_reason": claude_reason})
        return None, scan_result

    signal["claude_approved"] = True
    signal["claude_reason"]   = claude_reason

    # ── Level 4: บันทึก conditions ที่ active ตอน signal ยิง ──
    save_condition_snapshot(sym, side, regime, ts, t_rep, s_rep, o_rep, l_rep)

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
def save_condition_snapshot(sym, side, regime, signal_ts, t_rep, s_rep, o_rep, l_rep=None):
    """
    บันทึก conditions ทุกตัวที่ active ตอน signal ยิง
    พร้อม regime → ใช้ใน weekly_report.py เพื่อคำนวณ win rate ต่อ condition
    """
    try:
        td = t_rep.get("details", {})
        sd = s_rep.get("details", {})
        od = o_rep.get("details", {})
        ld = (l_rep or {})   # liquidity report (top-level keys: bull_sweep, bear_sweep, eq_highs, eq_lows)

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
                macd_dn         INTEGER DEFAULT 0,
                -- 🎯 Phase 1 (Trend+): ADX, BB squeeze
                adx_strong      INTEGER DEFAULT 0,
                bb_squeeze      INTEGER DEFAULT 0,
                -- 📈 Phase 3 (Osc+): OBV
                obv_above_20    INTEGER DEFAULT 0,
                obv_above_50    INTEGER DEFAULT 0,
                -- 💧 Phase 2 (Liquidity agent)
                bull_sweep      INTEGER DEFAULT 0,
                bear_sweep      INTEGER DEFAULT 0,
                eq_highs        INTEGER DEFAULT 0,
                eq_lows         INTEGER DEFAULT 0
            )
        """)
        # migrate: เพิ่ม columns ใน condition_snapshots ที่อาจไม่มีใน DB เก่า
        for _c, _d in [
            ("adx_strong",   "INTEGER DEFAULT 0"),
            ("bb_squeeze",   "INTEGER DEFAULT 0"),
            ("obv_above_20", "INTEGER DEFAULT 0"),
            ("obv_above_50", "INTEGER DEFAULT 0"),
            ("bull_sweep",   "INTEGER DEFAULT 0"),
            ("bear_sweep",   "INTEGER DEFAULT 0"),
            ("eq_highs",     "INTEGER DEFAULT 0"),
            ("eq_lows",      "INTEGER DEFAULT 0"),
        ]:
            try:
                cur.execute(f"ALTER TABLE condition_snapshots ADD COLUMN {_c} {_d}")
            except Exception:
                pass
        cur.execute("""
            INSERT INTO condition_snapshots (
                signal_ts, symbol, side, regime,
                cdc_bull, cross_up, cross_dn, touch_bull, touch_bear,
                above_sma50, above_sma200, sma50_gt_200, trail_slow_bull,
                bos_bull, bos_bear, choch_bull, choch_bear,
                qm_bull, qm_bear, in_discount, in_premium, in_eq,
                rsi_bull_div, rsi_bear_div, rsi_os, rsi_ob,
                st_up, st_dn, macd_up, macd_dn,
                adx_strong, bb_squeeze, obv_above_20, obv_above_50,
                bull_sweep, bear_sweep, eq_highs, eq_lows
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
            # NEW
            int(td.get("adx_strong",       False)),
            int(td.get("bb_squeeze",       False)),
            int(od.get("obv_above_20",     False)),
            int(od.get("obv_above_50",     False)),
            int(ld.get("bull_sweep",       False)),
            int(ld.get("bear_sweep",       False)),
            int(ld.get("eq_highs",         False)),
            int(ld.get("eq_lows",          False)),
        ))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[ERR] save_condition_snapshot: {e}")


# ══════════════════════════════════════════════════════════════
# SHADOW SIGNAL — บันทึก signals ที่ถูก Claude Reject (Shadow Mode)
# ══════════════════════════════════════════════════════════════
def save_shadow_signal(signal: dict):
    """
    บันทึก signal ที่ Claude Reject ลง shadow_trades table
    paper_trade.py จะ track outcome ทีหลัง (WIN/LOSS/TIMEOUT)
    ใช้ประเมินว่า Claude filter ช่วยหรือ over-filter
    """
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT,
                side          TEXT,
                score         INTEGER,
                entry_px      REAL,
                sl_px         REAL,
                tp1_px        REAL,
                tp2_px        REAL,
                sl_pct        REAL,
                regime        TEXT,
                claude_reason TEXT,
                created_at    TEXT,
                outcome       TEXT DEFAULT 'PENDING',
                exit_px       REAL,
                resolved_at   TEXT,
                tp1_hit       INTEGER DEFAULT 0,
                exit_reason   TEXT
            )
        """)
        # migrate: เพิ่ม columns ใน shadow_trades ที่อาจไม่มีใน DB เก่า
        for _col, _def in [
            ("tp1_hit",      "INTEGER DEFAULT 0"),
            ("exit_reason",  "TEXT"),
            ("score_liq",    "INTEGER DEFAULT 0"),
            ("score_fund",   "INTEGER DEFAULT 0"),
            ("funding_rate", "REAL"),
            ("bull_sweep",   "INTEGER DEFAULT 0"),
            ("bear_sweep",   "INTEGER DEFAULT 0"),
        ]:
            try:
                cur.execute(f"ALTER TABLE shadow_trades ADD COLUMN {_col} {_def}")
            except Exception:
                pass
        # ถ้า symbol เดิมยัง PENDING อยู่ → ไม่บันทึกซ้ำ
        existing = cur.execute(
            "SELECT id FROM shadow_trades WHERE symbol=? AND side=? AND outcome='PENDING'",
            (signal.get("symbol"), signal.get("side"))
        ).fetchone()
        if existing:
            con.close()
            return
        cur.execute("""
            INSERT INTO shadow_trades
            (symbol, side, score, entry_px, sl_px, tp1_px, tp2_px,
             sl_pct, regime, claude_reason, created_at,
             score_liq, score_fund, funding_rate, bull_sweep, bear_sweep)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            signal.get("symbol"), signal.get("side"),
            signal.get("score", 0),
            signal.get("price", 0), signal.get("sl", 0),
            signal.get("tp1",  0), signal.get("tp2", 0),
            signal.get("sl_pct", 0),
            signal.get("regime", "UNKNOWN"),
            signal.get("claude_reason", ""),
            datetime.now(timezone.utc).isoformat(),
            signal.get("score_liq",  0),
            signal.get("score_fund", 0),
            signal.get("funding_rate"),
            1 if signal.get("bull_sweep") else 0,
            1 if signal.get("bear_sweep") else 0,
        ))
        con.commit()
        con.close()
    except Exception as e:
        print(f"[ERR] save_shadow_signal: {e}")


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

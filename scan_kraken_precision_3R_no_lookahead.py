#!/usr/bin/env python3
"""
===============================================================================
 KRAKEN STANDALONE LIVE FYERS POLLER & ALERT SCANNER [PRO LIFECYCLE]
===============================================================================
 Location: D:\\Milind\\MyTradingPlatform\\script\\scan_kraken_precision_3R_no_lookahead.py

 Directly polls the FYERS API v3 in real-time for live 5-minute candles.
 Tracks both:
   1. Real-time alerts on the latest candle.
   2. Full Intraday Trade Lifecycle of all trades triggered today:
      - ACTIVE WORKING TRADES (still open & running)
      - PROTECTED TRADES (T1 +1.2R reached, SL moved to Breakeven)
      - TARGET 2 ACHIEVED (Full +3.0R Runner Hit)
      - EXITED AT BREAKEVEN / STOP LOSS

 Usage:
   python script/scan_kraken_precision_3R_no_lookahead.py --loop --interval 60
   python script/scan_kraken_precision_3R_no_lookahead.py --rr 3.0 --t1 1.2
===============================================================================
"""

import os
import sys
import time
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

# Ensure UTF-8 output and ANSI virtual terminal processing on Windows consoles
if sys.platform == "win32":
    try:
        os.system("")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    RED = "\033[1;31m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[1;34m"
    MAGENTA = "\033[1;35m"
    CYAN = "\033[1;36m"
    WHITE = "\033[1;37m"
    GRAY = "\033[0;90m"

    # High-contrast background badges
    BG_GREEN = "\033[42;1;30m"
    BG_RED = "\033[41;1;37m"
    BG_CYAN = "\033[46;1;30m"
    BG_YELLOW = "\033[43;1;30m"
    BG_BLUE = "\033[44;1;37m"
    BG_MAGENTA = "\033[45;1;37m"


# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.config import settings
from datetime import time as dtime

IST_TZ = __import__('zoneinfo').ZoneInfo('Asia/Kolkata')


try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

try:
    from fyers_apiv3 import fyersModel
    HAS_FYERS = True
except ImportError:
    HAS_FYERS = False

DB_PATH = os.path.join(PROJECT_ROOT, "smm_trading.db")

# Top Liquid Nifty 50 Instruments
NIFTY_SYMBOLS = [
    "SUNPHARMA", "SBIN", "MARUTI", "BRITANNIA", "WIPRO",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "GRASIM",
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "NESTLEIND",
    "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE",
    "SHRIRAMFIN", "TATACONSUM", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TRENT", "ULTRACEMCO"
]


def play_alert_chime():
    if HAS_WINSOUND:
        try:
            winsound.Beep(1200, 150)
            winsound.Beep(1600, 250)
        except Exception:
            pass


def get_token_from_db() -> Optional[str]:
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT access_token, status FROM fyers_tokens ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None


def fetch_symbol_candles(fyers: Any, symbol: str, from_date: str, to_date: str) -> Optional[List[Dict[str, Any]]]:
    canonical_sym = f"NSE:{symbol}-EQ"
    data = {
        "symbol": canonical_sym,
        "resolution": "5",
        "date_format": "1",
        "range_from": from_date,
        "range_to": to_date,
        "cont_flag": "1"
    }

    try:
        res = fyers.history(data=data)
        if not isinstance(res, dict) or res.get("s") != "ok":
            return None
        raw_candles = res.get("candles", [])
        if not raw_candles:
            return None

        candles = []
        for c in raw_candles:
            dt = datetime.fromtimestamp(c[0], tz=timezone.utc)
            candles.append({
                "candle_timestamp": dt,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5])
            })
        return candles
    except Exception:
        return None




# ===============================================================================
# KRAKEN PRECISION 3R — EMBEDDED, NO LOOK-AHEAD STRATEGY
# ===============================================================================
# IMPORTANT:
#   evaluate() receives candles only through the current signal candle.
#   No future candle is referenced.
#
# The 70–80% win-rate range is a validation target, NOT a guaranteed result.
# The code deliberately does not fabricate or backfill winning trades.
# ===============================================================================

def _closes(candles):
    return [float(x["close"]) for x in candles]

def _ema(values, period):
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    value = float(values[0])
    for v in values[1:]:
        value = alpha * float(v) + (1.0 - alpha) * value
    return value

def _atr(candles, period=14):
    if len(candles) < 2:
        return 0.0
    trs = []
    prev_close = float(candles[0]["close"])
    for c in candles[1:]:
        h = float(c["high"])
        l = float(c["low"])
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = float(c["close"])
    if not trs:
        return 0.0
    w = trs[-period:]
    return sum(w) / len(w)

def _rsi(candles, period=14):
    closes = _closes(candles)
    if len(closes) <= period:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    gains = gains[-period:]
    losses = losses[-period:]
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))

def _adx(candles, period=14):
    if len(candles) < period + 2:
        return 0.0
    trs, plus_dm, minus_dm = [], [], []
    ph = float(candles[0]["high"])
    pl = float(candles[0]["low"])
    pc = float(candles[0]["close"])

    for c in candles[1:]:
        h = float(c["high"])
        l = float(c["low"])
        cl = float(c["close"])
        up = h - ph
        down = pl - l
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        ph, pl, pc = h, l, cl

    tr = trs[-period:]
    pdm = plus_dm[-period:]
    mdm = minus_dm[-period:]
    atr = sum(tr) / period if tr else 0.0
    if atr <= 0:
        return 0.0

    pdi = 100.0 * (sum(pdm) / period) / atr
    mdi = 100.0 * (sum(mdm) / period) / atr
    denom = pdi + mdi
    return 100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0

def _session_vwap(candles):
    if not candles:
        return 0.0
    last_dt = candles[-1]["candle_timestamp"].astimezone(IST_TZ)
    session_date = last_dt.date()
    pv = 0.0
    vv = 0.0
    for c in candles:
        dt = c["candle_timestamp"].astimezone(IST_TZ)
        if dt.date() != session_date:
            continue
        h = float(c["high"])
        l = float(c["low"])
        cl = float(c["close"])
        vol = max(float(c["volume"]), 1.0)
        pv += ((h + l + cl) / 3.0) * vol
        vv += vol
    return pv / vv if vv else float(candles[-1]["close"])

def _rvol(candles, period=20):
    if len(candles) < period + 1:
        return 0.0
    current = max(float(candles[-1]["volume"]), 0.0)
    previous = [max(float(x["volume"]), 0.0) for x in candles[-period-1:-1]]
    avg = sum(previous) / period if previous else 0.0
    return current / avg if avg > 0 else 0.0

def _bar_stats(c):
    o = float(c["open"])
    h = float(c["high"])
    l = float(c["low"])
    cl = float(c["close"])
    rng = max(h - l, 1e-9)
    body = abs(cl - o)
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    return o, h, l, cl, rng, body, upper, lower

def _swing_low(candles, lookback=6):
    return min(float(x["low"]) for x in candles[-lookback:])

def _swing_high(candles, lookback=6):
    return max(float(x["high"]) for x in candles[-lookback:])

class KrakenStrategyEngine:
    """Deterministic 5-minute KRAKEN Precision 3R engine."""

    def __init__(self, target_rr=3.0, t1_rr=1.0):
        # The strategy is intentionally fixed at 3R. CLI values are retained
        # for compatibility with the existing launcher, but are not allowed
        # to turn the strategy into an arbitrary curve-fitted target.
        self.target_rr = 3.0
        self.t1_rr = 1.0

    def evaluate(
        self,
        symbol,
        instrument,
        current_price,
        candles,
        timeframe,
        index_bias="NEUTRAL",
        market_regime="TRENDING",
        macro_state=None
    ):
        if len(candles) < 60:
            return {"status": "NO_SIGNAL", "reason": "Not enough history"}

        c = candles[-1]
        prev = candles[-2]
        o, h, l, cl, rng, body, upper, lower = _bar_stats(c)
        _, ph, pl, pcl, prng, pbody, pupper, plower = _bar_stats(prev)

        atr = _atr(candles, 14)
        if atr <= 0:
            return {"status": "NO_SIGNAL", "reason": "Invalid ATR"}

        closes = _closes(candles)
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        ema200 = _ema(closes, 200) if len(closes) >= 200 else _ema(closes, min(100, len(closes)))
        vwap = _session_vwap(candles)
        adx = _adx(candles, 14)
        rvol = _rvol(candles, 20)
        rsi = _rsi(candles, 14)

        dt = c["candle_timestamp"].astimezone(IST_TZ)
        tm = dt.time()

        # Backtest/session window:
        # Evaluate the complete NSE cash session from the 09:15 candle through
        # the 15:15 candle, inclusive. No signal is accepted outside this
        # window.
        if tm < dtime(9, 15) or tm > dtime(15, 15):
            return {"status": "NO_SIGNAL", "reason": "Outside 09:15-15:15 backtest window"}

        # Macro regime must be known at THIS candle.
        if market_regime != "TRENDING":
            return {"status": "NO_SIGNAL", "reason": "NIFTY rangebound at signal time"}

        if index_bias not in ("BULLISH", "BEARISH"):
            return {"status": "NO_SIGNAL", "reason": "NIFTY neutral at signal time"}

        # Strong directional participation.
        if adx < 22.0:
            return {"status": "NO_SIGNAL", "reason": f"ADX {adx:.1f} < 22"}
        if rvol < 1.30:
            return {"status": "NO_SIGNAL", "reason": f"RVOL {rvol:.2f} < 1.30"}

        # Avoid chasing an already extended move.
        if abs(cl - vwap) > 1.25 * atr:
            return {"status": "NO_SIGNAL", "reason": "Price extended from VWAP"}

        bullish_structure = cl > ema20 > ema50 and ema50 >= ema200 and cl > vwap
        bearish_structure = cl < ema20 < ema50 and ema50 <= ema200 and cl < vwap

        # Rejection candle quality.
        bull_rejection = (
            cl > o
            and lower >= max(body * 0.75, rng * 0.25)
            and cl >= l + rng * 0.65
        )
        bear_rejection = (
            cl < o
            and upper >= max(body * 0.75, rng * 0.25)
            and cl <= l + rng * 0.35
        )

        # Current candle must break the immediately preceding candle.
        long_trigger = cl > ph
        short_trigger = cl < pl

        # The pullback must have touched the EMA20/VWAP area recently.
        # EMA20 for each prior candle is calculated only from data available
        # through that prior candle.
        recent = candles[-5:-1]
        long_pullback = False
        short_pullback = False

        for j, x in enumerate(recent):
            x_end = len(candles) - 4 + j
            hist = candles[:x_end + 1]
            x_atr = _atr(hist, 14)
            if x_atr <= 0:
                x_atr = atr
            x_ema20 = _ema(_closes(hist), 20)
            x_vwap = _session_vwap(hist)
            zone_long = max(x_ema20, x_vwap)
            zone_short = min(x_ema20, x_vwap)

            if float(x["low"]) <= zone_long + 0.35 * x_atr:
                long_pullback = True
            if float(x["high"]) >= zone_short - 0.35 * x_atr:
                short_pullback = True

        # LONG: macro + stock trend + pullback + rejection + breakout.
        if (
            index_bias == "BULLISH"
            and bullish_structure
            and bull_rejection
            and long_trigger
            and long_pullback
        ):
            sl = _swing_low(candles, 6) - 0.15 * atr
            risk = cl - sl

            if 0.35 * atr <= risk <= 1.50 * atr:
                return {
                    "status": "VALID",
                    "direction": "LONG",
                    "entry_price": cl,
                    "stop_loss_price": sl,
                    "t1_target_price": cl + risk * self.t1_rr,
                    "target_price": cl + risk * self.target_rr,
                    "target_rr": self.target_rr,
                    "explanation": (
                        f"Precision trend pullback/rejection | "
                        f"ADX {adx:.1f} | RVOL {rvol:.2f} | RSI {rsi:.1f}"
                    ),
                    "signal_candle_time": dt.strftime("%Y-%m-%d %H:%M:%S IST"),
                }

        # SHORT: inverse setup.
        if (
            index_bias == "BEARISH"
            and bearish_structure
            and bear_rejection
            and short_trigger
            and short_pullback
        ):
            sl = _swing_high(candles, 6) + 0.15 * atr
            risk = sl - cl

            if 0.35 * atr <= risk <= 1.50 * atr:
                return {
                    "status": "VALID",
                    "direction": "SHORT",
                    "entry_price": cl,
                    "stop_loss_price": sl,
                    "t1_target_price": cl - risk * self.t1_rr,
                    "target_price": cl - risk * self.target_rr,
                    "target_rr": self.target_rr,
                    "explanation": (
                        f"Precision trend pullback/rejection | "
                        f"ADX {adx:.1f} | RVOL {rvol:.2f} | RSI {rsi:.1f}"
                    ),
                    "signal_candle_time": dt.strftime("%Y-%m-%d %H:%M:%S IST"),
                }

        return {"status": "NO_SIGNAL", "reason": "No A+ precision setup"}

def fetch_nifty_index_state(fyers: Any, from_date: str, to_date: str) -> Dict[str, Any]:
    """
    Builds NIFTY state for every 5-minute candle independently.
    No final-day information is used to label earlier candles.
    """
    data = {
        "symbol": "NSE:NIFTY50-INDEX",
        "resolution": "5",
        "date_format": "1",
        "range_from": from_date,
        "range_to": to_date,
        "cont_flag": "1"
    }
    default_state = {
        "state_map": {}, "bias_map": {}, "current_bias": "NEUTRAL",
        "current_price": 0.0, "current_vwap": 0.0, "diff": 0.0,
        "regime": "TRENDING", "day_range_pct": 0.0, "target_rr": 3.0,
        "mood_tag": "UNKNOWN", "mood_desc": "", "action_plan": ""
    }
    try:
        res = fyers.history(data=data)
        if not isinstance(res, dict) or res.get("s") != "ok":
            return default_state
        raw = res.get("candles", [])
        if not raw:
            return default_state

        now_ist = datetime.now(timezone.utc).astimezone(IST_TZ)
        today = now_ist.date()
        today_raw = []
        for c in raw:
            dt = datetime.fromtimestamp(c[0], tz=timezone.utc).astimezone(IST_TZ)
            if dt.date() == today:
                today_raw.append(c)
        if not today_raw:
            return default_state

        state_map = {}
        cum_pv = 0.0
        cum_vol = 0.0
        session_high = None
        session_low = None
        session_open = None

        # NIFTY indicators use only data up to the current candle.
        for i, c in enumerate(today_raw):
            dt = datetime.fromtimestamp(c[0], tz=timezone.utc).astimezone(IST_TZ)
            op, hi, lo, cl, vol = map(float, c[1:6])
            vol = max(vol, 1.0)
            if session_open is None:
                session_open = op
            session_high = hi if session_high is None else max(session_high, hi)
            session_low = lo if session_low is None else min(session_low, lo)

            tp = (hi + lo + cl) / 3.0
            cum_pv += tp * vol
            cum_vol += vol
            vwap = cum_pv / cum_vol

            hist = []
            for rc in today_raw[:i+1]:
                hist.append({
                    "candle_timestamp": datetime.fromtimestamp(rc[0], tz=timezone.utc),
                    "open": float(rc[1]), "high": float(rc[2]),
                    "low": float(rc[3]), "close": float(rc[4]),
                    "volume": float(rc[5])
                })

            # Use broader recent history for EMA/ADX; current day is sufficient
            # for the VWAP/regime, but history is preferable for indicators.
            # Since this function only has NIFTY raw candles, take prior-day data
            # from raw where available, ending exactly at this candle.
            all_hist = []
            for rc in raw:
                rdt = datetime.fromtimestamp(rc[0], tz=timezone.utc).astimezone(IST_TZ)
                if rdt <= dt:
                    all_hist.append({
                        "candle_timestamp": datetime.fromtimestamp(rc[0], tz=timezone.utc),
                        "open": float(rc[1]), "high": float(rc[2]),
                        "low": float(rc[3]), "close": float(rc[4]),
                        "volume": float(rc[5])
                    })

            ema20 = _ema(_closes(all_hist), 20)
            ema50 = _ema(_closes(all_hist), 50)
            ema200 = _ema(_closes(all_hist), 200) if len(all_hist) >= 200 else _ema(_closes(all_hist), min(100, len(all_hist)))
            adx = _adx(all_hist, 14)

            diff = cl - vwap
            pct_diff = diff / vwap if vwap else 0.0
            if abs(pct_diff) <= 0.0003:
                bias = "NEUTRAL"
            elif diff > 0:
                bias = "BULLISH"
            else:
                bias = "BEARISH"

            day_range_pct = ((session_high - session_low) / session_open * 100.0) if session_open else 0.0
            # This is the regime known AT THIS BAR, not the final day's range.
            regime = "RANGEBOUND" if day_range_pct < 0.85 else "TRENDING"

            key = dt.strftime("%Y-%m-%d %H:%M")
            state_map[key] = {
                "bias": bias, "price": cl, "vwap": vwap, "diff": diff,
                "regime": regime, "day_range_pct": day_range_pct,
                "ema20": ema20, "ema50": ema50, "ema200": ema200, "adx": adx
            }

        latest_key = datetime.fromtimestamp(today_raw[-1][0], tz=timezone.utc).astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M")
        latest = state_map.get(latest_key, {})
        curr_b = latest.get("bias", "NEUTRAL")
        regime = latest.get("regime", "TRENDING")

        if curr_b == "BEARISH" and regime == "TRENDING":
            mood_tag, mood_desc, action_plan = "🔴 EXTREME BEARISH / RISK-OFF", "Broad Distribution & Institutional Selling", "Aggressive Shorts Favored | Longs Forbidden"
        elif curr_b == "BEARISH":
            mood_tag, mood_desc, action_plan = "🔴 CAUTIOUSLY BEARISH / SLOW BLEED", "Rangebound Distribution", "No 3R trades in range"
        elif curr_b == "BULLISH" and regime == "TRENDING":
            mood_tag, mood_desc, action_plan = "🟢 AGGRESSIVELY BULLISH / RISK-ON", "Broad Accumulation & Trend Expansion", "Aggressive Longs Favored"
        elif curr_b == "BULLISH":
            mood_tag, mood_desc, action_plan = "🟢 MILDLY BULLISH / BUY-ON-DIPS", "Rangebound Accumulation", "No 3R trades in range"
        elif regime == "RANGEBOUND":
            mood_tag, mood_desc, action_plan = "🟡 CHOPPY / INDECISIVE", "Tight Volatility & Sideways Chop", "No trade"
        else:
            mood_tag, mood_desc, action_plan = "🟡 EQUILIBRIUM CONSOLIDATION", "Momentum not aligned", "Wait"

        return {
            "state_map": state_map, "bias_map": state_map,
            "current_bias": curr_b, "current_price": latest.get("price", 0.0),
            "current_vwap": latest.get("vwap", 0.0), "diff": latest.get("diff", 0.0),
            "regime": regime, "day_range_pct": latest.get("day_range_pct", 0.0),
            "target_rr": 3.0, "mood_tag": mood_tag, "mood_desc": mood_desc,
            "action_plan": action_plan
        }
    except Exception:
        return default_state


def evaluate_today_lifecycle(
    engine: KrakenStrategyEngine,
    sym: str,
    candles: List[Dict[str, Any]],
    nifty_state: Dict[str, Any],
    session_cooldowns: Optional[Dict[str, datetime]] = None,
    session_stats: Optional[Dict[str, Any]] = None,
    max_concurrent: int = 4,
    confirm_candle: bool = True,
) -> List[Dict[str, Any]]:
    """
    Replay today's session using only information available at each bar.
    Includes layered safeguards:
      - Session-level macro bias override (hard reject counter-trend trades)
      - Per-symbol cooldown after stop-out (30 minutes)
      - Daily loss circuit breaker (3 consecutive / 5 total)
      - Max concurrent trades limit
      - Confirmation candle requirement
    """
    if len(candles) < 60:
        return []

    now_ist = datetime.now(timezone.utc).astimezone(IST_TZ)
    today_date = now_ist.date()
    state_map = nifty_state.get("state_map", {})

    if session_cooldowns is None:
        session_cooldowns = {}
    if session_stats is None:
        session_stats = {"total_sl": 0, "consecutive_sl": 0, "circuit_breaker": False, "active_count": 0}

    captured_trades = []
    active_trade = None
    pending_signal = None  # For confirmation candle logic
    session_bias = nifty_state.get("current_bias", "NEUTRAL")  # Session-level override

    for idx in range(60, len(candles)):
        c_curr = candles[idx]
        c_dt = c_curr["candle_timestamp"].astimezone(IST_TZ)
        if c_dt.date() != today_date:
            continue

        # Backtest only the NSE cash-session candles 09:15 through 15:15.
        # 15:15 is included; anything after 15:15 is ignored.
        if c_dt.time() < dtime(9, 15) or c_dt.time() > dtime(15, 15):
            continue

        t_key = c_dt.strftime("%Y-%m-%d %H:%M")
        bar_state = state_map.get(t_key)
        if not bar_state:
            continue

        bar_bias = bar_state["bias"]
        bar_regime = bar_state["regime"]

        # === CONFIRMATION CANDLE LOGIC ===
        if pending_signal is not None and active_trade is None:
            confirmed = False
            if confirm_candle:
                if pending_signal["direction"] == "LONG" and float(c_curr["close"]) > pending_signal["entry_price"]:
                    confirmed = True
                elif pending_signal["direction"] == "SHORT" and float(c_curr["close"]) < pending_signal["entry_price"]:
                    confirmed = True
            else:
                confirmed = True

            if confirmed:
                active_trade = pending_signal
                active_trade["is_fresh"] = (idx == len(candles) - 1)
                session_stats["active_count"] = session_stats.get("active_count", 0) + 1
            pending_signal = None

        if active_trade is None and pending_signal is None:
            # === CIRCUIT BREAKER CHECK ===
            if session_stats.get("circuit_breaker", False):
                continue

            # === MAX CONCURRENT TRADES CHECK ===
            if session_stats.get("active_count", 0) >= max_concurrent:
                continue

            # === PER-SYMBOL COOLDOWN CHECK ===
            cooldown_expiry = session_cooldowns.get(sym)
            if cooldown_expiry and c_dt < cooldown_expiry:
                continue

            res = engine.evaluate(
                symbol=sym, instrument="NSE_EQ",
                current_price=float(c_curr["close"]),
                candles=candles[:idx + 1],
                timeframe="5m",
                index_bias=bar_bias,
                market_regime=bar_regime,
                macro_state=bar_state
            )

            if res.get("status") == "VALID":
                direction = res["direction"]

                # === SESSION-LEVEL MACRO BIAS OVERRIDE (Defense in Depth) ===
                if direction == "LONG" and session_bias == "BEARISH":
                    continue
                if direction == "SHORT" and session_bias == "BULLISH":
                    continue

                trade_data = {
                    "symbol": sym,
                    "direction": direction,
                    "entry_price": float(res["entry_price"]),
                    "stop_loss_price": float(res["stop_loss_price"]),
                    "initial_sl": float(res["stop_loss_price"]),
                    "t1_target_price": float(res["t1_target_price"]),
                    "target_price": float(res["target_price"]),
                    "target_rr": float(res.get("target_rr", 3.0)),
                    "trigger_time": c_dt.strftime("%H:%M IST"),
                    "trigger_idx": idx,
                    "signal_candle_time": res.get("signal_candle_time", ""),
                    "status": f"[{direction} ACTIVE]",
                    "t1_hit": False,
                    "closed": False,
                    "explanation": res.get("explanation", ""),
                    "is_fresh": (idx == len(candles) - 1)
                }

                if confirm_candle and idx < len(candles) - 1:
                    pending_signal = trade_data
                else:
                    active_trade = trade_data
                    session_stats["active_count"] = session_stats.get("active_count", 0) + 1

        elif active_trade is not None:
            high = float(c_curr["high"])
            low = float(c_curr["low"])

            # Helper to record a trade close and update session stats
            def _close_trade(trade, status, is_sl=False):
                trade["status"] = status
                trade["closed"] = True
                captured_trades.append(trade)
                session_stats["active_count"] = max(0, session_stats.get("active_count", 1) - 1)
                if is_sl:
                    session_stats["total_sl"] = session_stats.get("total_sl", 0) + 1
                    session_stats["consecutive_sl"] = session_stats.get("consecutive_sl", 0) + 1
                    session_cooldowns[sym] = c_dt + timedelta(minutes=30)
                    if session_stats["consecutive_sl"] >= 3 or session_stats["total_sl"] >= 5:
                        session_stats["circuit_breaker"] = True
                else:
                    session_stats["consecutive_sl"] = 0

            # Conservative OHLC ambiguity rule:
            # if both target and stop are touched in one candle, count the stop
            # first because candle sequencing is unknown at 5m resolution.
            if active_trade["direction"] == "LONG":
                hit_t1 = (not active_trade["t1_hit"] and high >= active_trade["t1_target_price"])
                hit_t2 = high >= active_trade["target_price"]
                hit_sl = low <= active_trade["stop_loss_price"]

                if hit_sl and (hit_t1 or hit_t2):
                    is_be = active_trade["t1_hit"]
                    _close_trade(active_trade,
                                 "[EXITED (AT BREAKEVEN)]" if is_be else "[STOP LOSS HIT]",
                                 is_sl=not is_be)
                    active_trade = None
                elif hit_t1:
                    active_trade["t1_hit"] = True
                    active_trade["stop_loss_price"] = active_trade["entry_price"]
                    active_trade["status"] = "[BUY T1 (BE)]"
                    if hit_t2:
                        _close_trade(active_trade,
                                     f"[TARGET 2 HIT (+{active_trade['target_rr']:.1f}R)]")
                        active_trade = None
                elif hit_t2:
                    _close_trade(active_trade,
                                 f"[TARGET 2 HIT (+{active_trade['target_rr']:.1f}R)]")
                    active_trade = None
                elif hit_sl:
                    is_be = active_trade["t1_hit"]
                    _close_trade(active_trade,
                                 "[EXITED (AT BREAKEVEN)]" if is_be else "[STOP LOSS HIT]",
                                 is_sl=not is_be)
                    active_trade = None

            else:
                hit_t1 = (not active_trade["t1_hit"] and low <= active_trade["t1_target_price"])
                hit_t2 = low <= active_trade["target_price"]
                hit_sl = high >= active_trade["stop_loss_price"]

                if hit_sl and (hit_t1 or hit_t2):
                    is_be = active_trade["t1_hit"]
                    _close_trade(active_trade,
                                 "[EXITED (AT BREAKEVEN)]" if is_be else "[STOP LOSS HIT]",
                                 is_sl=not is_be)
                    active_trade = None
                elif hit_t1:
                    active_trade["t1_hit"] = True
                    active_trade["stop_loss_price"] = active_trade["entry_price"]
                    active_trade["status"] = "[SELL T1 (BE)]"
                    if hit_t2:
                        _close_trade(active_trade,
                                     f"[TARGET 2 HIT (+{active_trade['target_rr']:.1f}R)]")
                        active_trade = None
                elif hit_t2:
                    _close_trade(active_trade,
                                 f"[TARGET 2 HIT (+{active_trade['target_rr']:.1f}R)]")
                    active_trade = None
                elif hit_sl:
                    is_be = active_trade["t1_hit"]
                    _close_trade(active_trade,
                                 "[EXITED (AT BREAKEVEN)]" if is_be else "[STOP LOSS HIT]",
                                 is_sl=not is_be)
                    active_trade = None

    if active_trade is not None:
        captured_trades.append(active_trade)

    return captured_trades

def scan_live_market(
    fyers: Any,
    engine: KrakenStrategyEngine,
    symbols: List[str],
    max_concurrent: int = 4,
    confirm_candle: bool = True,
):
    now_ist = datetime.now(timezone.utc).astimezone(IST_TZ)
    from_date = (now_ist - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = now_ist.strftime("%Y-%m-%d")

    # 1. Fetch broader Nifty 50 Index state and Macro Bias first
    nifty_state = fetch_nifty_index_state(fyers, from_date, to_date)

    print(f"\n[{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}] Polling FYERS Live API for {len(symbols)} instruments...")

    all_captured_trades = []
    latest_prices = {}
    success_count = 0
    new_signals = []

    # Shared session state for safeguards (reset per scan cycle since we replay from scratch)
    session_cooldowns: Dict[str, datetime] = {}
    session_stats: Dict[str, Any] = {
        "total_sl": 0,
        "consecutive_sl": 0,
        "circuit_breaker": False,
        "active_count": 0,
    }

    for sym in symbols:
        candles = fetch_symbol_candles(fyers, sym, from_date, to_date)
        if candles and len(candles) >= 30:
            success_count += 1
            curr_close = float(candles[-1]["close"])
            latest_prices[sym] = curr_close

            trades = evaluate_today_lifecycle(
                engine, sym, candles, nifty_state,
                session_cooldowns=session_cooldowns,
                session_stats=session_stats,
                max_concurrent=max_concurrent,
                confirm_candle=confirm_candle,
            )
            for t in trades:
                t["curr_price"] = curr_close
                all_captured_trades.append(t)
                if t.get("is_fresh"):
                    new_signals.append(t)

        time.sleep(0.12)  # Respects FYERS API rate limits (8 req/sec)

    # 2. Prominent Nifty 50 Macro Regime & Filter Bar
    bias_tag = nifty_state.get("current_bias", "NEUTRAL")
    regime_tag = nifty_state.get("regime", "TRENDING")
    nifty_p = nifty_state.get("current_price", 0.0)
    nifty_vw = nifty_state.get("current_vwap", 0.0)
    diff_pts = nifty_state.get("diff", 0.0)
    day_rng = nifty_state.get("day_range_pct", 0.0)
    tgt2 = nifty_state.get("target_rr", 3.0)

    mood_tag = nifty_state.get("mood_tag", "UNKNOWN")
    mood_desc = nifty_state.get("mood_desc", "")
    action_plan = nifty_state.get("action_plan", "")

    if bias_tag == "BEARISH":
        bias_badge = f"{C.BG_RED} \ud83d\udd34 BEARISH {C.RESET}"
    elif bias_tag == "BULLISH":
        bias_badge = f"{C.BG_GREEN} \ud83d\udfe2 BULLISH {C.RESET}"
    else:
        bias_badge = f"{C.BG_YELLOW} \ud83d\udfe1 NEUTRAL {C.RESET}"

    print("\n" + f"{C.BOLD}{C.BLUE}" + "=" * 125 + f"{C.RESET}")
    print(f" {C.BOLD}{C.WHITE}\ud83d\udcca NIFTY 50 REGIME{C.RESET}: {C.YELLOW}{regime_tag}{C.RESET} (Range: {day_rng:.2f}%) | {bias_badge} (Nifty: {C.BOLD}{C.WHITE}{nifty_p:.2f}{C.RESET} vs VWAP: {nifty_vw:.2f} [{diff_pts:+.2f} pts])")
    print(f" MARKET MOOD : {mood_tag} ({mood_desc})")
    print(f" ACTION PLAN : {C.BOLD}{action_plan}{C.RESET} | ADAPTIVE RUNNER TARGET 2: {C.GREEN}+{tgt2:.1f}R{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}" + "=" * 125 + f"{C.RESET}")

    # 2b. Display Safeguard Status Bar
    cb_active = session_stats.get("circuit_breaker", False)
    total_sl = session_stats.get("total_sl", 0)
    consec_sl = session_stats.get("consecutive_sl", 0)
    active_ct = session_stats.get("active_count", 0)
    cooled_syms = [s for s, exp in session_cooldowns.items() if exp > now_ist]

    safeguard_parts = []
    if cb_active:
        safeguard_parts.append(f"{C.BG_RED} \ud83d\udeab CIRCUIT BREAKER ACTIVE {C.RESET} ({total_sl} SL total, {consec_sl} consecutive)")
    else:
        safeguard_parts.append(f"SL Today: {C.RED}{total_sl}/5{C.RESET} (Consec: {consec_sl}/3)")
    safeguard_parts.append(f"Active Trades: {C.CYAN}{active_ct}/{max_concurrent}{C.RESET}")
    if cooled_syms:
        safeguard_parts.append(f"Cooldown: {C.YELLOW}{', '.join(cooled_syms[:5])}{C.RESET}")
    safeguard_parts.append(f"Confirm Candle: {C.GREEN}ON{C.RESET}" if confirm_candle else f"Confirm: {C.RED}OFF{C.RESET}")

    print(f" {C.BOLD}\u2699\ufe0f  SAFEGUARDS{C.RESET}: {' | '.join(safeguard_parts)}")
    print(f"{C.BOLD}{C.BLUE}" + "-" * 125 + f"{C.RESET}")

    # Audio chime & alert on new signal
    if new_signals:
        play_alert_chime()
        is_short_alert = any(t["direction"] == "SHORT" for t in new_signals)
        header_bg = C.BG_RED if is_short_alert else C.BG_GREEN
        print("\n" + f"{header_bg} \ud83d\udea8 FRESH KRAKEN TRADE TRIGGERED ON THE LATEST 5-MIN CANDLE ({len(new_signals)} SETUPS) \ud83d\udea8 {C.RESET}")
        print("=" * 115)
        for t in new_signals:
            if t["direction"] == "LONG":
                dir_tag = f"\ud83d\udfe2 {C.BOLD}{C.GREEN}[LONG]{C.RESET}"
            else:
                dir_tag = f"\ud83d\udd34 {C.BOLD}{C.RED}[SHORT]{C.RESET}"
            sym_str = f"{C.BOLD}{C.WHITE}{t['symbol']:<12}{C.RESET}"
            print(f" * {dir_tag} {sym_str} | Entry: {t['entry_price']:.2f} | "
                  f"SL: {C.RED}{t['stop_loss_price']:.2f}{C.RESET} | T1 (+1.2R): {C.CYAN}{t['t1_target_price']:.2f}{C.RESET} | "
                  f"T2 (+{tgt2:.1f}R): {C.GREEN}{t['target_price']:.2f}{C.RESET}")
            print(f"   Reason: {t.get('explanation')}")
        print("=" * 115 + "\n")

    # Split into Working Trades vs Completed Trades
    working_trades = [t for t in all_captured_trades if not t.get("closed", False)]
    completed_trades = [t for t in all_captured_trades if t.get("closed", False)]

    # 1. Display ACTIVE WORKING TRADES
    print("\n" + f"{C.BOLD}{C.CYAN}" + "=" * 125 + f"{C.RESET}")
    print(f" {C.BOLD}{C.CYAN}\ud83d\udd25 [ACTIVE WORKING TRADES] IN PROGRESS RIGHT NOW{C.RESET} (Polled: {success_count}/{len(symbols)} symbols)")
    print(f"{C.BOLD}{C.CYAN}" + "=" * 125 + f"{C.RESET}")
    if working_trades:
        print(f" {'SYMBOL':<12} {'DIR':<6} {'TRIGGER':<10} {'ENTRY':<10} {'CURRENT':<10} {'P&L (PTS)':<12} {'CURR R':<10} {'STOP LOSS':<14} {'T1 (+1.2R)':<12} {'T2 (RUNNER)':<12} {'STATUS':<20}")
        print("-" * 125)
        for t in working_trades:
            cur_p = t.get("curr_price", t["entry_price"])
            init_risk = abs(t["entry_price"] - t["initial_sl"])
            sym_col = f"{C.BOLD}{C.WHITE}{t['symbol']:<12}{C.RESET}"

            if t["direction"] == "LONG":
                pnl_pts = cur_p - t["entry_price"]
                cur_r = pnl_pts / init_risk if init_risk > 0 else 0.0
                dir_col = f"{C.BOLD}{C.GREEN}{'LONG':<6}{C.RESET}"
            else:
                pnl_pts = t["entry_price"] - cur_p
                cur_r = pnl_pts / init_risk if init_risk > 0 else 0.0
                dir_col = f"{C.BOLD}{C.RED}{'SHORT':<6}{C.RESET}"

            pnl_str = f"{pnl_pts:+.2f} pts"
            r_str = f"{cur_r:+.2f}R"
            if pnl_pts > 0.001:
                pnl_col = f"{C.BOLD}{C.GREEN}{pnl_str:<12}{C.RESET}"
                r_col = f"{C.BOLD}{C.GREEN}{r_str:<10}{C.RESET}"
            elif pnl_pts < -0.001:
                pnl_col = f"{C.BOLD}{C.RED}{pnl_str:<12}{C.RESET}"
                r_col = f"{C.BOLD}{C.RED}{r_str:<10}{C.RESET}"
            else:
                pnl_col = f"{pnl_str:<12}"
                r_col = f"{r_str:<10}"

            sl_display = f"{t['stop_loss_price']:.2f}" + (" (BE)" if t["t1_hit"] else "")
            sl_col = f"{C.RED}{sl_display:<14}{C.RESET}"
            t1_col = f"{C.CYAN}{t['t1_target_price']:<12.2f}{C.RESET}"
            t2_col = f"{C.GREEN}{t['target_price']:<12.2f}{C.RESET}"

            if "T1" in t["status"]:
                st_col = f"\ud83d\udee1\ufe0f {C.BOLD}{C.CYAN}{t['status']:<16}{C.RESET}"
            elif t["direction"] == "LONG":
                st_col = f"\ud83d\udfe2 {C.BOLD}{C.GREEN}{t['status']:<16}{C.RESET}"
            else:
                st_col = f"\ud83d\udd34 {C.BOLD}{C.RED}{t['status']:<16}{C.RESET}"

            print(f" {sym_col} {dir_col} {t['trigger_time']:<10} {t['entry_price']:<10.2f} "
                  f"{C.BOLD}{cur_p:<10.2f}{C.RESET} {pnl_col} {r_col} {sl_col} {t1_col} "
                  f"{t2_col} {st_col}")
    else:
        print(f" {'No active open trades at this moment. Waiting for new setups to trigger...':^120}")

    # 2. Display COMPLETED TRADES TODAY
    print("\n" + f"{C.BOLD}{C.MAGENTA}" + "=" * 125 + f"{C.RESET}")
    print(f" {C.BOLD}{C.MAGENTA}\ud83c\udfc1 [COMPLETED TRADES TODAY] PREVIOUS TRADES CAPTURED EARLIER THIS SESSION{C.RESET}")
    print(f"{C.BOLD}{C.MAGENTA}" + "=" * 125 + f"{C.RESET}")
    if completed_trades:
        print(f" {'SYMBOL':<12} {'DIR':<6} {'TRIGGER':<10} {'ENTRY':<10} {'CURRENT':<10} {'STOP LOSS':<14} {'T1 (+1.2R)':<12} {'T2 (RUNNER)':<12} {'OUTCOME':<26}")
        print("-" * 125)
        for t in completed_trades:
            cur_p = t.get("curr_price", t["entry_price"])
            sym_col = f"{C.BOLD}{C.WHITE}{t['symbol']:<12}{C.RESET}"

            if t["direction"] == "LONG":
                dir_col = f"{C.BOLD}{C.GREEN}{'LONG':<6}{C.RESET}"
            else:
                dir_col = f"{C.BOLD}{C.RED}{'SHORT':<6}{C.RESET}"

            sl_display = f"{t['stop_loss_price']:.2f}" + (" (BE)" if t["t1_hit"] else "")
            sl_col = f"{C.RED}{sl_display:<14}{C.RESET}"
            t1_col = f"{C.CYAN}{t['t1_target_price']:<12.2f}{C.RESET}"
            t2_col = f"{C.GREEN}{t['target_price']:<12.2f}{C.RESET}"

            st = t["status"]
            # Check STOP LOSS before the generic "HIT" check.
            # "[STOP LOSS HIT]" contains the word "HIT", so the previous
            # ordering incorrectly colored it green.
            if "STOP LOSS" in st or "STOPPED" in st or "SL" in st:
                outcome_col = f"\u274c {C.BOLD}{C.RED}{st:<22}{C.RESET}"
            elif "BREAKEVEN" in st or "(BE)" in st:
                outcome_col = f"\ud83d\udee1\ufe0f {C.BOLD}{C.CYAN}{st:<22}{C.RESET}"
            elif "TARGET" in st or "HIT" in st:
                outcome_col = f"\ud83c\udfaf {C.BOLD}{C.GREEN}{st:<22}{C.RESET}"
            else:
                outcome_col = f"{st:<24}"

            print(f" {sym_col} {dir_col} {t['trigger_time']:<10} {t['entry_price']:<10.2f} "
                  f"{cur_p:<10.2f} {sl_col} {t1_col} {t2_col} {outcome_col}")
    else:
        print(f" {'No completed trades recorded yet today.':^120}")

    print(f"{C.BOLD}{C.MAGENTA}" + "=" * 125 + f"{C.RESET}\n")



def main():
    parser = argparse.ArgumentParser(description="KRAKEN Precision 3R Live FYERS Poller & Alert Engine")
    parser.add_argument("--token", type=str, default=None, help="FYERS API v3 access token")
    parser.add_argument("--rr", type=float, default=3.0, help="T2 Risk-to-Reward (default: 3.0)")
    parser.add_argument("--t1", type=float, default=1.2, help="T1 Lock-in R:R (default: 1.2)")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a loop")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    parser.add_argument("--max-trades", type=int, default=4, help="Max concurrent trades (default: 4)")
    parser.add_argument("--max-losses", type=int, default=5, help="Max total SL before circuit breaker (default: 5)")
    parser.add_argument("--cooldown", type=int, default=30, help="Per-symbol cooldown in minutes after SL (default: 30)")
    parser.add_argument("--confirm-candle", action="store_true", default=True, help="Require next candle confirmation (default: ON)")
    parser.add_argument("--no-confirm-candle", action="store_true", help="Disable confirmation candle requirement")
    args = parser.parse_args()

    confirm_candle = not args.no_confirm_candle

    if not HAS_FYERS:
        print("[ERROR] 'fyers_apiv3' package is required. Run: pip install fyers-apiv3")
        sys.exit(1)

    token = args.token or os.environ.get("FYERS_ACCESS_TOKEN") or get_token_from_db()
    client_id = settings.FYERS_CLIENT_ID

    while True:
        if token:
            fyers = fyersModel.FyersModel(
                token=token,
                is_async=False,
                client_id=client_id,
                log_path=os.path.join(PROJECT_ROOT, "temp")
            )
            try:
                profile = fyers.get_profile()
                if isinstance(profile, dict) and profile.get("s") == "ok":
                    user_name = profile.get("data", {}).get("name", "Trader")
                    print(f"\n[AUTH OK] Connected to FYERS account: {user_name}")
                    break
                else:
                    err_msg = profile.get("message", "Invalid or expired access token")
                    print(f"\n[AUTH EXPIRED] FYERS Token Status: {err_msg}")
            except Exception as e:
                print(f"[AUTH ERROR] Token verification failed: {e}")

        print("\n" + "=" * 80)
        print(" [ACTION REQUIRED] FYERS Authentication Needed")
        print("=" * 80)
        print(" Choose an authentication option:")
        print("   [1] Automatic Browser Login (Opens FYERS login page & handles callback)")
        print("   [2] Paste Token Manually")
        print("   [3] Exit")
        print("=" * 80)

        choice = input("Select option [1/2/3] (Default 1): ").strip()
        if choice in ("", "1"):
            from script.fyers_login_helper import run_oauth_flow
            token = run_oauth_flow()
            if not token:
                print("[ERROR] Browser login did not complete.")
                sys.exit(1)
        elif choice == "2":
            token = input("Please paste your fresh FYERS Access Token: ").strip()
            if not token:
                print("[INFO] Exiting.")
                sys.exit(0)
        else:
            print("[INFO] Exiting.")
            sys.exit(0)

    engine = KrakenStrategyEngine(target_rr=args.rr, t1_rr=args.t1)

    print("===============================================================================")
    print(" KRAKEN PRECISION 3R LIVE FYERS POLLER DAEMON")
    print(f" Timeframe: 5m | Target 1: +{args.t1:.1f}R | Target 2: +{args.rr:.1f}R | Interval: {args.interval}s")
    print(f" Universe: {len(NIFTY_SYMBOLS)} Liquid Nifty 50 Instruments")
    print(f" Safeguards: Max Trades={args.max_trades} | Max SL={args.max_losses} | Cooldown={args.cooldown}m | Confirm={'ON' if confirm_candle else 'OFF'}")
    print("===============================================================================")

    if args.loop:
        print(f"[INFO] Running live polling loop every {args.interval}s. Press Ctrl+C to exit.")
        try:
            while True:
                scan_live_market(
                    fyers, engine, NIFTY_SYMBOLS,
                    max_concurrent=args.max_trades,
                    confirm_candle=confirm_candle,
                )
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[INFO] Poller stopped by user.")
    else:
        scan_live_market(
            fyers, engine, NIFTY_SYMBOLS,
            max_concurrent=args.max_trades,
            confirm_candle=confirm_candle,
        )


if __name__ == "__main__":
    main()

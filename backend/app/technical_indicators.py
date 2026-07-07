"""Pure technical-analysis math — no HTTP, no state.

A Python port of the indicator library FinSurfing (github.com/surfingalien/
finsurfing, lib/technical-indicators.js) uses to feed its AI analysis engine.
Ported natively here so GainzAI can run the same style of analysis locally,
against Coinbase's own public candle data, without calling out to FinSurfing
or needing any FinSurfing credentials.
"""
import math
from typing import Any, Dict, List, Optional

KEY_PATTERNS = [
    "strong_uptrend", "strong_downtrend", "golden_cross", "death_cross",
    "20bar_breakout_up", "20bar_breakout_down", "volume_spike", "bb_squeeze",
    "bullish_engulfing", "bearish_engulfing",
]


def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if not closes or len(closes) < period + 1:
        return None

    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff

    avg_gain = gains / period
    avg_loss = losses / period

    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0
        loss = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def compute_ema(closes: List[float], period: int) -> Optional[float]:
    arr = compute_ema_array(closes, period)
    return arr[-1] if arr and not math.isnan(arr[-1]) else None


def compute_ema_array(closes: List[float], period: int) -> List[float]:
    if not closes or len(closes) < period:
        return []
    k = 2 / (period + 1)
    result = [float("nan")] * len(closes)
    ema = sum(closes[:period]) / period
    result[period - 1] = ema
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = ema
    return result


def compute_macd(closes: List[float]) -> Optional[Dict[str, Any]]:
    if not closes or len(closes) < 35:
        return None

    ema12 = compute_ema_array(closes, 12)
    ema26 = compute_ema_array(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(closes)) if not math.isnan(ema12[i]) and not math.isnan(ema26[i])]
    if len(macd_line) < 9:
        return None

    def ema9_of(series: List[float]) -> float:
        k = 2 / 10
        val = sum(series[:9]) / 9
        for v in series[9:]:
            val = v * k + val * (1 - k)
        return val

    signal = ema9_of(macd_line)
    macd_val = macd_line[-1]
    prev_macd = macd_line[-2]
    histogram = macd_val - signal

    prev_signal = ema9_of(macd_line[:-1]) if len(macd_line) >= 2 else signal
    prev_histogram = prev_macd - prev_signal

    return {
        "macd": round(macd_val, 4),
        "signal": round(signal, 4),
        "histogram": round(histogram, 4),
        "trend": "bullish" if macd_val > signal else "bearish",
        "histogramDir": "increasing" if histogram > prev_histogram else "decreasing",
    }


def compute_bb(closes: List[float], period: int = 20, mult: float = 2.0) -> Optional[Dict[str, Any]]:
    if not closes or len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    std_dev = math.sqrt(variance)

    upper = mean + mult * std_dev
    lower = mean - mult * std_dev
    price = closes[-1]
    bandwidth = 0 if std_dev == 0 else (upper - lower) / mean
    pct_b = 50 if upper == lower else (price - lower) / (upper - lower) * 100

    position = "upper" if price >= upper else "lower" if price <= lower else "middle"
    squeeze = bandwidth < 0.02

    return {
        "upper": round(upper, 4), "middle": round(mean, 4), "lower": round(lower, 4),
        "bandwidth": round(bandwidth, 4), "pctB": round(pct_b, 2),
        "position": position, "squeeze": squeeze,
    }


def compute_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if not highs or len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        hl = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i] - closes[i - 1])
        trs.append(max(hl, hpc, lpc))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 4)


def compute_stoch_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if not closes or len(closes) < period * 2 + 1:
        return None
    rsi_history = []
    for i in range(period, len(closes)):
        rsi = compute_rsi(closes[i - period:i + 1], period)
        if rsi is not None:
            rsi_history.append(rsi)
    if len(rsi_history) < period:
        return None
    window = rsi_history[-period:]
    min_rsi, max_rsi = min(window), max(window)
    current = window[-1]
    if max_rsi == min_rsi:
        return 50.0
    return round((current - min_rsi) / (max_rsi - min_rsi) * 100, 2)


def compute_uo_array(highs: List[float], lows: List[float], closes: List[float],
                     s1: int = 7, s2: int = 14, s3: int = 28) -> List[float]:
    """Ultimate Oscillator (Larry Williams) as a per-bar array, NaN until warm.

    Combines buying pressure over three lookbacks (7/14/28) so it reacts to
    short-term reversals without the whipsaw of a single-period oscillator.
    """
    n = len(closes)
    if n < s3 + 1:
        return [float("nan")] * n

    bp = [float("nan")]  # buying pressure, index-aligned to closes (bar 0 undefined)
    tr = [float("nan")]
    for i in range(1, n):
        low_or_pc = min(lows[i], closes[i - 1])
        high_or_pc = max(highs[i], closes[i - 1])
        bp.append(closes[i] - low_or_pc)
        tr.append(high_or_pc - low_or_pc)

    out = [float("nan")] * n
    for i in range(s3, n):
        def avg(period: int) -> float:
            bp_sum = sum(bp[i - period + 1:i + 1])
            tr_sum = sum(tr[i - period + 1:i + 1])
            return bp_sum / tr_sum if tr_sum else 0.0

        out[i] = 100 * (4 * avg(s1) + 2 * avg(s2) + avg(s3)) / 7
    return out


def compute_ultimate_oscillator(highs: List[float], lows: List[float], closes: List[float],
                                s1: int = 7, s2: int = 14, s3: int = 28) -> Optional[Dict[str, Any]]:
    """Latest Ultimate Oscillator value plus the prior bar's, so callers can
    detect a fresh cross up out of oversold territory (the entry trigger)."""
    arr = compute_uo_array(highs, lows, closes, s1, s2, s3)
    if not arr or math.isnan(arr[-1]):
        return None
    current = round(arr[-1], 2)
    prev = round(arr[-2], 2) if len(arr) >= 2 and not math.isnan(arr[-2]) else None
    return {"uo": current, "prev": prev}


def compute_vwap(highs: List[float], lows: List[float], closes: List[float], volumes: List[float]) -> Optional[float]:
    if not highs:
        return None
    length = min(len(highs), 50)
    start = len(highs) - length
    tpv_sum = vol_sum = 0.0
    for i in range(start, len(highs)):
        tp = (highs[i] + lows[i] + closes[i]) / 3
        vol = volumes[i] or 0
        tpv_sum += tp * vol
        vol_sum += vol
    if vol_sum == 0:
        return None
    return round(tpv_sum / vol_sum, 4)


def compute_obv(closes: List[float], volumes: List[float]) -> Optional[Dict[str, Any]]:
    if not closes or len(closes) < 2:
        return None
    obv = 0.0
    obv_arr = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv += volumes[i] or 0
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i] or 0
        obv_arr.append(obv)
    lookback = min(20, len(obv_arr) - 1)
    prev20 = obv_arr[-1 - lookback]
    return {"current": obv, "trend": "rising" if obv > prev20 else "falling"}


def find_support_resistance(highs: List[float], lows: List[float], closes: List[float]) -> Dict[str, Optional[float]]:
    if not highs or len(highs) < 7:
        return {"support": None, "resistance": None}
    length = min(len(highs), 100)
    start = len(highs) - length
    price = closes[-1]
    window = 3
    pivot_highs, pivot_lows = [], []

    for i in range(start + window, len(highs) - window):
        is_high = all(highs[i] > highs[i - j] and highs[i] > highs[i + j] for j in range(1, window + 1))
        is_low = all(lows[i] < lows[i - j] and lows[i] < lows[i + j] for j in range(1, window + 1))
        if is_high:
            pivot_highs.append(highs[i])
        if is_low:
            pivot_lows.append(lows[i])

    resistance_candidates = sorted(v for v in pivot_highs if v > price)
    support_candidates = sorted((v for v in pivot_lows if v < price), reverse=True)

    return {
        "support": round(support_candidates[0], 4) if support_candidates else None,
        "resistance": round(resistance_candidates[0], 4) if resistance_candidates else None,
    }


def volume_analysis(volumes: List[float]) -> Optional[Dict[str, Any]]:
    if not volumes or len(volumes) < 2:
        return None
    n = len(volumes)
    current = volumes[-1]
    window20 = volumes[max(0, n - 21):n - 1]
    avg20 = sum(window20) / len(window20) if window20 else 0
    ratio = round(current / avg20, 2) if avg20 > 0 else 0

    trend = "neutral"
    if n >= 10:
        recent5 = sum(volumes[-5:]) / 5
        prior5 = sum(volumes[-10:-5]) / 5
        if recent5 > prior5 * 1.1:
            trend = "increasing"
        elif recent5 < prior5 * 0.9:
            trend = "decreasing"

    return {"current": current, "avg20": round(avg20), "ratio": ratio, "trend": trend, "spike": ratio > 2}


def compute_adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[int]:
    n = len(closes)
    if n < period * 2 + 1:
        return None

    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        ph, pl = highs[i - 1], lows[i - 1]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, down = h - ph, pl - l
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)

    s_tr = sum(tr[:period])
    s_pdm = sum(plus_dm[:period])
    s_mdm = sum(minus_dm[:period])

    def calc_dx(s_tr_, s_pdm_, s_mdm_):
        if s_tr_ == 0:
            return 0
        di_p = s_pdm_ / s_tr_ * 100
        di_m = s_mdm_ / s_tr_ * 100
        total = di_p + di_m
        return abs(di_p - di_m) / total * 100 if total > 0 else 0

    dx_arr = [calc_dx(s_tr, s_pdm, s_mdm)]
    for i in range(period, len(tr)):
        s_tr = s_tr - s_tr / period + tr[i]
        s_pdm = s_pdm - s_pdm / period + plus_dm[i]
        s_mdm = s_mdm - s_mdm / period + minus_dm[i]
        dx_arr.append(calc_dx(s_tr, s_pdm, s_mdm))

    if len(dx_arr) < period:
        return None

    adx = sum(dx_arr[:period]) / period
    for dx in dx_arr[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx)


def detect_patterns(opens: List[float], highs: List[float], lows: List[float],
                     closes: List[float], volumes: List[float]) -> List[str]:
    patterns: List[str] = []
    n = len(closes)
    if n < 3:
        return patterns

    price = closes[-1]
    ema50_arr = compute_ema_array(closes, 50)
    ema200_arr = compute_ema_array(closes, 200)
    ema21_arr = compute_ema_array(closes, 21)
    e50 = ema50_arr[-1] if ema50_arr else float("nan")
    e200 = ema200_arr[-1] if ema200_arr else float("nan")
    e21 = ema21_arr[-1] if ema21_arr else float("nan")

    if not math.isnan(e50) and not math.isnan(e200):
        if price > e50 and price > e200 and e50 > e200:
            patterns.append("strong_uptrend")
        elif price < e50 and price < e200 and e50 < e200:
            patterns.append("strong_downtrend")
    if not math.isnan(e50):
        patterns.append("above_ema50" if price > e50 else "below_ema50")
    if not math.isnan(e200):
        patterns.append("above_ema200" if price > e200 else "below_ema200")

    if not math.isnan(e21) and not math.isnan(e50) and n >= 2 and len(ema21_arr) >= 2 and len(ema50_arr) >= 2:
        prev_e21, prev_e50 = ema21_arr[-2], ema50_arr[-2]
        if not math.isnan(prev_e21) and not math.isnan(prev_e50):
            if e21 > e50 and prev_e21 <= prev_e50:
                patterns.append("golden_cross")
            if e21 < e50 and prev_e21 >= prev_e50:
                patterns.append("death_cross")

    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    rng = h - l
    body = abs(c - o)
    up_wick = h - max(c, o)
    dn_wick = min(c, o) - l

    if rng > 0:
        if body / rng < 0.1:
            patterns.append("doji")
        if dn_wick > body * 2 and up_wick < body * 0.5:
            patterns.append("hammer")
        if up_wick > body * 2 and dn_wick < body * 0.5:
            patterns.append("shooting_star")
        if c > o and body / rng > 0.7:
            patterns.append("strong_bull_candle")
        if c < o and body / rng > 0.7:
            patterns.append("strong_bear_candle")

        if n >= 2:
            po, pc = opens[-2], closes[-2]
            if pc < po and c > o and o < pc and c > po:
                patterns.append("bullish_engulfing")
            if pc > po and c < o and o > pc and c < po:
                patterns.append("bearish_engulfing")

    if n >= 21:
        last20_highs = highs[n - 21:n - 1]
        last20_lows = lows[n - 21:n - 1]
        if h > max(last20_highs):
            patterns.append("20bar_breakout_up")
        if l < min(last20_lows):
            patterns.append("20bar_breakout_down")

    if volumes and len(volumes) >= 21:
        avg_vol20 = sum(volumes[n - 21:n - 1]) / 20
        cur_vol = volumes[-1]
        if avg_vol20 > 0 and cur_vol > avg_vol20 * 2:
            patterns.append("volume_spike")
            patterns.append("high_vol_bull" if c > o else "high_vol_bear")

    bb = compute_bb(closes)
    if bb and bb["squeeze"]:
        patterns.append("bb_squeeze")

    return patterns


def compute_all(opens: List[float], highs: List[float], lows: List[float],
                 closes: List[float], volumes: List[float]) -> Dict[str, Any]:
    """Everything the analysis engine needs, in one call."""
    return {
        "rsi": compute_rsi(closes),
        "macd": compute_macd(closes),
        "ema9": compute_ema(closes, 9),
        "ema21": compute_ema(closes, 21),
        "ema50": compute_ema(closes, 50),
        "ema200": compute_ema(closes, 200),
        "bb": compute_bb(closes),
        "atr": compute_atr(highs, lows, closes),
        "stoch_rsi": compute_stoch_rsi(closes),
        "uo": compute_ultimate_oscillator(highs, lows, closes),
        "vwap": compute_vwap(highs, lows, closes, volumes),
        "obv": compute_obv(closes, volumes),
        "sr": find_support_resistance(highs, lows, closes),
        "volume": volume_analysis(volumes),
        "adx": compute_adx(highs, lows, closes),
        "patterns": detect_patterns(opens, highs, lows, closes, volumes),
    }

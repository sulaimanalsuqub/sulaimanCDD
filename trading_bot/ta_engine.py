"""
ta_engine.py — مؤشرات تقنية من Binance OHLCV (RSI, MACD, EMA, Bollinger)
"""
import pandas as pd
try:
    import pandas_ta as ta
    HAS_TA = True
except ImportError:
    HAS_TA = False

from binance.client import Client as BinanceClient


def get_ohlcv(client: BinanceClient, symbol: str, interval: str = "15m", limit: int = 120) -> pd.DataFrame:
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","num_trades","taker_buy_base","taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def get_ta_signal(client: BinanceClient, symbol: str, interval: str = "15m") -> dict:
    if not HAS_TA:
        return {"error": "pandas_ta not installed", "symbol": symbol}
    try:
        df = get_ohlcv(client, symbol, interval=interval, limit=120)

        df.ta.rsi(length=14, append=True)
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.tema(length=9, append=True)
        df.ta.macd(fast=12, slow=26, signal=9, append=True)
        df.ta.bbands(length=20, std=2, append=True)
        df.ta.stoch(k=14, d=3, smooth_k=3, append=True)
        df.ta.atr(length=14, append=True)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        rsi       = float(last.get("RSI_14")        or 50)
        ema20     = float(last.get("EMA_20")        or 0)
        ema50     = float(last.get("EMA_50")        or 0)
        price     = float(last["close"])
        macd_val  = float(last.get("MACD_12_26_9")  or 0)
        macd_sig  = float(last.get("MACDs_12_26_9") or 0)
        macd_hist = float(last.get("MACDh_12_26_9") or 0)
        prev_hist = float(prev.get("MACDh_12_26_9") or 0)
        bb_upper  = float(last.get("BBU_20_2.0_2.0")    or 0)
        bb_lower  = float(last.get("BBL_20_2.0_2.0")    or 0)
        bb_mid    = float(last.get("BBM_20_2.0_2.0")    or 0)
        tema9     = float(last.get("TEMA_9")            or 0)
        stoch_k   = float(last.get("STOCHk_14_3_3")     or 50)
        stoch_d   = float(last.get("STOCHd_14_3_3")     or 50)
        atr       = float(last.get("ATRr_14")           or 0)

        buy_signals  = 0
        sell_signals = 0
        reasons      = []

        # RSI
        if rsi < 40:
            buy_signals += 1
            reasons.append(f"RSI ذروة بيع ({rsi:.1f})")
        elif rsi > 65:
            sell_signals += 1
            reasons.append(f"RSI ذروة شراء ({rsi:.1f})")

        # EMA20
        if ema20 > 0:
            if price > ema20:
                buy_signals += 1
                reasons.append("السعر فوق EMA20")
            else:
                sell_signals += 1

        # EMA50 trend
        if ema20 > 0 and ema50 > 0 and ema20 > ema50:
            buy_signals += 1
            reasons.append("EMA20 فوق EMA50 (صاعد)")

        # MACD crossover
        if macd_hist > 0 and prev_hist <= 0:
            buy_signals += 2
            reasons.append("MACD تقاطع صاعد")
        elif macd_hist > 0:
            buy_signals += 1
            reasons.append("MACD إيجابي")
        elif macd_hist < 0:
            sell_signals += 1

        # Bollinger Bands + BB Position
        bb_pct = 0.5
        if bb_upper > bb_lower > 0:
            bb_pct = (price - bb_lower) / (bb_upper - bb_lower)
        if bb_lower > 0 and price < bb_lower:
            buy_signals += 1
            reasons.append("تحت حد Bollinger السفلي")
        elif bb_mid > 0 and price < bb_mid:
            buy_signals += 0.5
            reasons.append("تحت منتصف Bollinger")
        elif bb_upper > 0 and price > bb_upper:
            sell_signals += 1

        # Stochastic Fast
        if stoch_k < 30 and stoch_d < 30:
            buy_signals += 1
            reasons.append(f"Stochastic ذروة بيع ({stoch_k:.1f})")
        elif stoch_k > 75 and stoch_d > 75:
            sell_signals += 1

        # TEMA trend
        if tema9 > 0 and price > tema9:
            buy_signals += 1
            reasons.append("فوق TEMA9 (اتجاه صاعد)")
        elif tema9 > 0 and price < tema9:
            sell_signals += 1

        max_signals = 8  # RSI + EMA20 + EMA50 + MACD×2 + BB + Stoch + TEMA
        score = round(buy_signals / max_signals, 2)

        if score >= 0.5:
            signal_label = "شراء"
            signal_color = "green"
        elif sell_signals >= 3:
            signal_label = "بيع"
            signal_color = "red"
        else:
            signal_label = "انتظار"
            signal_color = "yellow"

        return {
            "symbol":       symbol,
            "interval":     interval,
            "price":        round(price, 8),
            "rsi":          round(rsi, 2),
            "ema20":        round(ema20, 8),
            "ema50":        round(ema50, 8),
            "macd":         round(macd_val, 8),
            "macd_signal":  round(macd_sig, 8),
            "macd_hist":    round(macd_hist, 8),
            "bb_upper":     round(bb_upper, 8),
            "bb_lower":     round(bb_lower, 8),
            "bb_pct":       round(bb_pct, 4),
            "tema":         round(tema9, 8),
            "stoch_k":      round(stoch_k, 2),
            "stoch_d":      round(stoch_d, 2),
            "atr":          round(atr, 8),
            "buy_signals":  buy_signals,
            "sell_signals": sell_signals,
            "ta_score":     score,
            "signal":       signal_label,
            "signal_color": signal_color,
            "reasons":      reasons,
        }
    except Exception as e:
        return {"error": str(e), "symbol": symbol}

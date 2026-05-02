"""
ta_engine.py — مؤشرات فنية خفيفة للمضاربة الدقيقة مع caching وbackoff.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException, BinanceRequestException
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
SCALP_CANDLE_INTERVAL = os.getenv("SCALP_CANDLE_INTERVAL", "1m")
PRICE_CACHE_SECONDS = max(1.0, float(os.getenv("PRICE_CACHE_SECONDS", "20")))
TA_CACHE_SECONDS = max(5.0, float(os.getenv("TA_CACHE_SECONDS", "50")))
TA_KLINE_LIMIT = max(50, int(os.getenv("TA_KLINE_LIMIT", "120")))
SCALP_RATE_LIMIT_BACKOFF_MINUTES = max(
    1.0,
    float(os.getenv("SCALP_RATE_LIMIT_BACKOFF_MINUTES", "3")),
)

_client: BinanceClient | None = None
_price_cache: dict[str, tuple[float, float]] = {}
_ta_cache: dict[tuple[str, str, int], tuple[float, dict]] = {}
_backoff_until = 0.0


class BinanceRateLimited(RuntimeError):
    """يرفع عند ظهور rate limit من Binance."""


@dataclass
class Candle:
    close: float


def now_ts() -> float:
    return time.monotonic()


def get_backoff_remaining_seconds() -> float:
    return max(0.0, _backoff_until - now_ts())


def set_rate_limit_backoff() -> None:
    global _backoff_until
    _backoff_until = now_ts() + SCALP_RATE_LIMIT_BACKOFF_MINUTES * 60
    logger.warning(
        f"[TA] Binance rate limit — إيقاف مؤقت للمضاربة "
        f"{SCALP_RATE_LIMIT_BACKOFF_MINUTES:.1f} دقائق"
    )


def _raise_if_backoff() -> None:
    remaining = get_backoff_remaining_seconds()
    if remaining > 0:
        raise BinanceRateLimited(f"Binance backoff نشط، المتبقي {remaining:.0f}ث")


def _is_rate_limit(exc: Exception) -> bool:
    if isinstance(exc, BinanceAPIException):
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        return status in (418, 429) or code in (-1003, -1015)
    text = str(exc).lower()
    return "rate limit" in text or "too many requests" in text


def _handle_binance_error(exc: Exception) -> None:
    if _is_rate_limit(exc):
        set_rate_limit_backoff()
        raise BinanceRateLimited(str(exc)) from exc
    raise exc


def get_client() -> BinanceClient:
    global _client
    if _client is None:
        _client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    return _client


def get_price(symbol: str) -> float:
    """يجلب السعر مع cache قصير لتخفيف ضغط Binance."""
    _raise_if_backoff()
    symbol = symbol.upper()
    cached = _price_cache.get(symbol)
    now = now_ts()
    if cached and now - cached[0] <= PRICE_CACHE_SECONDS:
        return cached[1]

    try:
        ticker = get_client().get_symbol_ticker(symbol=symbol)
    except (BinanceAPIException, BinanceRequestException) as exc:
        _handle_binance_error(exc)

    price = float(ticker["price"])
    _price_cache[symbol] = (now, price)
    return price


def get_candles(symbol: str, interval: str, limit: int = TA_KLINE_LIMIT) -> list[Candle]:
    _raise_if_backoff()
    try:
        klines = get_client().get_klines(symbol=symbol, interval=interval, limit=limit)
    except (BinanceAPIException, BinanceRequestException) as exc:
        _handle_binance_error(exc)
    return [Candle(close=float(item[4])) for item in klines]


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for previous, current in zip(values[-period - 1:-1], values[-period:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values: list[float]) -> tuple[float, float]:
    if len(values) < 35:
        return 0.0, 0.0
    macd_line = ema(values, 12) - ema(values, 26)
    macd_series = []
    for index in range(35, len(values) + 1):
        window = values[:index]
        macd_series.append(ema(window, 12) - ema(window, 26))
    signal = ema(macd_series, 9)
    return macd_line, signal


def classify_signal(values: list[float]) -> tuple[str, int]:
    current_rsi = rsi(values)
    ema_fast = ema(values, 9)
    ema_slow = ema(values, 21)
    macd_line, macd_signal = macd(values)

    bullish_score = 0
    bearish_score = 0

    if ema_fast > ema_slow:
        bullish_score += 35
    else:
        bearish_score += 35

    if macd_line > macd_signal:
        bullish_score += 30
    else:
        bearish_score += 30

    if 45 <= current_rsi <= 68:
        bullish_score += 20
    elif 32 <= current_rsi < 45:
        bearish_score += 20
    elif current_rsi > 75:
        bearish_score += 15
    elif current_rsi < 28:
        bullish_score += 15

    if bullish_score >= 65:
        return "buy", min(100, bullish_score)
    if bearish_score >= 65:
        return "sell", min(100, bearish_score)
    return "hold", max(bullish_score, bearish_score)


def get_indicators(
    symbol: str,
    interval: str = SCALP_CANDLE_INTERVAL,
    limit: int = TA_KLINE_LIMIT,
) -> dict[str, Any]:
    """يرجع مؤشرات الرمز مع cache لتجنب طلبات متكررة."""
    symbol = symbol.upper()
    key = (symbol, interval, limit)
    now = now_ts()
    cached = _ta_cache.get(key)
    if cached and now - cached[0] <= TA_CACHE_SECONDS:
        result = dict(cached[1])
        result["cached"] = True
        result["cache_age_seconds"] = round(now - cached[0], 2)
        return result

    candles = get_candles(symbol, interval, limit)
    closes = [c.close for c in candles]
    current_rsi = rsi(closes)
    ema_fast = ema(closes, 9)
    ema_slow = ema(closes, 21)
    macd_line, macd_signal = macd(closes)
    signal, confidence = classify_signal(closes)

    result = {
        "symbol": symbol,
        "interval": interval,
        "price": closes[-1],
        "rsi": round(current_rsi, 4),
        "ema_fast": round(ema_fast, 8),
        "ema_slow": round(ema_slow, 8),
        "macd": round(macd_line, 8),
        "macd_signal": round(macd_signal, 8),
        "signal": signal,
        "confidence": confidence,
        "cached": False,
        "cache_age_seconds": 0,
    }
    _ta_cache[key] = (now, result)
    _price_cache[symbol] = (now, result["price"])
    return result


def get_market_snapshot(symbols: list[str], interval: str = SCALP_CANDLE_INTERVAL) -> list[dict]:
    snapshots = []
    for symbol in symbols:
        snapshots.append(get_indicators(symbol, interval=interval))
    return snapshots

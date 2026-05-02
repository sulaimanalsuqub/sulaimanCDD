"""
scalper.py — مضارب دقيقة واحدة مع TP/SL/Trailing وcache وrate-limit backoff.

التنفيذ الحقيقي لا يحدث إلا إذا كان:
TRADING_ENABLED=true و SCALP_REAL_EXECUTION=true
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import anthropic
from dotenv import load_dotenv
from loguru import logger

import database as db
import ta_engine
import trader

load_dotenv()

SCALP_ENABLED = os.getenv("SCALP_ENABLED", "true").lower() == "true"
SCALP_INTERVAL_MINUTES = max(1, int(os.getenv("SCALP_INTERVAL_MINUTES", "1")))
SCALP_CANDLE_INTERVAL = os.getenv("SCALP_CANDLE_INTERVAL", "1m")
SCALP_SYMBOLS = [
    item.strip().upper()
    for item in os.getenv("SCALP_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
    if item.strip()
]
SCALP_MIN_CONFIDENCE = int(os.getenv("SCALP_MIN_CONFIDENCE", "70"))
SCALP_TRADE_USDT = float(os.getenv("SCALP_TRADE_USDT", "15"))
SCALP_MAX_NEW_TRADES_PER_MINUTE = max(
    1,
    int(os.getenv("SCALP_MAX_NEW_TRADES_PER_MINUTE", "1")),
)
SCALP_STOP_LOSS_PCT = float(os.getenv("SCALP_STOP_LOSS_PCT", os.getenv("STOP_LOSS_PCT", "1.0")))
SCALP_TAKE_PROFIT_PCT = float(os.getenv("SCALP_TAKE_PROFIT_PCT", os.getenv("TAKE_PROFIT_PCT", "1.5")))
SCALP_TRAILING_PCT = float(os.getenv("SCALP_TRAILING_PCT", "0.7"))
SCALP_MAX_OPEN_POSITIONS = max(1, int(os.getenv("SCALP_MAX_OPEN_POSITIONS", "3")))
SCALP_REAL_EXECUTION = os.getenv("SCALP_REAL_EXECUTION", "false").lower() == "true"
TRADING_ENABLED = os.getenv("TRADING_ENABLED", "false").lower() == "true"
USE_FUTURES = os.getenv("BINANCE_FUTURES", "false").lower() == "true"
AI_CONFIRMATION = os.getenv("AI_CONFIRMATION", "false").lower() == "true"
AI_CONFIRMATION_TIMEOUT_SECONDS = min(
    8.0,
    max(1.0, float(os.getenv("AI_CONFIRMATION_TIMEOUT_SECONDS", "8"))),
)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")

_last_new_trade_bucket: int | None = None


def _float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _minute_bucket() -> int:
    return int(time.time() // 60)


def _last_db_trade_in_current_minute() -> bool:
    opened_at = db.get_last_scalp_opened_at()
    if not opened_at:
        return False
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return int(opened_at.timestamp() // 60) == _minute_bucket()


def can_open_new_trade(open_positions: list[dict]) -> bool:
    """يضمن عدم فتح أكثر من صفقة جديدة في الدقيقة وعدم تجاوز عدد المراكز."""
    if len(open_positions) >= SCALP_MAX_OPEN_POSITIONS:
        return False
    if _last_new_trade_bucket == _minute_bucket():
        return False
    return not _last_db_trade_in_current_minute()


def _side_from_signal(signal: str) -> str | None:
    if signal == "buy":
        return "long"
    if signal == "sell" and USE_FUTURES:
        return "short"
    return None


def _risk_prices(side: str, entry_price: float) -> tuple[float, float, float]:
    if side == "long":
        stop_loss = entry_price * (1 - SCALP_STOP_LOSS_PCT / 100)
        take_profit = entry_price * (1 + SCALP_TAKE_PROFIT_PCT / 100)
        trailing_stop = entry_price * (1 - SCALP_TRAILING_PCT / 100)
    else:
        stop_loss = entry_price * (1 + SCALP_STOP_LOSS_PCT / 100)
        take_profit = entry_price * (1 - SCALP_TAKE_PROFIT_PCT / 100)
        trailing_stop = entry_price * (1 + SCALP_TRAILING_PCT / 100)
    return round(stop_loss, 8), round(take_profit, 8), round(trailing_stop, 8)


def _position_pnl(position: dict, close_price: float) -> float:
    entry = _float(position["entry_price"])
    qty = _float(position["quantity"])
    if position["side"] == "long":
        return round((close_price - entry) * qty, 8)
    return round((entry - close_price) * qty, 8)


def _confirm_with_ai(snapshot: dict) -> bool:
    """تأكيد اختياري من Claude مع timeout لا يتجاوز 8 ثواني."""
    if not AI_CONFIRMATION:
        return True
    if not ANTHROPIC_API_KEY:
        logger.warning("[Scalper] AI_CONFIRMATION=true لكن ANTHROPIC_API_KEY غير موجود — تخطي الصفقة")
        return False

    prompt = {
        "task": "Confirm or reject a one-minute crypto scalp signal. Return JSON only.",
        "expected_schema": {"confirm": True, "reason": "short"},
        "snapshot": snapshot,
    }
    try:
        client = anthropic.Anthropic(
            api_key=ANTHROPIC_API_KEY,
            timeout=AI_CONFIRMATION_TIMEOUT_SECONDS,
        )
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=120,
            temperature=0,
            system="Return compact valid JSON only. Do not explain outside JSON.",
            messages=[{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        )
        data = json.loads(message.content[0].text)
        return bool(data.get("confirm"))
    except Exception as exc:
        logger.warning(
            f"[Scalper] تأكيد Claude فشل/انتهت مهلته خلال "
            f"{AI_CONFIRMATION_TIMEOUT_SECONDS:.0f}ث — تخطي الصفقة: {exc}"
        )
        return False


def _execute_open_order(symbol: str, side: str, quantity: float) -> str | None:
    if not (TRADING_ENABLED and SCALP_REAL_EXECUTION):
        return None

    client = trader.get_binance_client()
    order_side = "BUY" if side == "long" else "SELL"
    if USE_FUTURES:
        order = trader.execute_futures_order(client, symbol, order_side, quantity)
    else:
        order = trader.execute_spot_order(client, symbol, order_side, quantity)
    return str(order.get("orderId", ""))


def _execute_close_order(position: dict) -> str | None:
    if not (TRADING_ENABLED and SCALP_REAL_EXECUTION):
        return None

    client = trader.get_binance_client()
    side = "SELL" if position["side"] == "long" else "BUY"
    quantity = _float(position["quantity"])
    symbol = position["symbol"]
    if USE_FUTURES:
        order = trader.execute_futures_order(client, symbol, side, quantity)
    else:
        order = trader.execute_spot_order(client, symbol, side, quantity)
    return str(order.get("orderId", ""))


def monitor_open_positions() -> int:
    """يراقب TP/SL/Trailing لكل المراكز المفتوحة في كل دورة."""
    closed = 0
    positions = db.get_open_scalp_positions()
    for position in positions:
        symbol = position["symbol"]
        try:
            price = ta_engine.get_price(symbol)
        except ta_engine.BinanceRateLimited:
            raise
        except Exception as exc:
            logger.warning(f"[Scalper] تعذر تحديث سعر {symbol}: {exc}")
            continue

        side = position["side"]
        highest = max(_float(position.get("highest_price")), price)
        lowest = min(_float(position.get("lowest_price")) or price, price)
        trailing = _float(position.get("trailing_stop"))
        reason = None

        if side == "long":
            trailing = max(trailing, highest * (1 - SCALP_TRAILING_PCT / 100))
            if price >= _float(position["take_profit"]):
                reason = "take_profit"
            elif price <= _float(position["stop_loss"]):
                reason = "stop_loss"
            elif price <= trailing:
                reason = "trailing_stop"
        else:
            trailing = min(trailing or price, lowest * (1 + SCALP_TRAILING_PCT / 100))
            if price <= _float(position["take_profit"]):
                reason = "take_profit"
            elif price >= _float(position["stop_loss"]):
                reason = "stop_loss"
            elif price >= trailing:
                reason = "trailing_stop"

        if reason:
            close_order_id = _execute_close_order(position)
            pnl = _position_pnl(position, price)
            db.update_scalp_position(
                position["id"],
                status="closed",
                closed_at="NOW()",
                close_price=price,
                close_reason=reason,
                pnl=pnl,
                close_order_id=close_order_id,
                highest_price=highest,
                lowest_price=lowest,
                trailing_stop=round(trailing, 8),
            )
            closed += 1
            logger.info(f"[Scalper] إغلاق {symbol} بسبب {reason} | PnL={pnl:.4f}")
        else:
            db.update_scalp_position(
                position["id"],
                highest_price=highest,
                lowest_price=lowest,
                trailing_stop=round(trailing, 8),
            )
    return closed


def open_position_from_snapshot(snapshot: dict) -> int | None:
    global _last_new_trade_bucket

    signal = snapshot.get("signal")
    confidence = int(snapshot.get("confidence") or 0)
    side = _side_from_signal(signal)
    if not side or confidence < SCALP_MIN_CONFIDENCE:
        return None
    if not _confirm_with_ai(snapshot):
        return None

    symbol = snapshot["symbol"]
    entry = float(snapshot["price"])
    quantity = round(SCALP_TRADE_USDT / entry, 10)
    stop_loss, take_profit, trailing_stop = _risk_prices(side, entry)
    mode = "live" if TRADING_ENABLED and SCALP_REAL_EXECUTION else "paper"
    order_id = _execute_open_order(symbol, side, quantity)

    position_id = db.save_scalp_position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        amount_usdt=SCALP_TRADE_USDT,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        trailing_stop=trailing_stop,
        mode=mode,
        open_order_id=order_id,
    )
    _last_new_trade_bucket = _minute_bucket()
    logger.info(
        f"[Scalper] فتح {mode} {side} {symbol} | entry={entry} "
        f"TP={take_profit} SL={stop_loss} trailing={trailing_stop}"
    )
    return position_id


def run() -> dict:
    """دورة مضاربة واحدة. مصممة للتشغيل كل دقيقة من scheduler.py."""
    if not SCALP_ENABLED:
        return {"enabled": False, "opened": 0, "closed": 0}

    if not SCALP_SYMBOLS:
        return {"enabled": True, "opened": 0, "closed": 0, "error": "SCALP_SYMBOLS فارغ"}

    try:
        closed = monitor_open_positions()
        open_positions = db.get_open_scalp_positions()
        opened = 0

        if can_open_new_trade(open_positions):
            snapshots = ta_engine.get_market_snapshot(SCALP_SYMBOLS, SCALP_CANDLE_INTERVAL)
            for snapshot in snapshots:
                db.save_ta_snapshot(snapshot)

            candidates = [
                item for item in snapshots
                if item.get("signal") in ("buy", "sell")
                and int(item.get("confidence") or 0) >= SCALP_MIN_CONFIDENCE
            ]
            candidates.sort(key=lambda item: int(item.get("confidence") or 0), reverse=True)

            for snapshot in candidates[:SCALP_MAX_NEW_TRADES_PER_MINUTE]:
                if open_position_from_snapshot(snapshot):
                    opened += 1
                    break

        return {
            "enabled": True,
            "opened": opened,
            "closed": closed,
            "symbols": SCALP_SYMBOLS,
            "interval": SCALP_CANDLE_INTERVAL,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    except ta_engine.BinanceRateLimited as exc:
        logger.warning(f"[Scalper] Binance rate limit/backoff: {exc}")
        return {"enabled": True, "opened": 0, "closed": 0, "backoff": True, "error": str(exc)}


if __name__ == "__main__":
    db.init_db()
    try:
        logger.info(run())
    finally:
        db.close_pool()

"""
trader.py — تنفيذ أوامر التداول على Binance (Spot أو Futures)
يمكن تشغيله منفرداً: python trader.py
"""

import os
import sys
import time

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException, BinanceOrderException
from dotenv import load_dotenv
from loguru import logger

import database as db

load_dotenv()

# ─── إعداد اللوجر ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

# ─── الثوابت ──────────────────────────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")
USE_FUTURES        = os.getenv("BINANCE_FUTURES", "false").lower() == "true"
TRADING_ENABLED    = os.getenv("TRADING_ENABLED", "false").lower() == "true"
MAX_RETRIES        = 3
RETRY_DELAY_SEC    = 2

STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "2.0"))
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "4.0"))


def get_binance_client() -> BinanceClient:
    """ينشئ عميل Binance ويتحقق من الاتصال."""
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        raise RuntimeError("BINANCE_API_KEY أو BINANCE_SECRET_KEY غير موجود في .env")
    client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    client.ping()
    return client


def get_symbol(coin: str) -> str:
    """يُنشئ رمز الزوج — مثال: BTC → BTCUSDT"""
    coin = coin.upper().replace("USDT", "")
    return f"{coin}USDT"


def get_symbol_info(client: BinanceClient, symbol: str) -> dict:
    """يجلب معلومات الرمز من Binance للتحقق من الحدود الدنيا."""
    info = client.get_symbol_info(symbol)
    if info is None:
        raise ValueError(f"الرمز {symbol} غير موجود على Binance")
    return info


def get_min_notional(symbol_info: dict) -> float:
    """يُعيد الحد الأدنى لقيمة الصفقة (USDT)."""
    for f in symbol_info.get("filters", []):
        if f["filterType"] == "MIN_NOTIONAL":
            return float(f.get("minNotional", 10.0))
        if f["filterType"] == "NOTIONAL":
            return float(f.get("minNotional", 10.0))
    return 10.0


def calculate_quantity(client: BinanceClient, symbol: str,
                       usdt_amount: float) -> float:
    """يحسب الكمية بناءً على السعر الحالي ومبلغ USDT."""
    ticker = client.get_symbol_ticker(symbol=symbol)
    price  = float(ticker["price"])
    qty    = usdt_amount / price

    # التقريب حسب stepSize
    info     = get_symbol_info(client, symbol)
    step_size = None
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step_size = float(f["stepSize"])
            break

    if step_size and step_size > 0:
        import math
        precision = int(round(-math.log10(step_size)))
        qty = round(qty - (qty % step_size), precision)

    return qty


# ─── تنفيذ الصفقة مع إعادة المحاولة ──────────────────────────────────────────

def execute_spot_order(client: BinanceClient, symbol: str,
                       side: str, quantity: float) -> dict:
    """ينفذ أمر Spot Market."""
    return client.create_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=quantity,
    )


def execute_futures_order(client: BinanceClient, symbol: str,
                          side: str, quantity: float) -> dict:
    """ينفذ أمر Futures Market."""
    return client.futures_create_order(
        symbol=symbol,
        side=side,
        type="MARKET",
        quantity=quantity,
    )


def place_order_with_retry(client: BinanceClient, symbol: str,
                           side: str, quantity: float) -> dict:
    """
    ينفذ الأمر مع إعادة المحاولة حتى MAX_RETRIES مرات.
    يرفع Exception إذا فشلت جميع المحاولات.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if USE_FUTURES:
                order = execute_futures_order(client, symbol, side, quantity)
            else:
                order = execute_spot_order(client, symbol, side, quantity)

            logger.info(
                f"[Trader] أمر نُفِّذ ✓ — {symbol} {side} | "
                f"المحاولة: {attempt} | order_id: {order.get('orderId')}"
            )
            return order

        except (BinanceAPIException, BinanceOrderException) as e:
            last_error = e
            logger.warning(
                f"[Trader] فشل تنفيذ الأمر (محاولة {attempt}/{MAX_RETRIES}): {e}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)

    raise RuntimeError(
        f"فشل تنفيذ الأمر {symbol} {side} بعد {MAX_RETRIES} محاولات: {last_error}"
    )


def place_stop_loss_take_profit(client: BinanceClient, symbol: str,
                                side: str, quantity: float,
                                entry_price: float) -> int | None:
    """
    يضع أوامر Stop Loss وTake Profit بعد تنفيذ الصفقة.
    يُعيد orderListId الخاص بـ OCO (Spot) أو None (Futures/فشل).
    """
    if USE_FUTURES:
        opposite_side = "SELL" if side == "BUY" else "BUY"
        sl_price = round(
            entry_price * (1 - STOP_LOSS_PCT / 100)
            if side == "BUY"
            else entry_price * (1 + STOP_LOSS_PCT / 100),
            2,
        )
        tp_price = round(
            entry_price * (1 + TAKE_PROFIT_PCT / 100)
            if side == "BUY"
            else entry_price * (1 - TAKE_PROFIT_PCT / 100),
            2,
        )
        try:
            client.futures_create_order(
                symbol=symbol, side=opposite_side,
                type="STOP_MARKET", stopPrice=sl_price,
                quantity=quantity, reduceOnly=True,
            )
            client.futures_create_order(
                symbol=symbol, side=opposite_side,
                type="TAKE_PROFIT_MARKET", stopPrice=tp_price,
                quantity=quantity, reduceOnly=True,
            )
            logger.info(f"[Trader] SL/TP Futures: SL={sl_price} TP={tp_price}")
        except Exception as e:
            logger.warning(f"[Trader] تحذير: فشل وضع SL/TP: {e}")
        return None
    else:
        sl_price = round(entry_price * (1 - STOP_LOSS_PCT   / 100), 2)
        tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT / 100), 2)
        sl_limit = round(sl_price * 0.99, 2)

        if side == "BUY":
            try:
                oco = client.order_oco_sell(
                    symbol=symbol,
                    quantity=quantity,
                    price=str(tp_price),
                    stopPrice=str(sl_price),
                    stopLimitPrice=str(sl_limit),
                    stopLimitTimeInForce="GTC",
                )
                oco_list_id = oco.get("orderListId")
                logger.info(
                    f"[Trader] SL/TP Spot OCO: SL={sl_price} TP={tp_price} "
                    f"| listId={oco_list_id}"
                )
                return oco_list_id
            except Exception as e:
                logger.warning(f"[Trader] تحذير: فشل وضع OCO: {e}")
        return None


def execute_decision(client: BinanceClient, cycle_id: int,
                     decision: dict) -> dict | None:
    """
    ينفذ قرار تداول واحد على Binance.
    يُعيد بيانات الصفقة المنفذة أو None عند الفشل.
    """
    coin   = decision["coin"]
    action = decision["action"]
    amount = float(decision["amount"])
    symbol = get_symbol(coin)
    side   = "BUY" if action == "buy" else "SELL"

    logger.info(f"[Trader] تنفيذ: {symbol} {side} | المبلغ: {amount:.2f} USDT")

    # تحقق من الحد الأدنى
    try:
        info         = get_symbol_info(client, symbol)
        min_notional = get_min_notional(info)
        if amount < min_notional:
            logger.warning(
                f"[Trader] {symbol}: المبلغ {amount:.2f} USDT "
                f"أقل من الحد الأدنى {min_notional:.2f} — تخطي"
            )
            db.save_trade(cycle_id, coin, action, amount, None, None, "failed")
            return None

        quantity = calculate_quantity(client, symbol, amount)
        if quantity <= 0:
            raise ValueError(f"الكمية المحسوبة صفر أو سالبة للرمز {symbol}")

    except Exception as e:
        logger.error(f"[Trader] خطأ في التحقق من {symbol}: {e}")
        db.save_trade(cycle_id, coin, action, amount, None, None, "failed")
        return None

    try:
        order = place_order_with_retry(client, symbol, side, quantity)
    except RuntimeError as e:
        logger.error(str(e))
        db.save_trade(cycle_id, coin, action, amount, None, None, "failed")
        return None

    order_id  = str(order.get("orderId", ""))
    fills     = order.get("fills", [])
    avg_price = (
        sum(float(f["price"]) * float(f["qty"]) for f in fills)
        / sum(float(f["qty"]) for f in fills)
        if fills else None
    )

    trade_id = db.save_trade(
        cycle_id=cycle_id,
        coin=coin,
        action=action,
        amount=amount,
        price=avg_price,
        order_id=order_id,
        status="filled",
    )

    if avg_price:
        oco_list_id = place_stop_loss_take_profit(client, symbol, side, quantity, avg_price)
        if oco_list_id:
            db.update_trade_oco(trade_id, oco_list_id)

    result = {
        "trade_id": trade_id,
        "coin":     coin,
        "action":   action,
        "amount":   amount,
        "price":    avg_price,
        "order_id": order_id,
        "status":   "filled",
    }
    logger.success(
        f"[Trader] صفقة منفذة ✓ — {coin} {action.upper()} | "
        f"السعر: {avg_price} | order_id: {order_id}"
    )
    return result


def check_open_trades(client: BinanceClient) -> None:
    """يفحص الصفقات المفتوحة ويحدّث PnL عند اكتمال OCO."""
    open_trades = db.get_open_trades()
    if not open_trades:
        return

    logger.info(f"[Trader] فحص {len(open_trades)} صفقة مفتوحة...")

    for trade in open_trades:
        trade_id     = int(trade["id"])
        coin         = trade["coin"]
        symbol       = get_symbol(coin)
        entry_price  = float(trade["price"] or 0)
        amount       = float(trade["amount"])
        oco_list_id  = int(trade["oco_order_list_id"])

        try:
            order_list   = client.get_order_list(orderListId=oco_list_id)
            list_status  = order_list.get("listOrderStatus", "")

            if list_status != "ALL_DONE":
                continue

            exit_price = None
            exit_type  = None

            for order_ref in order_list.get("orders", []):
                detail    = client.get_order(symbol=symbol, orderId=order_ref["orderId"])
                if detail.get("status") == "FILLED":
                    exec_qty   = float(detail.get("executedQty") or 1)
                    cum_quote  = float(detail.get("cummulativeQuoteQty") or 0)
                    exit_price = cum_quote / exec_qty if exec_qty > 0 else float(detail.get("price", 0))
                    exit_type  = "tp" if detail.get("type") == "LIMIT_MAKER" else "sl"
                    break

            if exit_price and entry_price > 0:
                pnl = round((exit_price - entry_price) / entry_price * amount, 4)
                db.update_trade_exit(trade_id, exit_price, pnl, exit_type or "unknown")
                logger.success(
                    f"[Trader] {coin} مُغلق ({exit_type}) | "
                    f"دخول: {entry_price:.4f} → خروج: {exit_price:.4f} | "
                    f"PnL: {pnl:+.2f} USDT"
                )
        except Exception as e:
            logger.warning(f"[Trader] تعذر فحص OCO للصفقة #{trade_id}: {e}")


def run(cycle_id: int) -> list[dict]:
    """
    نقطة الدخول المتزامنة — تُستدعى من scheduler.py.
    يُعيد قائمة الصفقات المنفذة أو يرفع Exception.
    """
    logger.info(f"[Trader] بدء تنفيذ الصفقات — الدورة #{cycle_id}")

    decisions = db.get_decisions_for_cycle(cycle_id)
    if not decisions:
        raise RuntimeError(f"لا توجد قرارات محفوظة للدورة #{cycle_id} — شغّل decision.py أولاً")

    active = [d for d in decisions if d["action"] in ("buy", "sell")]
    if not active:
        logger.info("[Trader] جميع القرارات hold — لا صفقات تُنفَّذ")
        return []

    if not TRADING_ENABLED:
        logger.warning(
            "[Trader] التداول الحقيقي معطل TRADING_ENABLED=false — "
            f"تخطي تنفيذ {len(active)} قرار نشط"
        )
        return []

    try:
        client = get_binance_client()
    except RuntimeError as e:
        raise RuntimeError(f"فشل الاتصال بـ Binance: {e}") from e

    mode = "Futures" if USE_FUTURES else "Spot"
    logger.info(f"[Trader] وضع التداول: {mode} | قرارات نشطة: {len(active)}")

    executed = []
    for decision in active:
        result = execute_decision(client, cycle_id, dict(decision))
        if result:
            executed.append(result)

    logger.success(
        f"[Trader] انتهى — "
        f"مُنفَّذ: {len(executed)} | "
        f"فاشل: {len(active) - len(executed)}"
    )
    return executed


# ─── تشغيل مباشر للاختبار ───────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل trader.py للاختبار المستقل")
    logger.info("═" * 60)

    db.init_db()
    cycle_id = db.create_cycle()

    try:
        trades = run(cycle_id)
        db.complete_cycle(cycle_id)
        logger.success(f"trader.py اجتاز الاختبار بنجاح ✓  | صفقات: {len(trades)}")
    except Exception as e:
        db.fail_cycle(cycle_id, str(e))
        logger.error(f"فشل الاختبار: {e}")
        sys.exit(1)
    finally:
        db.close_pool()

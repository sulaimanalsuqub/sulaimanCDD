"""
decision.py — اتخاذ قرارات التداول بناءً على تحليل Claude
يمكن تشغيله منفرداً: python decision.py
"""

import os
import sys

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException
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
MIN_CONFIDENCE     = int(os.getenv("MIN_CONFIDENCE", 60))
TRADING_ENABLED    = os.getenv("TRADING_ENABLED", "false").lower() == "true"
PAPER_CAPITAL_USDT = float(os.getenv("PAPER_CAPITAL_USDT", "1000"))

# نسبة رأس المال لكل صفقة بحسب مستوى الثقة
CAPITAL_TIERS = [
    (90, 0.15),   # ثقة 90-100 → 15% من رأس المال
    (80, 0.10),   # ثقة 80-89  → 10%
    (70, 0.07),   # ثقة 70-79  → 7%
    (60, 0.05),   # ثقة 60-69  → 5%
]

STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT",   "2.0"))   # 2%
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "4.0"))   # 4%


def get_binance_client() -> BinanceClient:
    """ينشئ عميل Binance ويتحقق من الاتصال."""
    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        raise RuntimeError("BINANCE_API_KEY أو BINANCE_SECRET_KEY غير موجود في .env")
    client = BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    # اختبار سريع للاتصال
    client.ping()
    return client


def get_available_capital(client: BinanceClient) -> float:
    """يجلب رصيد USDT المتاح من Binance Spot."""
    try:
        account = client.get_account()
        for balance in account["balances"]:
            if balance["asset"] == "USDT":
                free = float(balance["free"])
                logger.info(f"[Decision] رأس المال المتاح (USDT): {free:.2f}")
                return free
        logger.warning("[Decision] لا يوجد رصيد USDT في الحساب")
        return 0.0
    except BinanceAPIException as e:
        raise RuntimeError(f"فشل جلب رأس المال من Binance: {e}") from e


def capital_fraction(confidence: int) -> float:
    """يُعيد نسبة رأس المال المخصصة بناءً على مستوى الثقة."""
    for threshold, fraction in CAPITAL_TIERS:
        if confidence >= threshold:
            return fraction
    return 0.0


def decide_action(coin_data: dict, market_sentiment: str, confidence: int) -> str:
    """
    يقرر الإجراء (buy/sell/hold) لعملة واحدة.
    القواعد:
    - confidence < MIN_CONFIDENCE → hold دائماً
    - توجه bullish + coin sentiment bullish → buy
    - توجه bearish + coin sentiment bearish → sell
    - تناقض بين السوق والعملة → hold
    """
    if confidence < MIN_CONFIDENCE:
        return "hold"

    coin_sentiment = coin_data.get("sentiment", "neutral").lower()

    if market_sentiment == "bullish" and coin_sentiment == "bullish":
        return "buy"
    if market_sentiment == "bearish" and coin_sentiment == "bearish":
        return "sell"
    return "hold"


def make_decisions(cycle_id: int, analysis: dict, capital: float) -> list[dict]:
    """
    يأخذ نتيجة التحليل ويُنشئ قرارات لكل عملة.
    يُعيد قائمة بالقرارات المحفوظة.
    """
    decisions = []
    market_sentiment = analysis.get("sentiment", "neutral")
    confidence       = int(analysis.get("confidence", 0))
    coins            = analysis.get("coins", [])

    if not coins:
        logger.warning("[Decision] لا توجد عملات في التحليل — لا قرارات")
        return decisions

    fraction = capital_fraction(confidence)

    for coin_data in coins:
        symbol     = coin_data.get("symbol", "").upper()
        mentions   = int(coin_data.get("mentions", 0))

        if not symbol:
            continue

        action = decide_action(coin_data, market_sentiment, confidence)

        # حساب حجم الصفقة — يتناسب مع عدد الإشارات
        if action in ("buy", "sell") and capital > 0:
            # وزن العملة بحسب نسبة الإشارات
            total_mentions = sum(int(c.get("mentions", 1)) for c in coins) or 1
            weight = mentions / total_mentions
            amount = round(capital * fraction * weight, 2)
            amount = max(amount, 10.0)   # حد أدنى 10 USDT
        else:
            amount = 0.0

        # بناء سبب القرار
        reason = (
            f"السوق {market_sentiment} | "
            f"العملة {coin_data.get('sentiment', 'neutral')} | "
            f"الثقة {confidence}% | "
            f"الإشارات {mentions}"
        )

        # SL/TP معلوماتي فقط (يُنفَّذ في trader.py)
        sl_pct = STOP_LOSS_PCT
        tp_pct = TAKE_PROFIT_PCT
        reason += f" | SL -{sl_pct}% / TP +{tp_pct}%"

        decision_id = db.save_decision(
            cycle_id=cycle_id,
            coin=symbol,
            action=action,
            confidence=confidence,
            amount=amount,
            reason=reason,
        )

        decisions.append({
            "id":         decision_id,
            "coin":       symbol,
            "action":     action,
            "confidence": confidence,
            "amount":     amount,
            "reason":     reason,
        })

        logger.info(
            f"[Decision] {symbol}: {action.upper()} | "
            f"المبلغ: {amount:.2f} USDT | "
            f"الثقة: {confidence}%"
        )

    return decisions


def run(cycle_id: int) -> list[dict]:
    """
    نقطة الدخول المتزامنة — تُستدعى من scheduler.py.
    يُعيد قائمة القرارات أو يرفع Exception.
    """
    logger.info(f"[Decision] بدء اتخاذ القرارات — الدورة #{cycle_id}")

    analysis = db.get_latest_analysis()
    if not analysis:
        raise RuntimeError("لا يوجد تحليل محفوظ — شغّل analyzer.py أولاً")

    if TRADING_ENABLED:
        try:
            client  = get_binance_client()
            capital = get_available_capital(client)
        except RuntimeError as e:
            logger.error(f"[Decision] {e}")
            raise
    else:
        capital = PAPER_CAPITAL_USDT
        logger.warning(
            "[Decision] التداول الحقيقي معطل TRADING_ENABLED=false — "
            f"استخدام رأس مال تجريبي {capital:.2f} USDT"
        )

    # تحويل RealDictRow إلى dict عادي
    analysis_dict = dict(analysis)

    # coins محفوظ كـ JSONB — قد يأتي كـ str أو list
    import json as _json
    coins = analysis_dict.get("coins", [])
    if isinstance(coins, str):
        coins = _json.loads(coins)
    analysis_dict["coins"] = coins

    decisions = make_decisions(cycle_id, analysis_dict, capital)

    active = [d for d in decisions if d["action"] != "hold"]
    logger.success(
        f"[Decision] انتهى — "
        f"إجمالي: {len(decisions)} | "
        f"نشط (buy/sell): {len(active)} | "
        f"hold: {len(decisions) - len(active)}"
    )

    return decisions


# ─── تشغيل مباشر للاختبار ───────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل decision.py للاختبار المستقل")
    logger.info("═" * 60)

    db.init_db()
    cycle_id = db.create_cycle()

    try:
        decisions = run(cycle_id)
        db.complete_cycle(cycle_id)
        logger.success(f"decision.py اجتاز الاختبار بنجاح ✓  | قرارات: {len(decisions)}")
        for d in decisions:
            logger.info(f"  ← {d['coin']}: {d['action']} | {d['amount']:.2f} USDT")
    except Exception as e:
        db.fail_cycle(cycle_id, str(e))
        logger.error(f"فشل الاختبار: {e}")
        sys.exit(1)
    finally:
        db.close_pool()

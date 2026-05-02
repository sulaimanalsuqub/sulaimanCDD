"""
decision.py — اتخاذ قرارات التداول بناءً على تحليل Claude
يمكن تشغيله منفرداً: python decision.py
"""

import json
import os
import sys
from pathlib import Path

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv
from loguru import logger

import database as db

load_dotenv()

BASE_DIR = Path(__file__).parent
_COINS_CONFIG = BASE_DIR / "data" / "coins_config.json"


def get_selected_coins() -> set[str]:
    """يُعيد مجموعة رموز العملات المختارة للتداول. فارغة = كل العملات."""
    if not _COINS_CONFIG.exists():
        return set()
    try:
        data = json.loads(_COINS_CONFIG.read_text(encoding="utf-8"))
        return {str(s).upper().replace("USDT", "") for s in data.get("selected", [])}
    except Exception:
        return set()


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
MIN_TRADE_CONFIDENCE = int(os.getenv("MIN_TRADE_CONFIDENCE", MIN_CONFIDENCE))
MAX_TRADES_PER_CYCLE = max(1, int(os.getenv("MAX_TRADES_PER_CYCLE", 2)))
MIN_TRADE_USDT = float(os.getenv("MIN_TRADE_USDT", "10"))
MAX_TRADE_USDT = float(os.getenv("MAX_TRADE_USDT", "25"))
TRADE_CAPITAL_PCT = float(os.getenv("TRADE_CAPITAL_PCT", "10"))
ALLOW_SELL_ORDERS = os.getenv("ALLOW_SELL_ORDERS", "false").lower() == "true"

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


def make_recommendation_decisions(
    cycle_id: int,
    analysis: dict,
    capital: float,
) -> list[dict]:
    """ينشئ قرارات تنفيذ من recommendations القادمة من Claude."""
    recommendations = analysis.get("recommendations") or []
    if not recommendations:
        logger.warning("[Decision] لا توجد recommendations في التحليل — لا قرارات")
        return []

    spend_cap = max(0.0, capital * (TRADE_CAPITAL_PCT / 100))
    spend_cap = min(spend_cap, MAX_TRADE_USDT * MAX_TRADES_PER_CYCLE)
    if spend_cap < MIN_TRADE_USDT:
        logger.warning("[Decision] الرصيد المتاح لا يسمح بأي صفقة")
        return []

    # فلتر العملات المختارة
    selected_coins = get_selected_coins()
    if selected_coins:
        before = len(recommendations)
        recommendations = [
            r for r in recommendations
            if str(r.get("symbol") or "").upper().replace("USDT", "") in selected_coins
        ]
        if before != len(recommendations):
            logger.info(
                f"[Decision] فلتر العملات: {before} → {len(recommendations)} توصية "
                f"(مختارة: {len(selected_coins)} عملة)"
            )

    eligible = []
    passive = []
    for rec in recommendations:
        symbol = str(rec.get("symbol") or "").upper().replace("USDT", "")
        action = str(rec.get("action") or "watch").lower()
        confidence = int(rec.get("confidence") or analysis.get("confidence") or 0)
        if not symbol:
            continue

        if action == "sell" and not ALLOW_SELL_ORDERS:
            action = "hold"

        if action in ("buy", "sell") and confidence >= MIN_TRADE_CONFIDENCE:
            eligible.append({**rec, "symbol": symbol, "action": action, "confidence": confidence})
        else:
            passive.append({**rec, "symbol": symbol, "action": "hold", "confidence": confidence})

    eligible = sorted(eligible, key=lambda item: int(item.get("confidence") or 0), reverse=True)
    active = eligible[:MAX_TRADES_PER_CYCLE]
    per_trade = round(min(MAX_TRADE_USDT, spend_cap / len(active)), 2) if active else 0.0
    if active and per_trade < MIN_TRADE_USDT:
        logger.warning(
            f"[Decision] حجم الصفقة {per_trade:.2f} USDT أقل من الحد الأدنى "
            f"{MIN_TRADE_USDT:.2f} — تحويل القرارات إلى hold"
        )
        passive = active + passive
        active = []
        per_trade = 0.0

    decisions = []
    for rec in active + passive[: max(0, MAX_TRADES_PER_CYCLE - len(active))]:
        action = rec["action"] if rec in active else "hold"
        amount = per_trade if action in ("buy", "sell") else 0.0
        reason = rec.get("reason") or analysis.get("summary") or ""
        risk = rec.get("risk") or ""
        if risk:
            reason = f"{reason} | المخاطر: {risk}"

        decision_id = db.save_decision(
            cycle_id=cycle_id,
            coin=rec["symbol"],
            action=action,
            confidence=int(rec.get("confidence") or 0),
            amount=amount,
            reason=reason,
        )
        decision = {
            "id": decision_id,
            "coin": rec["symbol"],
            "action": action,
            "confidence": int(rec.get("confidence") or 0),
            "amount": amount,
            "reason": reason,
        }
        decisions.append(decision)
        logger.info(
            f"[Decision] {decision['coin']}: {action.upper()} | "
            f"المبلغ: {amount:.2f} USDT | الثقة: {decision['confidence']}%"
        )

    return decisions


def run(cycle_id: int) -> list[dict]:
    """
    نقطة الدخول المتزامنة — تُستدعى من scheduler.py.
    يُعيد قائمة القرارات أو يرفع Exception.
    """
    logger.info(f"[Decision] بدء اتخاذ القرارات — الدورة #{cycle_id}")

    analysis = db.get_analysis_for_cycle(cycle_id)
    if not analysis:
        raise RuntimeError(f"لا يوجد تحليل محفوظ للدورة #{cycle_id} — شغّل analyzer.py أولاً")

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

    try:
        cycle = db.get_cycle(cycle_id)
        raw_result = cycle.get("analysis_result") if cycle else None
        if raw_result:
            full_result = _json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            if isinstance(full_result, dict):
                analysis_dict.update(full_result)
    except Exception as exc:
        logger.warning(f"[Decision] تعذر قراءة analysis_result الكامل: {exc}")

    if analysis_dict.get("recommendations"):
        decisions = make_recommendation_decisions(cycle_id, analysis_dict, capital)
    else:
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

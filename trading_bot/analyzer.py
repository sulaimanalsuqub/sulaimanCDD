"""
analyzer.py — تحليل التغريدات عبر Claude API واستخراج توجه السوق
يمكن تشغيله منفرداً: python analyzer.py
"""

import json
import os
import sys

import anthropic
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
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL       = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
MAX_TWEETS_IN_PROMPT = int(os.getenv("MAX_TWEETS_IN_PROMPT", 200))  # حد أقصى لتجنب تجاوز context
INTERVAL_MINUTES   = int(os.getenv("INTERVAL_MINUTES", 5))

SYSTEM_PROMPT = """\
أنت محلل سوق كريبتو متخصص. حلل التغريدات واستخرج:
1. التوجه العام (bullish/bearish/neutral)
2. العملات المذكورة ونسبة الاهتمام
3. نسبة الثقة 0-100
4. سبب مختصر
أجب بـ JSON فقط بدون أي نص إضافي:
{
  "market_sentiment": "bullish",
  "coins": [{"symbol": "BTC", "sentiment": "bullish", "mentions": 15}],
  "confidence": 75,
  "reasoning": "..."
}"""


def build_prompt(tweets: list[dict]) -> str:
    """يبني نص المطالبة من قائمة التغريدات."""
    lines = []
    for i, t in enumerate(tweets[:MAX_TWEETS_IN_PROMPT], 1):
        account = t.get("account", "unknown")
        content = t.get("content", "").replace("\n", " ").strip()
        lines.append(f"{i}. @{account}: {content}")
    return "التغريدات:\n" + "\n".join(lines)


def parse_claude_response(raw: str) -> dict:
    """
    يحاول استخراج JSON من رد Claude.
    يُعيد dict عند النجاح أو يرفع ValueError.
    """
    raw = raw.strip()

    # إذا أحاط Claude الرد بـ markdown code block
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    data = json.loads(raw)

    # تحقق من الحقول الإلزامية
    required = {"market_sentiment", "coins", "confidence", "reasoning"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"حقول مفقودة في رد Claude: {missing}")

    sentiment = data["market_sentiment"].lower()
    if sentiment not in ("bullish", "bearish", "neutral"):
        logger.warning(f"توجه غير معروف '{sentiment}' — سيُعامل كـ neutral")
        sentiment = "neutral"
    data["market_sentiment"] = sentiment

    confidence = int(data["confidence"])
    if not (0 <= confidence <= 100):
        raise ValueError(f"قيمة confidence غير صالحة: {confidence}")
    data["confidence"] = confidence

    if not isinstance(data["coins"], list):
        raise ValueError("حقل coins يجب أن يكون قائمة")

    return data


def analyze_tweets(tweets: list[dict]) -> dict:
    """
    يُرسل التغريدات لـ Claude ويُعيد نتيجة التحليل كـ dict.
    يرفع Exception عند فشل الاتصال أو تحليل الرد.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY غير موجود في ملف .env")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = build_prompt(tweets)

    logger.info(f"[Analyzer] إرسال {min(len(tweets), MAX_TWEETS_IN_PROMPT)} تغريدة لـ Claude...")

    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"فشل الاتصال بـ Claude API: {e}") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"تجاوز حد معدل Claude API: {e}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"خطأ من Claude API [{e.status_code}]: {e.message}") from e

    raw = message.content[0].text
    logger.debug(f"[Analyzer] رد Claude الخام:\n{raw}")

    try:
        result = parse_claude_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise RuntimeError(f"فشل تحليل رد Claude: {e}\nالرد: {raw}") from e

    return result


def run(cycle_id: int) -> dict:
    """
    نقطة الدخول المتزامنة — تُستدعى من scheduler.py.
    يُعيد نتيجة التحليل أو يرفع Exception.
    """
    logger.info(f"[Analyzer] بدء التحليل — الدورة #{cycle_id}")

    tweets = db.get_recent_tweets(minutes=INTERVAL_MINUTES)
    if not tweets:
        raise RuntimeError(f"لا توجد تغريدات في آخر {INTERVAL_MINUTES} دقائق لتحليلها")

    logger.info(f"[Analyzer] تم جلب {len(tweets)} تغريدة من قاعدة البيانات")

    result = analyze_tweets(tweets)

    analysis_id = db.save_analysis(
        cycle_id=cycle_id,
        sentiment=result["market_sentiment"],
        coins=result["coins"],
        confidence=result["confidence"],
        reasoning=result["reasoning"],
    )

    logger.success(
        f"[Analyzer] انتهى — "
        f"التوجه: {result['market_sentiment']} | "
        f"الثقة: {result['confidence']}% | "
        f"عملات: {len(result['coins'])} | "
        f"ID التحليل: #{analysis_id}"
    )

    return result


# ─── تشغيل مباشر للاختبار ───────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل analyzer.py للاختبار المستقل")
    logger.info("═" * 60)

    db.init_db()
    cycle_id = db.create_cycle()

    try:
        result = run(cycle_id)
        db.complete_cycle(cycle_id)
        logger.success(f"analyzer.py اجتاز الاختبار بنجاح ✓")
        logger.info(f"النتيجة: {json.dumps(result, ensure_ascii=False, indent=2)}")
    except Exception as e:
        db.fail_cycle(cycle_id, str(e))
        logger.error(f"فشل الاختبار: {e}")
        sys.exit(1)
    finally:
        db.close_pool()

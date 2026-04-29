"""
analyzer.py — تحليل ملفات تغريدات الدورات عبر Claude.
يمكن تشغيله منفردا: python analyzer.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv
from loguru import logger

import database as db

load_dotenv()

BASE_DIR = Path(__file__).parent
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TWEETS_IN_PROMPT = int(os.getenv("MAX_TWEETS_IN_PROMPT", 200))

SYSTEM_PROMPT = """\
أنت محلل سوق كريبتو متخصص. حلل تغريدات حسابات X المرسلة لك.

المطلوب:
- تلخيص أهم الأخبار والإشارات من الحسابات.
- تحديد العملات أو الرموز المذكورة.
- تقييم المعنويات: bullish أو bearish أو neutral.
- استخراج الإشارات القوية فقط.
- ذكر الحسابات المؤثرة التي نشرت الإشارة.
- عدم اقتراح صفقة إذا البيانات غير كافية.
- التذكير أن التداول الحقيقي معطل حاليًا.

أجب بصيغة JSON فقط بدون Markdown:
{
  "market_sentiment": "neutral",
  "coins": [{"symbol": "BTC", "sentiment": "neutral", "mentions": 1}],
  "confidence": 55,
  "summary": "...",
  "strong_signals": [{"symbol": "BTC", "sentiment": "bullish", "accounts": ["..."], "reason": "..."}],
  "influential_accounts": ["..."],
  "reasoning": "...",
  "trading_note": "التداول الحقيقي معطل حاليًا."
}"""


def _json_default(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def load_tweets_file(path: str | Path) -> list[dict]:
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = BASE_DIR / file_path
    if not file_path.exists():
        raise RuntimeError(f"ملف التغريدات غير موجود: {file_path}")

    tweets = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tweets.append(json.loads(line))
    return tweets


def chunked(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_prompt(tweets: list[dict], *, batch_number: int | None = None) -> str:
    title = "التغريدات"
    if batch_number is not None:
        title += f" — الدفعة {batch_number}"

    lines = [title + ":"]
    for index, tweet in enumerate(tweets, 1):
        account = tweet.get("account", "unknown")
        text = (tweet.get("text") or tweet.get("content") or "").replace("\n", " ").strip()
        created_at = tweet.get("created_at") or tweet.get("tweet_created_at") or ""
        url = tweet.get("url") or tweet.get("tweet_url") or ""
        metrics = (
            f"likes={tweet.get('likes', 0)}, "
            f"retweets={tweet.get('retweets', 0)}, "
            f"replies={tweet.get('replies', 0)}, "
            f"views={tweet.get('views', 0)}"
        )
        lines.append(f"{index}. @{account} | {created_at} | {metrics} | {url}\n{text}")
    return "\n".join(lines)


def parse_claude_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    data = json.loads(raw)
    sentiment = str(data.get("market_sentiment", "neutral")).lower()
    if sentiment not in ("bullish", "bearish", "neutral"):
        sentiment = "neutral"

    confidence = int(data.get("confidence", 50))
    confidence = max(0, min(100, confidence))

    coins = data.get("coins") or []
    if not isinstance(coins, list):
        coins = []

    return {
        "market_sentiment": sentiment,
        "coins": coins,
        "confidence": confidence,
        "summary": data.get("summary") or "",
        "strong_signals": data.get("strong_signals") or [],
        "influential_accounts": data.get("influential_accounts") or [],
        "reasoning": data.get("reasoning") or "",
        "trading_note": data.get("trading_note") or "التداول الحقيقي معطل حاليًا.",
    }


def call_claude(prompt: str, max_tokens: int = 1600) -> dict:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY غير موجود في ملف .env")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"فشل الاتصال بـ Claude API: {e}") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError(f"تجاوز حد معدل Claude API: {e}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"خطأ من Claude API [{e.status_code}]: {e.message}") from e

    return parse_claude_response(message.content[0].text)


def combine_batch_results(results: list[dict]) -> dict:
    if len(results) == 1:
        return results[0]

    prompt = (
        "هذه تحليلات جزئية لدفعات تغريدات. ادمجها في تحليل نهائي واحد "
        "بنفس صيغة JSON المطلوبة، واستخرج فقط الإشارات القوية:\n"
        + json.dumps(results, ensure_ascii=False, default=_json_default)
    )
    return call_claude(prompt, max_tokens=1800)


def analyze_tweets(tweets: list[dict]) -> dict:
    if not tweets:
        raise RuntimeError("لا توجد تغريدات لتحليلها")

    batches = chunked(tweets, max(1, MAX_TWEETS_IN_PROMPT))
    logger.info(f"[Analyzer] تحليل {len(tweets)} تغريدة عبر {len(batches)} دفعة")

    partial_results = []
    for index, batch in enumerate(batches, 1):
        prompt = build_prompt(batch, batch_number=index)
        partial_results.append(call_claude(prompt))

    final = combine_batch_results(partial_results)
    final["tweets_analyzed"] = len(tweets)
    final["batches"] = len(batches)
    return final


def run(cycle_id: int, tweets_file_path: str | None = None) -> dict:
    logger.info(f"[Analyzer] بدء التحليل — الدورة #{cycle_id}")
    db.update_cycle(
        cycle_id,
        status="analyzing",
        analyzer_status="running",
        error_message="",
    )

    if not tweets_file_path:
        cycle = db.get_current_cycle()
        tweets_file_path = cycle.get("tweets_file_path") if cycle else None
    if not tweets_file_path:
        raise RuntimeError("لم يتم تحديد ملف التغريدات للتحليل")

    try:
        tweets = load_tweets_file(tweets_file_path)
        result = analyze_tweets(tweets)

        analysis_id = db.save_analysis(
            cycle_id=cycle_id,
            sentiment=result["market_sentiment"],
            coins=result["coins"],
            confidence=result["confidence"],
            reasoning=result.get("reasoning") or result.get("summary", ""),
        )
        result["analysis_id"] = analysis_id
        result["tweets_file_path"] = tweets_file_path
        db.save_cycle_analysis_result(cycle_id, result)
    except Exception as exc:
        db.update_cycle(
            cycle_id,
            status="analyzer_failed",
            analyzer_status="failed",
            error_message=str(exc),
            mark_finished=True,
        )
        raise

    logger.success(
        f"[Analyzer] انتهى — التوجه: {result['market_sentiment']} | "
        f"الثقة: {result['confidence']}% | ID التحليل: #{result['analysis_id']}"
    )
    return result


if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل analyzer.py للاختبار المستقل")
    logger.info("═" * 60)

    db.init_db()
    cycle = db.get_current_cycle()
    if not cycle:
        logger.error("لا توجد دورة حالية")
        sys.exit(1)

    try:
        result = run(cycle["cycle_id"], cycle.get("tweets_file_path"))
        logger.success("analyzer.py اجتاز الاختبار بنجاح")
        logger.info(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.error(f"فشل الاختبار: {exc}")
        sys.exit(1)
    finally:
        db.close_pool()

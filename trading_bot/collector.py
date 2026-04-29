"""
collector.py — جمع التغريدات من حسابات X عبر twitterapi.io
يمكن تشغيله منفرداً: python collector.py
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx
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
ACCOUNTS_FILE      = Path(__file__).parent / "accounts.txt"
TWEETS_PER_ACCOUNT = int(os.getenv("TWEETS_PER_ACCOUNT", 5))
MAX_CONCURRENT     = int(os.getenv("MAX_CONCURRENT", 20))
TWITTER_API_KEY    = os.getenv("TWITTER_API_KEY", "")

BASE_URL = "https://api.twitterapi.io"

HEADERS = {
    "X-API-Key": TWITTER_API_KEY,
    "Accept": "application/json",
}


def load_accounts() -> list[str]:
    """يقرأ قائمة الحسابات من accounts.txt — سطر لكل حساب."""
    if not ACCOUNTS_FILE.exists():
        logger.error(f"ملف الحسابات غير موجود: {ACCOUNTS_FILE}")
        return []

    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        handle = line.strip().lstrip("@")
        if handle and not handle.startswith("#"):
            accounts.append(handle)

    if not accounts:
        logger.warning("accounts.txt موجود لكنه فارغ!")
    else:
        logger.info(f"تم تحميل {len(accounts)} حساب من accounts.txt")

    return accounts


async def fetch_user_tweets(
    client: httpx.AsyncClient,
    account: str,
    limit: int,
) -> list[dict]:
    """
    يجلب آخر [limit] تغريدة من حساب واحد عبر twitterapi.io.
    يُعيد قائمة فارغة عند الفشل.
    """
    try:
        resp = await client.get(
            f"{BASE_URL}/twitter/user/last_tweets",
            params={"userName": account, "count": limit},
            timeout=15.0,
        )

        if resp.status_code == 401:
            logger.error("مفتاح TWITTER_API_KEY غير صحيح أو غير مفعّل")
            return []

        if resp.status_code == 404:
            logger.warning(f"الحساب غير موجود: @{account}")
            return []

        if resp.status_code == 429:
            logger.warning(f"تجاوزت الحد المسموح به مؤقتاً — @{account}")
            await asyncio.sleep(2)
            return []

        if resp.status_code != 200:
            logger.warning(f"@{account}: HTTP {resp.status_code}")
            return []

        data = resp.json()

        # twitterapi.io يُعيد: {"tweets": [...]} أو {"data": [...]}
        tweets = (
            data.get("tweets")
            or data.get("data")
            or []
        )

        logger.debug(f"@{account}: جُلبت {len(tweets)} تغريدة")
        return tweets

    except httpx.TimeoutException:
        logger.warning(f"@{account}: انتهت مهلة الطلب")
        return []
    except Exception as e:
        logger.error(f"خطأ في جلب تغريدات @{account}: {e}")
        return []


def extract_text(tweet: dict) -> str:
    """يستخرج نص التغريدة من أي صيغة ردّ."""
    return (
        tweet.get("text")
        or tweet.get("full_text")
        or tweet.get("rawContent")
        or tweet.get("content")
        or ""
    ).strip()


def extract_id(tweet: dict) -> str:
    """يستخرج معرّف التغريدة."""
    return str(
        tweet.get("id")
        or tweet.get("tweet_id")
        or tweet.get("id_str")
        or ""
    )


async def collect_all(cycle_id: int, accounts: list[str]) -> dict:
    """
    يجمع التغريدات من جميع الحسابات بشكل متوازٍ.
    يُعيد إحصائيات الجلسة.
    """
    if not TWITTER_API_KEY:
        raise RuntimeError(
            "TWITTER_API_KEY غير موجود في .env — "
            "احصل على مفتاح من https://twitterapi.io وأضفه"
        )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    stats = {"fetched": 0, "saved": 0, "skipped": 0, "errors": 0}

    async with httpx.AsyncClient(headers=HEADERS, http2=True) as client:

        async def bounded_fetch(account: str):
            async with semaphore:
                return account, await fetch_user_tweets(client, account, TWEETS_PER_ACCOUNT)

        tasks = [bounded_fetch(acc) for acc in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            stats["errors"] += 1
            logger.error(f"استثناء غير متوقع: {result}")
            continue

        account, tweets = result
        if not tweets:
            stats["errors"] += 1
            continue

        for tweet in tweets:
            stats["fetched"] += 1

            content  = extract_text(tweet)
            tweet_id = extract_id(tweet)

            if not content:
                stats["skipped"] += 1
                continue

            saved = db.save_tweet(
                cycle_id=cycle_id,
                account=account,
                tweet_id=tweet_id or f"{account}_{stats['fetched']}",
                content=content,
            )
            if saved:
                stats["saved"] += 1
            else:
                stats["skipped"] += 1  # مكررة أو خطأ

    return stats


def run(cycle_id: int) -> dict:
    """
    نقطة الدخول المتزامنة — تُستدعى من scheduler.py.
    """
    logger.info(f"[Collector] بدء جمع التغريدات — الدورة #{cycle_id}")

    accounts = load_accounts()
    if not accounts:
        raise RuntimeError("لا توجد حسابات في accounts.txt")

    stats = asyncio.run(collect_all(cycle_id, accounts))

    logger.info(
        f"[Collector] انتهى — "
        f"جُلب: {stats['fetched']} | "
        f"حُفظ: {stats['saved']} | "
        f"مكرر/فارغ: {stats['skipped']} | "
        f"أخطاء: {stats['errors']}"
    )

    if stats["saved"] == 0 and stats["fetched"] == 0:
        raise RuntimeError(
            "لم تُجلب أي تغريدات — تحقق من TWITTER_API_KEY في .env"
        )

    return stats


# ─── تشغيل مباشر للاختبار ─────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل collector.py للاختبار المستقل")
    logger.info("═" * 60)

    db.init_db()
    cycle_id = db.create_cycle()

    try:
        stats = run(cycle_id)
        db.complete_cycle(cycle_id)
        logger.success(f"collector.py اجتاز الاختبار ✓  |  {stats}")
    except Exception as e:
        db.fail_cycle(cycle_id, str(e))
        logger.error(f"فشل الاختبار: {e}")
        sys.exit(1)
    finally:
        db.close_pool()

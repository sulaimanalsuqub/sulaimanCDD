"""
collector.py — جمع التغريدات من حسابات X عبر twscrape
يمكن تشغيله منفرداً: python collector.py
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger
from twscrape import API, gather
from twscrape.logger import set_log_level

import database as db

load_dotenv()

# ─── إعداد اللوجر ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

# أسكت لوجر twscrape حتى لا يفيض bot.log
set_log_level("ERROR")

# ─── الثوابت ──────────────────────────────────────────────────────────────────
ACCOUNTS_FILE = Path(__file__).parent / "accounts.txt"
TWEETS_PER_ACCOUNT = int(os.getenv("TWEETS_PER_ACCOUNT", 5))   # آخر 5 تغريدات لكل حساب
MAX_CONCURRENT   = int(os.getenv("MAX_CONCURRENT", 10))         # طلبات متزامنة


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


async def fetch_user_tweets(api: API, account: str, limit: int) -> list:
    """يجلب آخر [limit] تغريدة من حساب واحد. يُعيد قائمة فارغة عند الفشل."""
    try:
        user = await api.user_by_login(account)
        if user is None:
            logger.warning(f"الحساب غير موجود أو محظور: @{account}")
            return []

        tweets = await gather(api.user_tweets(user.id, limit=limit))
        logger.debug(f"@{account}: جُلبت {len(tweets)} تغريدة")
        return tweets

    except Exception as e:
        logger.error(f"خطأ في جلب تغريدات @{account}: {e}")
        return []


async def collect_all(cycle_id: int, accounts: list[str]) -> dict:
    """
    يجمع التغريدات من جميع الحسابات بشكل متوازٍ (semaphore للتحكم بالضغط).
    يُعيد إحصائيات الجلسة.
    """
    api = API()

    # تحقق من وجود حسابات twscrape مُعدّة
    twscrape_accounts = await api.pool.get_all()
    if not twscrape_accounts:
        logger.warning(
            "لا توجد حسابات X في twscrape pool. "
            "شغّل: twscrape add_accounts accounts_auth.txt --cookies "
            "ثم: twscrape login_all"
        )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    stats = {"fetched": 0, "saved": 0, "skipped": 0, "errors": 0}

    async def bounded_fetch(account: str):
        async with semaphore:
            return account, await fetch_user_tweets(api, account, TWEETS_PER_ACCOUNT)

    tasks = [bounded_fetch(acc) for acc in accounts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            stats["errors"] += 1
            logger.error(f"استثناء غير متوقع أثناء الجلب: {result}")
            continue

        account, tweets = result
        if not tweets:
            stats["errors"] += 1
            continue

        for tweet in tweets:
            stats["fetched"] += 1
            tweet_id  = str(tweet.id)
            content   = tweet.rawContent or tweet.content or ""

            if not content.strip():
                stats["skipped"] += 1
                continue

            saved = db.save_tweet(
                cycle_id=cycle_id,
                account=account,
                tweet_id=tweet_id,
                content=content,
            )
            if saved:
                stats["saved"] += 1
            else:
                stats["skipped"] += 1   # تغريدة مكررة أو خطأ

    return stats


def run(cycle_id: int) -> dict:
    """
    نقطة الدخول المتزامنة — تُستدعى من scheduler.py.
    يُعيد dict بالإحصائيات أو يرفع Exception عند الفشل الكلي.
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
        raise RuntimeError("لم تُجلب أي تغريدات — تحقق من حسابات twscrape")

    return stats


# ─── تشغيل مباشر للاختبار ───────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل collector.py للاختبار المستقل")
    logger.info("═" * 60)

    db.init_db()
    cycle_id = db.create_cycle()

    try:
        stats = run(cycle_id)
        db.complete_cycle(cycle_id)
        logger.success(f"collector.py اجتاز الاختبار بنجاح ✓  |  {stats}")
    except Exception as e:
        db.fail_cycle(cycle_id, str(e))
        logger.error(f"فشل الاختبار: {e}")
        sys.exit(1)
    finally:
        db.close_pool()

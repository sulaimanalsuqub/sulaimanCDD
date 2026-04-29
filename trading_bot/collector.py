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

# ─── الثوابت ──────────────────────────────────────────────────────────────────
ACCOUNTS_FILE      = Path(__file__).parent / "accounts.txt"
TWEETS_PER_ACCOUNT = int(os.getenv("TWEETS_PER_ACCOUNT", 5))
MAX_CONCURRENT     = int(os.getenv("MAX_CONCURRENT", 5))   # محافظ لتجنّب 429
TWITTER_API_KEY    = os.getenv("TWITTER_API_KEY", "")

BASE_URL = "https://api.twitterapi.io"
HEADERS  = {"X-API-Key": TWITTER_API_KEY, "Accept": "application/json"}


class CollectorConfigError(RuntimeError):
    """خطأ يمنع الجمع من الاستمرار ويحتاج إجراء خارجي."""


def load_accounts() -> list[str]:
    if not ACCOUNTS_FILE.exists():
        logger.error(f"ملف الحسابات غير موجود: {ACCOUNTS_FILE}")
        return []
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        handle = line.strip().lstrip("@")
        if handle and not handle.startswith("#") and handle.lower() != "handle":
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
    retries: int = 2,
) -> list[dict]:
    """يجلب آخر [limit] تغريدة من حساب واحد. يعيد محاولة عند 429."""
    for attempt in range(retries + 1):
        try:
            resp = await client.get(
                f"{BASE_URL}/twitter/user/last_tweets",
                params={"userName": account, "count": limit},
                timeout=20.0,
            )

            if resp.status_code == 200:
                data   = resp.json()
                tweets = data.get("tweets") or data.get("data") or []
                logger.debug(f"@{account}: جُلبت {len(tweets)} تغريدة")
                return tweets

            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                logger.warning(f"@{account}: تجاوز الحد — انتظار {wait}ث")
                await asyncio.sleep(wait)
                continue

            if resp.status_code == 401:
                raise CollectorConfigError("TWITTER_API_KEY غير صحيح أو غير مصرح")

            if resp.status_code == 402:
                try:
                    message = resp.json().get("message", "")
                except Exception:
                    message = resp.text[:120]
                raise CollectorConfigError(
                    f"رصيد twitterapi.io غير كاف لجمع التغريدات: {message}"
                )

            if resp.status_code == 404:
                logger.debug(f"@{account}: غير موجود (404)")
                return []

            logger.warning(f"@{account}: HTTP {resp.status_code}")
            return []

        except httpx.TimeoutException:
            logger.warning(f"@{account}: انتهت المهلة")
            return []
        except Exception as e:
            logger.error(f"خطأ في @{account}: {e}")
            return []

    return []


def extract_text(tweet: dict) -> str:
    return (
        tweet.get("text") or tweet.get("full_text")
        or tweet.get("rawContent") or tweet.get("content") or ""
    ).strip()


def extract_id(tweet: dict) -> str:
    return str(
        tweet.get("id") or tweet.get("tweet_id") or tweet.get("id_str") or ""
    )


async def collect_all(cycle_id: int, accounts: list[str]) -> dict:
    if not TWITTER_API_KEY:
        raise RuntimeError(
            "TWITTER_API_KEY غير موجود في .env"
        )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    stats = {"fetched": 0, "saved": 0, "skipped": 0, "errors": 0}

    async with httpx.AsyncClient(headers=HEADERS, http2=True) as client:

        async def bounded_fetch(account: str):
            async with semaphore:
                return account, await fetch_user_tweets(client, account, TWEETS_PER_ACCOUNT)

        results = await asyncio.gather(
            *[bounded_fetch(acc) for acc in accounts],
            return_exceptions=True,
        )

    for result in results:
        if isinstance(result, Exception):
            if isinstance(result, CollectorConfigError):
                raise result
            stats["errors"] += 1
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
                stats["skipped"] += 1

    return stats


def run(cycle_id: int) -> dict:
    logger.info(f"[Collector] بدء جمع التغريدات — الدورة #{cycle_id}")

    accounts = load_accounts()
    if not accounts:
        raise RuntimeError("لا توجد حسابات في accounts.txt")

    stats = asyncio.run(collect_all(cycle_id, accounts))

    logger.info(
        f"[Collector] انتهى — "
        f"جُلب: {stats['fetched']} | "
        f"حُفظ: {stats['saved']} | "
        f"مكرر: {stats['skipped']} | "
        f"أخطاء: {stats['errors']}"
    )

    if stats["saved"] == 0 and stats["fetched"] == 0:
        raise RuntimeError(
            "لم تُجلب أي تغريدات — تحقق من رصيد twitterapi.io وصلاحية TWITTER_API_KEY"
        )

    return stats


# ─── تشغيل مباشر للاختبار ─────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    logger.add("bot.log", rotation="10 MB", retention="7 days",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

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

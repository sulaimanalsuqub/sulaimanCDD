"""
collector.py — جمع تغريدات X من twitterapi.io أو Twikit.
يمكن تشغيله منفردا: python collector.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from loguru import logger

import database as db

load_dotenv()

BASE_DIR = Path(__file__).parent
ACCOUNTS_FILE = BASE_DIR / "accounts.txt"
DATA_DIR = BASE_DIR / "data" / "tweets"
LAST_SEEN_FILE = DATA_DIR / "last_seen_tweets.json"

TWITTER_PROVIDER = os.getenv("TWITTER_PROVIDER", "twitterapi").strip().lower()
TWEETS_PER_ACCOUNT = int(os.getenv("TWEETS_PER_ACCOUNT", 5))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", 5))
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY", "")
TWIKIT_USERNAME = os.getenv("TWIKIT_USERNAME", "")
TWIKIT_EMAIL = os.getenv("TWIKIT_EMAIL", "")
TWIKIT_PASSWORD = os.getenv("TWIKIT_PASSWORD", "")
TWIKIT_COOKIES_FILE = os.getenv("TWIKIT_COOKIES_FILE", "x_cookies.json")

BASE_URL = "https://api.twitterapi.io"
HEADERS = {"X-API-Key": TWITTER_API_KEY, "Accept": "application/json"}


class CollectorConfigError(RuntimeError):
    """خطأ يمنع الجمع من مزود محدد ويحتاج إجراء خارجي."""


class NoNewTweets(RuntimeError):
    """لا توجد تغريدات جديدة قابلة للحفظ في هذه الدورة."""


def load_accounts(limit: int | None = None) -> list[str]:
    if not ACCOUNTS_FILE.exists():
        logger.error(f"ملف الحسابات غير موجود: {ACCOUNTS_FILE}")
        return []

    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        handle = line.strip().lstrip("@")
        if handle and not handle.startswith("#") and handle.lower() != "handle":
            accounts.append(handle)

    if limit:
        accounts = accounts[:limit]

    if not accounts:
        logger.warning("accounts.txt موجود لكنه فارغ")
    else:
        logger.info(f"تم تحميل {len(accounts)} حساب من accounts.txt")
    return accounts


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _tweet_url(account: str, tweet_id: str) -> str:
    return f"https://x.com/{account}/status/{tweet_id}"


def normalize_twitterapi_tweet(account: str, tweet: dict) -> dict | None:
    tweet_id = str(tweet.get("id") or tweet.get("tweet_id") or tweet.get("id_str") or "")
    text = (
        tweet.get("text")
        or tweet.get("full_text")
        or tweet.get("rawContent")
        or tweet.get("content")
        or ""
    ).strip()
    if not tweet_id or not text:
        return None

    created_at = (
        tweet.get("createdAt")
        or tweet.get("created_at")
        or tweet.get("date")
        or tweet.get("time")
    )
    return {
        "account": account,
        "tweet_id": tweet_id,
        "created_at": _iso_datetime(created_at),
        "text": text,
        "url": tweet.get("url") or _tweet_url(account, tweet_id),
        "likes": _int(tweet.get("likeCount") or tweet.get("likes")),
        "retweets": _int(tweet.get("retweetCount") or tweet.get("retweets")),
        "replies": _int(tweet.get("replyCount") or tweet.get("replies")),
        "views": _int(tweet.get("viewCount") or tweet.get("views")),
    }


def normalize_twikit_tweet(account: str, tweet: Any) -> dict | None:
    tweet_id = str(getattr(tweet, "id", "") or "")
    text = (getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or "").strip()
    if not tweet_id or not text:
        return None

    created_at = getattr(tweet, "created_at_datetime", None) or getattr(tweet, "created_at", None)
    return {
        "account": account,
        "tweet_id": tweet_id,
        "created_at": _iso_datetime(created_at),
        "text": text,
        "url": _tweet_url(account, tweet_id),
        "likes": _int(getattr(tweet, "favorite_count", 0)),
        "retweets": _int(getattr(tweet, "retweet_count", 0)),
        "replies": _int(getattr(tweet, "reply_count", 0)),
        "views": _int(getattr(tweet, "view_count", 0)),
    }


async def fetch_twitterapi_account(
    client: httpx.AsyncClient,
    account: str,
    limit: int,
    retries: int = 2,
) -> list[dict]:
    for attempt in range(retries + 1):
        resp = await client.get(
            f"{BASE_URL}/twitter/user/last_tweets",
            params={"userName": account, "count": limit},
            timeout=20.0,
        )

        if resp.status_code == 200:
            data = resp.json()
            tweets = data.get("tweets") or data.get("data") or []
            return [
                item for item in (
                    normalize_twitterapi_tweet(account, tweet)
                    for tweet in tweets
                )
                if item
            ]

        if resp.status_code == 429:
            if attempt < retries:
                wait = 5 * (attempt + 1)
                logger.warning(f"@{account}: twitterapi rate limit — انتظار {wait}ث")
                await asyncio.sleep(wait)
                continue
            raise CollectorConfigError("twitterapi.io rate limit")

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
            return []

        raise CollectorConfigError(f"twitterapi.io HTTP {resp.status_code}: {resp.text[:120]}")

    return []


async def collect_with_twitterapi(accounts: list[str]) -> list[dict]:
    if not TWITTER_API_KEY:
        raise CollectorConfigError("TWITTER_API_KEY غير موجود في .env")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    collected: list[dict] = []
    async with httpx.AsyncClient(headers=HEADERS, http2=True) as client:
        async def bounded_fetch(account: str) -> list[dict]:
            async with semaphore:
                return await fetch_twitterapi_account(client, account, TWEETS_PER_ACCOUNT)

        results = await asyncio.gather(
            *(bounded_fetch(account) for account in accounts),
            return_exceptions=True,
        )

    config_errors = []
    for account, result in zip(accounts, results):
        if isinstance(result, CollectorConfigError):
            config_errors.append(str(result))
            continue
        if isinstance(result, Exception):
            logger.warning(f"@{account}: فشل twitterapi.io: {result}")
            continue
        collected.extend(result)

    if not collected and config_errors:
        raise CollectorConfigError("; ".join(sorted(set(config_errors))))
    return collected


def twikit_available() -> bool:
    return bool(TWIKIT_USERNAME and TWIKIT_PASSWORD)


async def collect_with_twikit(accounts: list[str]) -> list[dict]:
    if not twikit_available():
        raise CollectorConfigError("بيانات Twikit غير مكتملة في .env")

    try:
        from twikit import Client
    except ImportError as exc:
        raise CollectorConfigError("مكتبة twikit غير مثبتة") from exc

    cookies_path = Path(TWIKIT_COOKIES_FILE)
    if not cookies_path.is_absolute():
        cookies_path = BASE_DIR / cookies_path

    client = Client(language="en-US")
    await client.login(
        auth_info_1=TWIKIT_USERNAME,
        auth_info_2=TWIKIT_EMAIL or None,
        password=TWIKIT_PASSWORD,
        cookies_file=str(cookies_path),
    )

    collected: list[dict] = []
    for account in accounts:
        try:
            user = await client.get_user_by_screen_name(account)
            tweets = await user.get_tweets("Tweets", count=TWEETS_PER_ACCOUNT)
            items = [
                item for item in (
                    normalize_twikit_tweet(account, tweet)
                    for tweet in tweets
                )
                if item
            ]
            collected.extend(items)
            logger.debug(f"@{account}: Twikit جلب {len(items)} تغريدة")
            await asyncio.sleep(1.0)
        except Exception as exc:
            logger.warning(f"@{account}: فشل Twikit: {exc}")
    return collected


def read_last_seen() -> dict[str, str]:
    if not LAST_SEEN_FILE.exists():
        return {}
    try:
        return json.loads(LAST_SEEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("تعذر قراءة last_seen_tweets.json، سيتم تجاهله")
        return {}


def write_last_seen(last_seen: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_FILE.write_text(
        json.dumps(last_seen, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(cycle_id: int, tweets: list[dict]) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"cycle_{cycle_id}_tweets.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for tweet in tweets:
            payload = {"cycle_id": cycle_id, **tweet}
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


async def collect_from_provider(provider: str, accounts: list[str]) -> tuple[str, list[dict]]:
    provider = provider.lower()
    if provider == "twikit":
        return "twikit", await collect_with_twikit(accounts)
    if provider in ("twitterapi", "twitterapi.io"):
        return "twitterapi", await collect_with_twitterapi(accounts)
    raise CollectorConfigError(f"TWITTER_PROVIDER غير معروف: {provider}")


async def collect_all(accounts: list[str]) -> tuple[str, list[dict]]:
    preferred = TWITTER_PROVIDER if TWITTER_PROVIDER else "twitterapi"
    fallback = "twikit" if preferred != "twikit" else "twitterapi"

    try:
        return await collect_from_provider(preferred, accounts)
    except CollectorConfigError as exc:
        if preferred in ("twitterapi", "twitterapi.io") and twikit_available():
            logger.warning(f"{preferred} فشل ({exc}) — استخدام Twikit كـ fallback")
            return await collect_from_provider(fallback, accounts)
        raise


def persist_tweets(cycle_id: int, tweets: list[dict]) -> dict:
    seen = read_last_seen()
    saved_tweets = []
    stats = {"fetched": len(tweets), "saved": 0, "skipped": 0, "errors": 0}

    for tweet in tweets:
        account = tweet["account"]
        tweet_id = tweet["tweet_id"]

        if seen.get(account) == tweet_id:
            stats["skipped"] += 1
            continue

        saved = db.save_tweet(
            cycle_id=cycle_id,
            account=account,
            tweet_id=tweet_id,
            content=tweet["text"],
            created_at=tweet.get("created_at"),
            url=tweet.get("url"),
            likes=tweet.get("likes", 0),
            retweets=tweet.get("retweets", 0),
            replies=tweet.get("replies", 0),
            views=tweet.get("views", 0),
        )
        if saved:
            saved_tweets.append(tweet)
            stats["saved"] += 1
            seen[account] = tweet_id
        else:
            stats["skipped"] += 1

    write_last_seen(seen)
    tweets_file = write_jsonl(cycle_id, saved_tweets)
    stats["tweets_file_path"] = str(tweets_file.relative_to(BASE_DIR))
    return stats


def run(cycle_id: int, account_limit: int | None = None) -> dict:
    logger.info(f"[Collector] بدء جمع التغريدات — الدورة #{cycle_id}")
    db.update_cycle(
        cycle_id,
        status="collecting",
        collector_status="running",
        error_message="",
    )

    accounts = load_accounts(limit=account_limit)
    if not accounts:
        raise RuntimeError("لا توجد حسابات في accounts.txt")

    try:
        provider, tweets = asyncio.run(collect_all(accounts))
        stats = persist_tweets(cycle_id, tweets)
    except Exception as exc:
        db.update_cycle(
            cycle_id,
            status="collector_failed",
            collector_status="failed",
            error_message=str(exc),
            mark_finished=True,
        )
        raise

    if stats["saved"] == 0:
        db.update_cycle(
            cycle_id,
            status="no_new_tweets",
            collector_status="completed",
            tweets_count=0,
            tweets_file_path=stats["tweets_file_path"],
            mark_collected=True,
            mark_finished=True,
        )
        raise NoNewTweets("لا توجد تغريدات جديدة")

    db.update_cycle(
        cycle_id,
        status="collected",
        collector_status="completed",
        tweets_count=stats["saved"],
        tweets_file_path=stats["tweets_file_path"],
        mark_collected=True,
    )

    logger.info(
        f"[Collector] انتهى عبر {provider} — "
        f"جلب: {stats['fetched']} | حفظ: {stats['saved']} | "
        f"مكرر: {stats['skipped']} | ملف: {stats['tweets_file_path']}"
    )
    return stats


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    logger.add("bot.log", rotation="10 MB", retention="7 days",
               format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

    db.init_db()
    cycle_id = db.create_cycle()

    try:
        stats = run(cycle_id)
        logger.success(f"collector.py اجتاز الاختبار | {stats}")
    except NoNewTweets as exc:
        logger.warning(str(exc))
    except Exception as exc:
        logger.error(f"فشل الاختبار: {exc}")
        sys.exit(1)
    finally:
        db.close_pool()

"""
collector.py — جمع تغريدات X عبر SocialData.tools.
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

SOCIALDATA_API_KEY = os.getenv("SOCIALDATA_API_KEY", "")
TWEETS_PER_ACCOUNT = int(os.getenv("TWEETS_PER_ACCOUNT", 5))
SOCIALDATA_REQUEST_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("SOCIALDATA_REQUEST_DELAY_SECONDS", 1.2)),
)

SOCIALDATA_BASE_URL = "https://api.socialdata.tools"
HEADERS = {
    "Authorization": f"Bearer {SOCIALDATA_API_KEY}",
    "Accept": "application/json",
}


class CollectorConfigError(RuntimeError):
    """خطأ يمنع الجمع من الاستمرار ويحتاج إجراء خارجي."""


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
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
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


def _retry_after_seconds(resp: httpx.Response, fallback: float) -> float:
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), fallback)
        except ValueError:
            pass
    return fallback


def normalize_socialdata_tweet(account: str, tweet: dict) -> dict | None:
    tweet_id = str(tweet.get("id_str") or tweet.get("id") or "")
    text = (tweet.get("full_text") or tweet.get("text") or "").strip()
    if not tweet_id or not text:
        return None

    user = tweet.get("user") or {}
    screen_name = user.get("screen_name") or account
    return {
        "account": screen_name,
        "tweet_id": tweet_id,
        "created_at": _iso_datetime(tweet.get("tweet_created_at") or tweet.get("created_at")),
        "text": text,
        "url": _tweet_url(screen_name, tweet_id),
        "likes": _int(tweet.get("favorite_count")),
        "retweets": _int(tweet.get("retweet_count")),
        "replies": _int(tweet.get("reply_count")),
        "views": _int(tweet.get("views_count")),
    }


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


async def fetch_socialdata_account(
    client: httpx.AsyncClient,
    account: str,
    limit: int,
    retries: int = 2,
) -> list[dict]:
    query = f"from:{account} -filter:replies"
    params = {"query": query, "type": "Latest"}

    for attempt in range(retries + 1):
        resp = await client.get(
            f"{SOCIALDATA_BASE_URL}/twitter/search",
            params=params,
            timeout=30.0,
        )

        if resp.status_code == 200:
            data = resp.json()
            tweets = data.get("tweets") or []
            normalized = [
                item for item in (
                    normalize_socialdata_tweet(account, tweet)
                    for tweet in tweets[:limit]
                )
                if item
            ]
            logger.debug(f"@{account}: SocialData جلب {len(normalized)} تغريدة")
            return normalized

        if resp.status_code == 402:
            raise CollectorConfigError("رصيد SocialData.tools غير كاف لجمع التغريدات")

        if resp.status_code == 401:
            raise CollectorConfigError("SOCIALDATA_API_KEY غير صحيح أو غير مصرح")

        if resp.status_code == 403:
            raise CollectorConfigError("حساب SocialData لا يملك صلاحية الوصول لهذا endpoint")

        if resp.status_code == 404:
            logger.debug(f"@{account}: غير موجود في SocialData")
            return []

        if resp.status_code == 422:
            raise CollectorConfigError(f"طلب SocialData غير صالح: {resp.text[:160]}")

        if resp.status_code in (429, 500):
            if attempt < retries:
                wait = _retry_after_seconds(resp, 30.0 * (attempt + 1))
                logger.warning(f"@{account}: SocialData HTTP {resp.status_code} — انتظار {wait}ث")
                await asyncio.sleep(wait)
                continue

        raise CollectorConfigError(
            f"SocialData HTTP {resp.status_code}: {resp.text[:160]}"
        )

    return []


async def collect_all(accounts: list[str]) -> tuple[str, list[dict]]:
    if not SOCIALDATA_API_KEY:
        raise CollectorConfigError("SOCIALDATA_API_KEY غير موجود في .env")

    collected: list[dict] = []
    errors: list[str] = []

    async with httpx.AsyncClient(headers=HEADERS, http2=True) as client:
        for index, account in enumerate(accounts):
            if index > 0 and SOCIALDATA_REQUEST_DELAY_SECONDS:
                await asyncio.sleep(SOCIALDATA_REQUEST_DELAY_SECONDS)

            try:
                tweets = await fetch_socialdata_account(
                    client,
                    account,
                    TWEETS_PER_ACCOUNT,
                )
            except CollectorConfigError:
                raise
            except Exception as exc:
                logger.warning(f"@{account}: فشل SocialData: {exc}")
                errors.append(str(exc))
                continue

            collected.extend(tweets)

    if not collected and errors:
        raise CollectorConfigError("; ".join(sorted(set(errors))))

    return "socialdata", collected


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

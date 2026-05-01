"""
scheduler.py — المجدول الرئيسي: جمع تغريدات X ثم تحليلها عبر Claude.
تشغيل: python scheduler.py
"""

import os
import sys
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from dotenv import load_dotenv
from loguru import logger

import database as db
import collector
import analyzer
import decision
import trader

load_dotenv()

# ─── إعداد اللوجر ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

# ─── الثوابت ──────────────────────────────────────────────────────────────────
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", 60))


def run_cycle() -> None:
    """
    ينفذ دورة كاملة بالترتيب:
    1. Collector  → جمع التغريدات وحفظ JSONL
    2. Analyzer   → تحليل Claude API
    3. Decision   → تحويل التوصيات إلى قرارات
    4. Trader     → تنفيذ القرارات إذا TRADING_ENABLED=true

    إذا فشلت أي مرحلة → يُسجَّل الخطأ ويُكمل للدورة التالية.
    التنفيذ الحقيقي لا يحدث إلا إذا TRADING_ENABLED=true.
    """
    cycle_id = db.create_cycle()
    logger.info("=" * 60)
    logger.info(f"بدء الدورة #{cycle_id}")
    logger.info("=" * 60)

    # ── المرحلة 1: جمع التغريدات ──────────────────────────────────────────────
    try:
        stats = collector.run(cycle_id)
        logger.info(
            f"✓ المرحلة 1 (Collector) — محفوظ: {stats['saved']} | "
            f"ملف: {stats.get('tweets_file_path')}"
        )
    except collector.NoNewTweets as e:
        logger.warning(f"لا توجد تغريدات جديدة في الدورة #{cycle_id}: {e}")
        return
    except Exception as e:
        logger.error(f"✗ المرحلة 1 (Collector) فشلت: {e}")
        db.update_cycle(
            cycle_id,
            status="collector_failed",
            collector_status="failed",
            error_message=f"Collector: {e}",
            mark_finished=True,
        )
        return

    # ── المرحلة 2: التحليل ────────────────────────────────────────────────────
    try:
        result = analyzer.run(cycle_id, stats.get("tweets_file_path"))
        logger.info(
            f"✓ المرحلة 2 (Analyzer) — "
            f"{result.get('market_sentiment')} | "
            f"ثقة: {result.get('confidence')}%"
        )
    except Exception as e:
        logger.error(f"✗ المرحلة 2 (Analyzer) فشلت: {e}")
        db.update_cycle(
            cycle_id,
            status="analyzer_failed",
            analyzer_status="failed",
            error_message=f"Analyzer: {e}",
            mark_finished=True,
        )
        return

    # ── المرحلة 3: القرارات ──────────────────────────────────────────────────
    try:
        decisions = decision.run(cycle_id)
        logger.info(f"✓ المرحلة 3 (Decision) — قرارات: {len(decisions)}")
    except Exception as e:
        logger.error(f"✗ المرحلة 3 (Decision) فشلت: {e}")
        db.update_cycle(
            cycle_id,
            status="decision_failed",
            error_message=f"Decision: {e}",
            mark_finished=True,
        )
        return

    # ── المرحلة 4: التنفيذ ───────────────────────────────────────────────────
    try:
        trades = trader.run(cycle_id)
        logger.info(f"✓ المرحلة 4 (Trader) — صفقات منفذة: {len(trades)}")
        db.complete_cycle(cycle_id)
    except Exception as e:
        logger.error(f"✗ المرحلة 4 (Trader) فشلت: {e}")
        db.update_cycle(
            cycle_id,
            status="trader_failed",
            error_message=f"Trader: {e}",
            mark_finished=True,
        )
        return

    logger.success(f"اكتملت دورة التداول #{cycle_id} بنجاح ✓")
    logger.info("=" * 60)


def on_job_event(event) -> None:
    """مستمع أحداث APScheduler للتسجيل."""
    if event.exception:
        logger.error(f"[Scheduler] خطأ في المهمة: {event.exception}")
    else:
        logger.debug("[Scheduler] اكتملت المهمة بنجاح")


def shutdown(signum, frame) -> None:
    """يُغلق المجدول عند استقبال SIGTERM/SIGINT."""
    logger.info("[Scheduler] استقبلت إشارة إيقاف — جارٍ الإغلاق...")
    db.close_pool()
    sys.exit(0)


def main() -> None:
    """نقطة الدخول الرئيسية."""
    logger.info("=" * 60)
    logger.info("نظام التداول الذكي — يبدأ التشغيل")
    logger.info(f"الفاصل الزمني: كل {INTERVAL_MINUTES} دقائق")
    logger.info("=" * 60)

    # تهيئة قاعدة البيانات
    if not db.init_db():
        logger.critical("فشل تهيئة قاعدة البيانات — إيقاف")
        sys.exit(1)

    # تسجيل إشارات الإيقاف
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_listener(on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

    # جدولة الدورة
    scheduler.add_job(
        run_cycle,
        trigger="interval",
        minutes=INTERVAL_MINUTES,
        id="trading_cycle",
        name="دورة التداول الكاملة",
        max_instances=1,         # منع تداخل الدورات
        coalesce=True,           # تجميع التشغيلات الفائتة في واحدة
        misfire_grace_time=60,   # تجاهل إذا تأخر أكثر من 60 ثانية
    )

    logger.info("تشغيل دورة أولى فورية...")
    run_cycle()   # دورة فورية عند البدء

    logger.info(f"المجدول يعمل — التالية بعد {INTERVAL_MINUTES} دقائق")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[Scheduler] إيقاف يدوي")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()

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
import scalper

load_dotenv()

# ─── إعداد اللوجر ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

# ─── الثوابت ──────────────────────────────────────────────────────────────────




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
    logger.info("نظام المضاربة السريعة — يبدأ التشغيل")
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

    # مضاربة سريعة — الفاصل من SCALP_INTERVAL_MINUTES
    _scalp_interval = int(os.getenv("SCALP_INTERVAL_MINUTES", "1"))
    if os.getenv("SCALP_ENABLED", "false").lower() == "true":
        scheduler.add_job(
            scalper.run,
            trigger="interval",
            minutes=_scalp_interval,
            id="scalp_cycle",
            name="دورة المضاربة السريعة",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
        logger.info(f"المضاربة السريعة مفعّلة — كل {_scalp_interval} دقيقة")
    else:
        logger.warning("المضاربة السريعة معطلة SCALP_ENABLED=false")

    logger.info(f"المجدول يعمل — المضارب كل {_scalp_interval} دقيقة")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[Scheduler] إيقاف يدوي")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()

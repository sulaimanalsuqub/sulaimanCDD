"""
database.py — إعداد قاعدة البيانات وإنشاء الجداول
يمكن تشغيله منفرداً: python database.py
"""

import os
import sys
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ─── إعداد اللوجر ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

# ─── إعدادات الاتصال ────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "trading_bot"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASS", ""),
}

# Connection pool — يُنشأ عند أول استخدام
_pool: pool.ThreadedConnectionPool | None = None


def get_pool() -> pool.ThreadedConnectionPool:
    """يُعيد connection pool موجود أو يُنشئ واحداً جديداً."""
    global _pool
    if _pool is None or _pool.closed:
        try:
            _pool = pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                **DB_CONFIG,
            )
            logger.info("تم إنشاء connection pool بنجاح")
        except psycopg2.OperationalError as e:
            logger.critical(f"فشل الاتصال بقاعدة البيانات: {e}")
            raise
    return _pool


@contextmanager
def get_conn():
    """Context manager يُعطي اتصالاً من الـ pool ويُعيده تلقائياً."""
    conn_pool = get_pool()
    conn = conn_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn_pool.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True):
    """Context manager يُعطي cursor جاهز للاستخدام."""
    cursor_factory = RealDictCursor if dict_cursor else None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur


# ─── SQL لإنشاء الجداول ─────────────────────────────────────────────────────────
CREATE_TABLES_SQL = """
-- ─────────────────────────────────────────────────────────────────────────────
-- جدول الدورات: يتتبع كل دورة تشغيل كاملة (جمع → تحليل → قرار → تنفيذ)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cycles (
    id           SERIAL PRIMARY KEY,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status       VARCHAR(40)  NOT NULL DEFAULT 'running',
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycles_started_at ON cycles (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cycles_status     ON cycles (status);

ALTER TABLE cycles DROP CONSTRAINT IF EXISTS cycles_status_check;
ALTER TABLE cycles ALTER COLUMN status TYPE VARCHAR(40);
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS collector_status VARCHAR(30);
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS analyzer_status  VARCHAR(30);
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS tweets_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS tweets_file_path TEXT;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS analysis_result  TEXT;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS error_message    TEXT;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS collected_at     TIMESTAMPTZ;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS analyzed_at      TIMESTAMPTZ;
ALTER TABLE cycles ADD COLUMN IF NOT EXISTS finished_at      TIMESTAMPTZ;

-- ─────────────────────────────────────────────────────────────────────────────
-- جدول التغريدات: يخزن التغريدات المجمعة من حسابات X
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tweets (
    id           BIGSERIAL PRIMARY KEY,
    cycle_id     INTEGER     NOT NULL REFERENCES cycles (id) ON DELETE CASCADE,
    account      VARCHAR(50) NOT NULL,
    tweet_id     VARCHAR(30) UNIQUE,          -- لمنع التكرار
    content      TEXT        NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE tweets ALTER COLUMN tweet_id TYPE VARCHAR(80);
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS tweet_created_at TIMESTAMPTZ;
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS tweet_url        TEXT;
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS likes            INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS retweets         INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS replies          INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tweets ADD COLUMN IF NOT EXISTS views            INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tweets_cycle_id     ON tweets (cycle_id);
CREATE INDEX IF NOT EXISTS idx_tweets_collected_at ON tweets (collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_account      ON tweets (account);

-- ─────────────────────────────────────────────────────────────────────────────
-- جدول التحليلات: نتائج Claude API
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analyses (
    id              SERIAL PRIMARY KEY,
    cycle_id        INTEGER      NOT NULL REFERENCES cycles (id) ON DELETE CASCADE,
    sentiment       VARCHAR(10)  NOT NULL CHECK (sentiment IN ('bullish', 'bearish', 'neutral')),
    coins           JSONB        NOT NULL DEFAULT '[]',   -- [{"symbol":"BTC","sentiment":"bullish","mentions":15}]
    confidence      SMALLINT     NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    reasoning       TEXT,
    analyzed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_analyses_cycle_id    ON analyses (cycle_id);
CREATE INDEX IF NOT EXISTS idx_analyses_analyzed_at ON analyses (analyzed_at DESC);
CREATE INDEX IF NOT EXISTS idx_analyses_confidence  ON analyses (confidence);

-- ─────────────────────────────────────────────────────────────────────────────
-- جدول القرارات: buy / sell / hold لكل عملة
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS decisions (
    id          SERIAL PRIMARY KEY,
    cycle_id    INTEGER       NOT NULL REFERENCES cycles (id) ON DELETE CASCADE,
    coin        VARCHAR(20)   NOT NULL,
    action      VARCHAR(5)    NOT NULL CHECK (action IN ('buy', 'sell', 'hold')),
    confidence  SMALLINT      NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    amount      NUMERIC(18,8) NOT NULL DEFAULT 0,
    reason      TEXT,
    decided_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decisions_cycle_id   ON decisions (cycle_id);
CREATE INDEX IF NOT EXISTS idx_decisions_decided_at ON decisions (decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_coin       ON decisions (coin);

-- ─────────────────────────────────────────────────────────────────────────────
-- جدول الصفقات: نتائج التنفيذ الفعلي على Binance
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id          SERIAL PRIMARY KEY,
    cycle_id    INTEGER       NOT NULL REFERENCES cycles (id) ON DELETE CASCADE,
    coin        VARCHAR(20)   NOT NULL,
    action      VARCHAR(5)    NOT NULL CHECK (action IN ('buy', 'sell')),
    amount      NUMERIC(18,8) NOT NULL,
    price       NUMERIC(18,8),
    order_id    VARCHAR(50),
    status      VARCHAR(20)   NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'filled', 'failed', 'cancelled')),
    pnl         NUMERIC(18,8) DEFAULT 0,    -- الربح/الخسارة لهذه الصفقة
    executed_at TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_cycle_id     ON trades (cycle_id);
CREATE INDEX IF NOT EXISTS idx_trades_executed_at  ON trades (executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_coin         ON trades (coin);
CREATE INDEX IF NOT EXISTS idx_trades_status       ON trades (status);
"""


def init_db() -> bool:
    """ينشئ الجداول إذا لم تكن موجودة. يُعيد True عند النجاح."""
    logger.info("جارٍ تهيئة قاعدة البيانات...")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLES_SQL)
        logger.success("تمت تهيئة قاعدة البيانات وإنشاء الجداول بنجاح ✓")
        return True
    except Exception as e:
        logger.error(f"فشل تهيئة قاعدة البيانات: {e}")
        return False


# ─── دوال مساعدة للـ CRUD ────────────────────────────────────────────────────────

def create_cycle() -> int:
    """ينشئ دورة جديدة ويُعيد الـ ID."""
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO cycles (status, collector_status, analyzer_status)
               VALUES ('running', 'pending', 'pending')
               RETURNING id"""
        )
        row = cur.fetchone()
        cycle_id = row["id"]
    logger.info(f"بدأت دورة جديدة: #{cycle_id}")
    return cycle_id


def update_cycle(
    cycle_id: int,
    *,
    status: str | None = None,
    collector_status: str | None = None,
    analyzer_status: str | None = None,
    tweets_count: int | None = None,
    tweets_file_path: str | None = None,
    analysis_result: str | None = None,
    error_message: str | None = None,
    mark_collected: bool = False,
    mark_analyzed: bool = False,
    mark_finished: bool = False,
) -> None:
    """يحدث حقول دورة واحدة بدون حذف بيانات قديمة."""
    assignments = []
    values = []
    fields = {
        "status": status,
        "collector_status": collector_status,
        "analyzer_status": analyzer_status,
        "tweets_count": tweets_count,
        "tweets_file_path": tweets_file_path,
        "analysis_result": analysis_result,
        "error_message": error_message,
        "error": error_message,
    }
    for column, value in fields.items():
        if value is not None:
            assignments.append(f"{column} = %s")
            values.append(value)

    if mark_collected:
        assignments.append("collected_at = NOW()")
    if mark_analyzed:
        assignments.append("analyzed_at = NOW()")
    if mark_finished:
        assignments.append("finished_at = NOW()")
        assignments.append("completed_at = NOW()")

    if not assignments:
        return

    values.append(cycle_id)
    with get_cursor() as cur:
        cur.execute(
            f"UPDATE cycles SET {', '.join(assignments)} WHERE id = %s",
            tuple(values),
        )


def complete_cycle(cycle_id: int) -> None:
    """يُعلم الدورة باكتمالها."""
    update_cycle(cycle_id, status="completed", mark_finished=True)
    logger.info(f"اكتملت الدورة #{cycle_id}")


def fail_cycle(cycle_id: int, error: str) -> None:
    """يُسجّل فشل الدورة مع رسالة الخطأ."""
    update_cycle(
        cycle_id,
        status="failed",
        error_message=error,
        mark_finished=True,
    )
    logger.warning(f"فشلت الدورة #{cycle_id}: {error}")


def save_tweet(
    cycle_id: int,
    account: str,
    tweet_id: str,
    content: str,
    *,
    created_at=None,
    url: str | None = None,
    likes: int = 0,
    retweets: int = 0,
    replies: int = 0,
    views: int = 0,
) -> bool:
    """يحفظ تغريدة جديدة — يتجاهل المكررة. يُعيد True إذا حُفظت."""
    try:
        with get_cursor() as cur:
            cur.execute(
                """INSERT INTO tweets (
                       cycle_id, account, tweet_id, content, tweet_created_at,
                       tweet_url, likes, retweets, replies, views
                   )
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (tweet_id) DO NOTHING
                   RETURNING id""",
                (
                    cycle_id, account, tweet_id, content, created_at,
                    url, likes, retweets, replies, views,
                ),
            )
            return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"خطأ في حفظ التغريدة [{tweet_id}]: {e}")
        return False


def get_recent_tweets(minutes: int = 5) -> list[dict]:
    """يجلب التغريدات الأخيرة خلال عدد دقائق محدد."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT account, content, collected_at
               FROM   tweets
               WHERE  collected_at >= NOW() - (%s || ' minutes')::INTERVAL
               ORDER  BY collected_at DESC""",
            (minutes,),
        )
        return cur.fetchall()


def get_cycle_tweets(cycle_id: int) -> list[dict]:
    """يجلب تغريدات دورة محددة للتحليل أو العرض."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT account, tweet_id, content, tweet_created_at, tweet_url,
                      likes, retweets, replies, views, collected_at
               FROM   tweets
               WHERE  cycle_id = %s
               ORDER  BY collected_at ASC""",
            (cycle_id,),
        )
        return cur.fetchall()


def save_analysis(cycle_id: int, sentiment: str, coins: list,
                  confidence: int, reasoning: str) -> int:
    """يحفظ نتيجة تحليل Claude ويُعيد الـ ID."""
    with get_cursor() as cur:
        import json
        cur.execute(
            """INSERT INTO analyses (cycle_id, sentiment, coins, confidence, reasoning)
               VALUES (%s, %s, %s::jsonb, %s, %s)
               RETURNING id""",
            (cycle_id, sentiment, json.dumps(coins), confidence, reasoning),
        )
        return cur.fetchone()["id"]


def save_cycle_analysis_result(cycle_id: int, result: dict) -> None:
    """يحفظ نتيجة التحليل النهائية داخل سجل الدورة نفسه."""
    import json

    update_cycle(
        cycle_id,
        analysis_result=json.dumps(result, ensure_ascii=False),
        analyzer_status="completed",
        status="analyzed",
        mark_analyzed=True,
        mark_finished=True,
    )


def get_latest_analysis() -> dict | None:
    """يجلب آخر تحليل محفوظ."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT * FROM analyses ORDER BY analyzed_at DESC LIMIT 1"""
        )
        return cur.fetchone()


def get_analysis_for_cycle(cycle_id: int) -> dict | None:
    """يجلب تحليل دورة محددة فقط حتى لا تُستخدم نتيجة دورة أخرى."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT * FROM analyses
               WHERE cycle_id = %s
               ORDER BY analyzed_at DESC
               LIMIT 1""",
            (cycle_id,),
        )
        return cur.fetchone()


def get_current_cycle() -> dict | None:
    """يجلب آخر دورة بكل حقول حالة سير العمل."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT id AS cycle_id, status, collector_status, analyzer_status,
                      tweets_count, tweets_file_path, analysis_result,
                      COALESCE(error_message, error) AS error_message,
                      started_at, collected_at, analyzed_at, finished_at,
                      completed_at
               FROM   cycles
               ORDER  BY started_at DESC
               LIMIT  1"""
        )
        return cur.fetchone()


def get_cycle(cycle_id: int) -> dict | None:
    """يجلب دورة محددة بكل حقول حالة سير العمل."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT id AS cycle_id, status, collector_status, analyzer_status,
                      tweets_count, tweets_file_path, analysis_result,
                      COALESCE(error_message, error) AS error_message,
                      started_at, collected_at, analyzed_at, finished_at,
                      completed_at
               FROM   cycles
               WHERE  id = %s
               LIMIT  1""",
            (cycle_id,),
        )
        return cur.fetchone()


def get_cycles(limit: int = 15) -> list[dict]:
    """يجلب سجل الدورات الجديد مع توافق مع الحقول القديمة."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT id AS cycle_id, id, status, collector_status, analyzer_status,
                      tweets_count, tweets_file_path, analysis_result,
                      COALESCE(error_message, error) AS error_message,
                      started_at, collected_at, analyzed_at, finished_at,
                      completed_at
               FROM   cycles
               ORDER  BY started_at DESC
               LIMIT  %s""",
            (limit,),
        )
        return cur.fetchall()


def save_decision(cycle_id: int, coin: str, action: str,
                  confidence: int, amount: float, reason: str) -> int:
    """يحفظ قرار التداول ويُعيد الـ ID."""
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO decisions (cycle_id, coin, action, confidence, amount, reason)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (cycle_id, coin, action, confidence, amount, reason),
        )
        return cur.fetchone()["id"]


def get_latest_decisions() -> list[dict]:
    """يجلب قرارات آخر دورة مكتملة."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT d.*
               FROM   decisions d
               JOIN   cycles c ON c.id = d.cycle_id
               WHERE  c.id = (
                   SELECT id FROM cycles
                   WHERE  status NOT IN ('failed', 'collector_failed', 'analyzer_failed')
                   ORDER  BY started_at DESC
                   LIMIT  1
               )
               ORDER  BY d.confidence DESC"""
        )
        return cur.fetchall()


def get_decisions_for_cycle(cycle_id: int) -> list[dict]:
    """يجلب قرارات دورة محددة فقط."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT *
               FROM   decisions
               WHERE  cycle_id = %s
               ORDER  BY confidence DESC""",
            (cycle_id,),
        )
        return cur.fetchall()


def save_trade(cycle_id: int, coin: str, action: str, amount: float,
               price: float | None, order_id: str | None, status: str) -> int:
    """يحفظ نتيجة صفقة منفذة ويُعيد الـ ID."""
    with get_cursor() as cur:
        cur.execute(
            """INSERT INTO trades (cycle_id, coin, action, amount, price, order_id, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (cycle_id, coin, action, amount, price, order_id, status),
        )
        return cur.fetchone()["id"]


def get_total_pnl() -> float:
    """يحسب إجمالي الربح/الخسارة من جميع الصفقات."""
    with get_cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE status = 'filled'")
        return float(cur.fetchone()["total"])


def get_stats() -> dict:
    """يجلب إحصائيات سريعة للـ dashboard."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                (SELECT COALESCE(tweets_count, 0)
                 FROM cycles ORDER BY started_at DESC LIMIT 1) AS recent_tweets,
                (SELECT COUNT(*) FROM trades WHERE status = 'filled')  AS total_trades,
                (SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = 'filled') AS total_pnl,
                (SELECT started_at FROM cycles ORDER BY started_at DESC LIMIT 1) AS last_cycle,
                (SELECT status    FROM cycles ORDER BY started_at DESC LIMIT 1) AS last_status
        """)
        return dict(cur.fetchone())


def close_pool() -> None:
    """يغلق الـ connection pool عند إيقاف التطبيق."""
    global _pool
    if _pool and not _pool.closed:
        _pool.closeall()
        logger.info("تم إغلاق connection pool")


# ─── تشغيل مباشر للاختبار ───────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("═" * 60)
    logger.info("تشغيل database.py للاختبار المستقل")
    logger.info("═" * 60)

    success = init_db()
    if not success:
        sys.exit(1)

    # اختبار بسيط
    try:
        stats = get_stats()
        logger.success(f"الاتصال يعمل — الإحصائيات: {stats}")
    except Exception as e:
        logger.error(f"فشل الاختبار: {e}")
        sys.exit(1)
    finally:
        close_pool()

    logger.success("database.py اجتاز الاختبار بنجاح ✓")

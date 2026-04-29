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
    status       VARCHAR(20)  NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'failed')),
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycles_started_at ON cycles (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cycles_status     ON cycles (status);

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
            "INSERT INTO cycles (status) VALUES ('running') RETURNING id"
        )
        row = cur.fetchone()
        cycle_id = row["id"]
    logger.info(f"بدأت دورة جديدة: #{cycle_id}")
    return cycle_id


def complete_cycle(cycle_id: int) -> None:
    """يُعلم الدورة باكتمالها."""
    with get_cursor() as cur:
        cur.execute(
            """UPDATE cycles
               SET status = 'completed', completed_at = NOW()
               WHERE id = %s""",
            (cycle_id,),
        )
    logger.info(f"اكتملت الدورة #{cycle_id}")


def fail_cycle(cycle_id: int, error: str) -> None:
    """يُسجّل فشل الدورة مع رسالة الخطأ."""
    with get_cursor() as cur:
        cur.execute(
            """UPDATE cycles
               SET status = 'failed', completed_at = NOW(), error = %s
               WHERE id = %s""",
            (error, cycle_id),
        )
    logger.warning(f"فشلت الدورة #{cycle_id}: {error}")


def save_tweet(cycle_id: int, account: str, tweet_id: str, content: str) -> bool:
    """يحفظ تغريدة جديدة — يتجاهل المكررة. يُعيد True إذا حُفظت."""
    try:
        with get_cursor() as cur:
            cur.execute(
                """INSERT INTO tweets (cycle_id, account, tweet_id, content)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (tweet_id) DO NOTHING
                   RETURNING id""",
                (cycle_id, account, tweet_id, content),
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


def get_latest_analysis() -> dict | None:
    """يجلب آخر تحليل محفوظ."""
    with get_cursor() as cur:
        cur.execute(
            """SELECT * FROM analyses ORDER BY analyzed_at DESC LIMIT 1"""
        )
        return cur.fetchone()


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
                   WHERE  status != 'failed'
                   ORDER  BY started_at DESC
                   LIMIT  1
               )
               ORDER  BY d.confidence DESC"""
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
                (SELECT COUNT(*) FROM tweets
                 WHERE collected_at >= NOW() - INTERVAL '5 minutes') AS recent_tweets,
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

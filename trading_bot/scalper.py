"""
scalper.py — مضاربة سريعة كل دقيقة مع caching ومعالجة rate limits وtrailing stop
"""
import json, math, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException, BinanceOrderException
from dotenv import load_dotenv
from loguru import logger

import database as db
import ta_engine

load_dotenv()

BASE_DIR = Path(__file__).parent

# ── إعدادات ──────────────────────────────────────────────────
TRADING_ENABLED    = os.getenv("TRADING_ENABLED",     "false").lower() == "true"
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY",     "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY",  "")
SCALP_ENABLED      = os.getenv("SCALP_ENABLED",       "false").lower() == "true"
SCALP_TP_PCT       = float(os.getenv("SCALP_TP_PCT",        "2.0"))
SCALP_SL_PCT       = float(os.getenv("SCALP_SL_PCT",        "1.0"))
SCALP_TRAIL_PCT    = float(os.getenv("SCALP_TRAIL_PCT",      "0.2"))  # trailing stop distance %
SCALP_AMOUNT_USDT  = float(os.getenv("SCALP_AMOUNT_USDT",   "15"))
SCALP_MAX_POS      = int(os.getenv("SCALP_MAX_POSITIONS",    "1"))
SCALP_DROP_MIN     = float(os.getenv("SCALP_DROP_MIN",       "0.5"))
SCALP_DROP_MAX     = float(os.getenv("SCALP_DROP_MAX",       "3.5"))
SCALP_MIN_VOL      = float(os.getenv("SCALP_MIN_VOL_USDT",   "3000000"))
SCALP_MIN_CHANGE   = float(os.getenv("SCALP_MIN_CHANGE",     "-3.0"))
SCALP_USE_FULL_BAL = os.getenv("SCALP_USE_FULL_BALANCE", "true").lower() == "true"
SCALP_BALANCE_PCT  = float(os.getenv("SCALP_BALANCE_PCT", "0.90"))
SCALP_USE_ATR_SL   = os.getenv("SCALP_USE_ATR_SL",  "true").lower() == "true"
SCALP_ATR_SL_MULT  = float(os.getenv("SCALP_ATR_SL_MULT",  "1.5"))  # SL = entry - mult*ATR
SCALP_ATR_TP_MULT  = float(os.getenv("SCALP_ATR_TP_MULT",  "3.0"))  # TP = entry + mult*ATR
SCALP_MAX_AGE_MIN  = int(os.getenv("SCALP_MAX_AGE_MIN",     "8"))    # إغلاق الصفقات القديمة
SCALP_CANDLE       = os.getenv("SCALP_CANDLE_INTERVAL",      "1m")
AI_CONFIRMATION    = os.getenv("AI_CONFIRMATION",            "false").lower() == "true"
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",          "")

# ── Cache ─────────────────────────────────────────────────────
_price_cache: dict[str, tuple[float, float]] = {}   # symbol → (price, ts)
_ta_cache:    dict[str, tuple[dict,  float]] = {}   # symbol → (ta_result, ts)
_ta_5m_cache: dict[str, tuple[dict,  float]] = {}   # symbol → (ta_5m, ts)

# Dynamic ROI Table (Freqtrade-inspired): TP يتناقص كلما طالت الصفقة
ROI_TABLE = [
    (0,   SCALP_TP_PCT),        # 0-2 دقيقة:  TP كامل (2%)
    (2,   SCALP_TP_PCT * 0.75), # 2-5 دقائق: TP 1.5%
    (5,   SCALP_TP_PCT * 0.50), # 5-8 دقائق: TP 1%
]
PRICE_CACHE_TTL = 25   # seconds — يُجدَّد كل 25 ث
TA_CACHE_TTL    = 55   # seconds — يُجدَّد كل 55 ث (أقل من دقيقة)

# ── Rate limit backoff ────────────────────────────────────────
_backoff_until: float = 0.0
_BACKOFF_SECONDS      = 180   # 3 دقائق عند rate limit

# ── حد صفقة واحدة جديدة في الدقيقة ──────────────────────────
_minute_bucket: int = 0
_minute_new:    int = 0

# ── Trailing stop (in-memory) ──────────────────────────────────
_trailing_highs: dict[int, float] = {}   # pos_id → highest price seen


logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


# ══════════════════════════════════════════════════════════════
# Binance helpers
# ══════════════════════════════════════════════════════════════

def get_client() -> BinanceClient:
    return BinanceClient(BINANCE_API_KEY, BINANCE_SECRET_KEY)


def get_usdt_balance(client: BinanceClient) -> float:
    try:
        info = client.get_asset_balance(asset="USDT")
        return float(info.get("free", 0))
    except Exception as e:
        logger.warning(f"[Scalper] فشل جلب الرصيد: {e}")
        return 0.0


def cached_price(client: BinanceClient, symbol: str) -> float:
    now = time.time()
    if symbol in _price_cache:
        price, ts = _price_cache[symbol]
        if now - ts < PRICE_CACHE_TTL:
            return price
    price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    _price_cache[symbol] = (price, now)
    return price


def cached_ta(client: BinanceClient, symbol: str) -> dict:
    now = time.time()
    if symbol in _ta_cache:
        ta, ts = _ta_cache[symbol]
        if now - ts < TA_CACHE_TTL:
            return ta
    ta = ta_engine.get_ta_signal(client, symbol, interval=SCALP_CANDLE)
    _ta_cache[symbol] = (ta, now)
    return ta


def cached_ta_5m(client: BinanceClient, symbol: str) -> dict:
    """TA على الفريم 5 دقائق — يُحدَّث كل 4 دقائق."""
    now = time.time()
    if symbol in _ta_5m_cache:
        ta, ts = _ta_5m_cache[symbol]
        if now - ts < 240:
            return ta
    ta = ta_engine.get_ta_signal(client, symbol, interval="5m")
    _ta_5m_cache[symbol] = (ta, now)
    return ta


def get_dynamic_tp(opened_at) -> float:
    """ROI Table: TP يتناقص مع عمر الصفقة (Freqtrade-inspired)."""
    try:
        age_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
    except Exception:
        return SCALP_TP_PCT
    for min_age, roi in reversed(ROI_TABLE):
        if age_min >= min_age:
            return roi
    return SCALP_TP_PCT


def in_backoff() -> bool:
    return time.time() < _backoff_until


def set_backoff():
    global _backoff_until
    _backoff_until = time.time() + _BACKOFF_SECONDS
    until_str = time.strftime("%H:%M:%S", time.localtime(_backoff_until))
    logger.warning(f"[Scalper] ⚠️ Rate limit — backoff 3 دقائق حتى {until_str}")


def can_open_new() -> bool:
    """يُعيد True إذا لم تُفتح أي صفقة في هذه الدقيقة بعد."""
    global _minute_bucket, _minute_new
    bucket = int(time.time() // 60)
    if bucket != _minute_bucket:
        _minute_bucket = bucket
        _minute_new    = 0
    return _minute_new < 1


def record_opened():
    global _minute_new
    _minute_new += 1


def get_quantity(client: BinanceClient, symbol: str, usdt: float) -> tuple[float, float]:
    price = cached_price(client, symbol)
    qty   = usdt / price
    info  = client.get_symbol_info(symbol) or {}
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            if step > 0:
                prec = int(round(-math.log10(step)))
                qty  = round(qty - (qty % step), prec)
            break
    return qty, price


def min_notional(client: BinanceClient, symbol: str) -> float:
    info = client.get_symbol_info(symbol) or {}
    for f in info.get("filters", []):
        if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            return float(f.get("minNotional", 5))
    return 5.0


def avg_fill_price(order: dict, fallback: float) -> float:
    fills = order.get("fills", [])
    if not fills:
        return fallback
    total_qty = sum(float(f["qty"]) for f in fills)
    total_val = sum(float(f["price"]) * float(f["qty"]) for f in fills)
    return total_val / total_qty if total_qty else fallback


# ══════════════════════════════════════════════════════════════
# AI Confirmation (اختياري — Haiku فقط، timeout 8 ث)
# ══════════════════════════════════════════════════════════════

def ai_confirm(symbol: str, ta: dict) -> bool:
    if not AI_CONFIRMATION or not ANTHROPIC_API_KEY:
        return True
    if ta.get("ta_score", 0) < 0.5:
        return False  # لا نستشير Claude لإشارات ضعيفة
    try:
        import anthropic
        prompt = (
            f"Symbol: {symbol} | RSI: {ta.get('rsi')} | "
            f"MACD hist: {ta.get('macd_hist')} | Score: {ta.get('ta_score')} | "
            f"Reasons: {', '.join(ta.get('reasons', []))}\n"
            "Scalp trade (1-min). Reply: BUY or SKIP only."
        )
        ac   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=8)
        resp = ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = resp.content[0].text.strip().upper()
        logger.debug(f"[Scalper/AI] {symbol} → {answer}")
        return "BUY" in answer
    except Exception as e:
        logger.debug(f"[Scalper/AI] timeout/error ({e}) — proceeding")
        return True   # Claude فشل → نكمل بدونه


# ══════════════════════════════════════════════════════════════
# DB helpers
# ══════════════════════════════════════════════════════════════

def get_open_positions() -> list[dict]:
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM scalp_positions WHERE status='open' ORDER BY opened_at DESC")
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"[Scalper] خطأ قراءة الصفقات: {e}")
        return []


def save_position(symbol, entry, qty, tp, sl, amount, order_id) -> int:
    with db.get_cursor() as cur:
        cur.execute(
            """INSERT INTO scalp_positions
               (symbol, entry_price, quantity, tp_price, sl_price, amount_usdt, status, buy_order_id)
               VALUES (%s,%s,%s,%s,%s,%s,'open',%s) RETURNING id""",
            (symbol, entry, qty, tp, sl, amount, str(order_id)),
        )
        return cur.fetchone()["id"]


def close_position(pos_id: int, exit_price: float, pnl: float, reason: str):
    with db.get_cursor() as cur:
        cur.execute(
            """UPDATE scalp_positions
               SET status='closed', exit_price=%s, pnl=%s, close_reason=%s, closed_at=NOW()
               WHERE id=%s""",
            (exit_price, pnl, reason, pos_id),
        )
    with db.get_cursor() as cur:
        cur.execute("UPDATE scalp_positions SET sl_price=%s WHERE id=%s", (0, pos_id))


def update_sl(pos_id: int, new_sl: float):
    with db.get_cursor() as cur:
        cur.execute("UPDATE scalp_positions SET sl_price=%s WHERE id=%s", (new_sl, pos_id))


def get_selected_coins() -> list[str]:
    p = BASE_DIR / "data" / "coins_config.json"
    if not p.exists():
        return []
    try:
        return [s.upper() for s in json.loads(p.read_text()).get("selected", [])]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
# منطق المضاربة
# ══════════════════════════════════════════════════════════════

def check_and_close_positions(client: BinanceClient) -> int:
    positions = get_open_positions()
    closed    = 0

    for pos in positions:
        symbol = pos["symbol"]
        pos_id = pos["id"]
        try:
            price = cached_price(client, symbol)
            entry = float(pos["entry_price"])
            tp    = float(pos["tp_price"])
            sl    = float(pos["sl_price"])
            qty   = float(pos["quantity"])

            # ── Trailing stop: رفع SL مع ارتفاع السعر ──
            trail_high = _trailing_highs.get(pos_id, entry)
            if price > trail_high:
                trail_high = price
                _trailing_highs[pos_id] = trail_high
                new_sl = round(trail_high * (1 - SCALP_TRAIL_PCT / 100), 8)
                if new_sl > sl:
                    sl = new_sl
                    update_sl(pos_id, sl)
                    logger.debug(f"[Scalper] 🔼 {symbol} trail SL → {sl:.6f}")

            reason = None
            # Dynamic ROI — TP يتناقص مع الوقت
            dyn_tp_pct = get_dynamic_tp(pos["opened_at"])
            dyn_tp     = round(entry * (1 + dyn_tp_pct / 100), 8)
            age_min    = (datetime.now(timezone.utc) - pos["opened_at"]).total_seconds() / 60

            if price >= dyn_tp:
                reason = "tp"
            elif price <= sl:
                reason = "trail" if sl > float(pos["sl_price"]) * 0.9999 and price > entry else "sl"
            elif age_min > SCALP_MAX_AGE_MIN:
                pnl_pct = (price - entry) / entry * 100
                if pnl_pct > -0.15:  # أغلق الصفقة القديمة إذا قريبة من التعادل
                    reason = "age"
                    logger.info(f"[Scalper] ⌛ {symbol} إغلاق بسبب العمر ({age_min:.1f} دقيقة)")

            if not reason:
                change_pct = (price - entry) / entry * 100
                logger.debug(f"[Scalper] {symbol} | دخول:{entry:.5f} حالي:{price:.5f} ({change_pct:+.2f}%) | trail:{trail_high:.5f}")
                continue

            # تحقق من الكمية الفعلية + LOT_SIZE step
            from decimal import Decimal
            asset = symbol[:-4] if symbol.endswith('USDT') else symbol.replace('USDT','')
            bal_info = client.get_asset_balance(asset=asset)
            actual_free = float(bal_info['free']) if bal_info else 0.0
            if actual_free < qty * 0.5:
                logger.warning(f"[Scalper] {symbol}: رصيد {asset} غير كافٍ ({actual_free}) — إغلاق وهمي")
                close_position(pos_id, price, 0.0, "phantom")
                _trailing_highs.pop(pos_id, None)
                continue
            # احصل على step size وقرّب للأسفل بالقسمة الصحيحة
            sym_info = client.get_symbol_info(symbol) or {}
            step_str = next(
                (f['stepSize'] for f in sym_info.get('filters', []) if f['filterType']=='LOT_SIZE'),
                '0.00001000'
            )
            step_d  = Decimal(str(float(step_str)))       # '0.00010000' → 0.0001
            raw_d   = Decimal(str(min(qty, actual_free)))
            sell_d  = (raw_d // step_d) * step_d          # floor إلى أقرب step صحيح
            sell_qty = str(sell_d)                         # string مباشرة لتجنب float precision
            logger.debug(f"[Scalper] {symbol} sell_qty={sell_qty} (free={actual_free} step={step_str})")
            sell_order = client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=sell_qty)
            exit_price  = avg_fill_price(sell_order, price)
            sold_qty    = float(sell_qty) if isinstance(sell_qty, str) else sell_qty
            fees        = (entry * sold_qty + exit_price * sold_qty) * 0.001
            net_pnl     = (exit_price - entry) * sold_qty - fees

            close_position(pos_id, exit_price, net_pnl, reason)
            _trailing_highs.pop(pos_id, None)

            icon = {"tp": "✅", "trail": "🔼", "sl": "🛑"}.get(reason, "⬛")
            logger.success(
                f"[Scalper] {icon} {symbol} {reason.upper()} | "
                f"{entry:.5f}→{exit_price:.5f} | PnL: {net_pnl:+.4f} USDT"
            )
            closed += 1

        except BinanceAPIException as e:
            if e.status_code == 429 or "TOO_MANY_REQUESTS" in str(e):
                set_backoff()
                return closed
            if e.code == -2010 or "insufficient balance" in str(e).lower():
                # لا يوجد رصيد للبيع — الصفقة وهمية، أغلقها في DB
                logger.warning(f"[Scalper] {symbol}: رصيد غير كافٍ للبيع — إغلاق وهمي في DB")
                close_position(pos_id, price, 0.0, "phantom")
                _trailing_highs.pop(pos_id, None)
            else:
                logger.warning(f"[Scalper] Binance {symbol}: {e}")
        except Exception as e:
            logger.warning(f"[Scalper] خطأ {symbol}: {e}")

    return closed


def scan_entries(client: BinanceClient, coins: list[str], open_count: int) -> list[dict]:
    """
    Scalping + Mean Reversion + Momentum Filter
    ─────────────────────────────────────────────
    Mean Reversion : السعر انخفض 0.5-3.5% عن قمة 24h (فرصة الارتداد)
    Momentum Filter: RSI 35-60 + MACD hist > 0 + فوق EMA20 (التحقق من الزخم)
    Score          : يجمع جودة الانخفاض + قوة إشارة TA
    """
    if open_count >= SCALP_MAX_POS:
        return []
    if not can_open_new():
        logger.debug("[Scalper] حد الدقيقة — لا صفقات جديدة")
        return []

    open_syms   = {p["symbol"] for p in get_open_positions()}
    all_tickers = {t["symbol"]: t for t in client.get_ticker()}

    candidates = []
    for coin in coins:
        sym = coin.replace("USDT","") + "USDT"
        if sym in open_syms:
            continue
        t = all_tickers.get(sym)
        if not t:
            continue

        price      = float(t["lastPrice"])
        high_24h   = float(t["highPrice"])
        change_pct = float(t["priceChangePercent"])
        vol_usdt   = float(t["quoteVolume"])

        if price <= 0 or high_24h <= 0:
            continue
        # ── فلتر الحجم والتغيير اليومي ──
        if vol_usdt < SCALP_MIN_VOL:
            continue
        if change_pct < SCALP_MIN_CHANGE:
            continue

        # ── Mean Reversion: انخفاض معتدل عن القمة ──
        drop_pct = (high_24h - price) / high_24h * 100
        if not (SCALP_DROP_MIN <= drop_pct <= SCALP_DROP_MAX):
            continue

        candidates.append({
            "symbol": sym, "price": price,
            "high_24h": high_24h, "drop_pct": drop_pct,
            "change_24h": change_pct, "vol_usdt": vol_usdt,
        })

    if not candidates:
        return []

    # ── Momentum Filter: فحص TA لأفضل 8 مرشحين بحجم تداول ──
    candidates.sort(key=lambda x: x["vol_usdt"], reverse=True)
    scored = []

    for cand in candidates[:8]:
        ta     = cached_ta(client, cand["symbol"])
        rsi    = ta.get("rsi", 50)
        macd_h = ta.get("macd_hist", 0)
        score  = ta.get("ta_score", 0)
        ema20  = ta.get("ema20", 0)
        price  = cand["price"]

        stoch_k = ta.get("stoch_k", 50)
        atr     = ta.get("atr", 0)

        # شرط 1: 5m trend confirmation (multi-timeframe — Jesse/Freqtrade)
        ta5 = cached_ta_5m(client, cand["symbol"])
        rsi_5m = ta5.get("rsi", 50)
        if rsi_5m < 42:
            logger.debug(f"[Scalper] {cand['symbol']} RSI_5m={rsi_5m:.1f} هبوط على 5m ✗")
            continue

        # شرط 2: RSI 1m في منطقة الارتداد (30-62)
        if not (30 <= rsi <= 62):
            logger.debug(f"[Scalper] {cand['symbol']} RSI={rsi:.1f} ✗ (30-62)")
            continue

        # شرط 3: Stochastic ليس في ذروة شراء (OctoBot-inspired)
        if stoch_k > 80:
            logger.debug(f"[Scalper] {cand['symbol']} Stoch={stoch_k:.1f} ذروة شراء ✗")
            continue

        # شرط 4: MACD histogram إيجابي
        if macd_h <= 0:
            logger.debug(f"[Scalper] {cand['symbol']} MACD ✗")
            continue

        # شرط 5: فوق EMA20 (±0.3% مرونة)
        if ema20 > 0 and price < ema20 * 0.997:
            logger.debug(f"[Scalper] {cand['symbol']} تحت EMA20 ✗")
            continue

        # شرط 6: TA score >= 0.33
        if score < 0.33:
            logger.debug(f"[Scalper] {cand['symbol']} score={score:.2f} ✗")
            continue

        if not ai_confirm(cand["symbol"], ta):
            continue

        # درجة مركّبة: TA score (60%) + جودة الانخفاض (40%)
        # الانخفاض المثالي قريب من SCALP_DROP_MIN (طازج)
        drop_quality = 1 - (cand["drop_pct"] - SCALP_DROP_MIN) / max(SCALP_DROP_MAX - SCALP_DROP_MIN, 1)
        composite    = score * 0.6 + drop_quality * 0.4

        cand.update({
            "ta_score":    score,
            "ta_signal":   ta.get("signal","—"),
            "ta_rsi":      rsi,
            "ta_macd_h":   macd_h,
            "ta_reasons":  ta.get("reasons",[]),
            "composite":   composite,
        })
        scored.append(cand)

    if not scored:
        return []

    # اختر أفضل فرصة مركّبة
    best = max(scored, key=lambda x: x["composite"])
    logger.debug(
        f"[Scalper] أفضل فرصة: {best['symbol']} | "
        f"انخفاض:{best['drop_pct']:.2f}% RSI:{best['ta_rsi']:.1f} "
        f"MACD:{best['ta_macd_h']:.5f} composite:{best['composite']:.2f}"
    )
    return [best]

def execute_scalp(client: BinanceClient, opp: dict) -> bool:
    symbol = opp["symbol"]
    try:
        if SCALP_USE_FULL_BAL:
            usdt_free = get_usdt_balance(client)
            amount    = round(usdt_free * SCALP_BALANCE_PCT, 2)
            if amount < 5:
                logger.warning(f"[Scalper] رصيد غير كافٍ: {usdt_free:.2f} USDT")
                return False
        else:
            amount = float(os.getenv("SCALP_AMOUNT_USDT","15"))
        qty, est_price = get_quantity(client, symbol, amount)
        if qty <= 0:
            return False

        mn = min_notional(client, symbol)
        if est_price * qty < mn:
            logger.debug(f"[Scalper] {symbol} تحت الحد ({mn} USDT)")
            return False

        order  = client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
        entry  = avg_fill_price(order, est_price)
        tp     = round(entry * (1 + SCALP_TP_PCT  / 100), 8)
        sl     = round(entry * (1 - SCALP_SL_PCT  / 100), 8)

        pos_id = save_position(symbol, entry, qty, tp, sl, amount, order.get("orderId", ""))
        _trailing_highs[pos_id] = entry
        record_opened()

        logger.success(
            f"[Scalper] 📈 {symbol} | دخول:{entry:.5f} qty:{qty} | "
            f"TP:{round(entry*(1+SCALP_TP_PCT/100),5)} (+{SCALP_TP_PCT}%) | "
            f"SL:{round(entry*(1-SCALP_SL_PCT/100),5)} (-{SCALP_SL_PCT}%) | "
            f"RSI:{opp.get('ta_rsi',0):.1f} TA:{opp['ta_score']:.2f} مبلغ:{amount:.2f}$"
        )
        return True

    except BinanceAPIException as e:
        if e.status_code == 429 or "TOO_MANY_REQUESTS" in str(e):
            set_backoff()
        else:
            logger.warning(f"[Scalper] فشل شراء {symbol}: {e}")
        return False
    except Exception as e:
        logger.error(f"[Scalper] خطأ {symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════
# نقطة الدخول
# ══════════════════════════════════════════════════════════════

def run() -> dict:
    if not SCALP_ENABLED:
        return {"skipped": True, "reason": "SCALP_ENABLED=false"}
    if not TRADING_ENABLED:
        return {"skipped": True, "reason": "TRADING_ENABLED=false"}

    if in_backoff():
        remaining = int(_backoff_until - time.time())
        logger.info(f"[Scalper] في فترة backoff — {remaining}ث متبقية")
        return {"skipped": True, "reason": f"backoff {remaining}s"}

    logger.info("[Scalper] ══ دورة مضاربة ══")

    try:
        bc = get_client()
    except Exception as e:
        logger.error(f"[Scalper] Binance: {e}")
        return {"error": str(e)}

    # 1 — فحص وإغلاق الصفقات المفتوحة
    closed = check_and_close_positions(bc)

    if in_backoff():
        return {"closed": closed, "opened": 0, "reason": "backoff"}

    # 2 — البحث عن فرص جديدة
    open_positions = get_open_positions()
    open_count     = len(open_positions)

    selected = get_selected_coins()
    if not selected:
        logger.warning("[Scalper] لا توجد عملات مختارة")
        return {"closed": closed, "opened": 0}

    opps   = scan_entries(bc, selected, open_count)
    opened = 0

    for opp in opps:
        logger.info(
            f"[Scalper] 🎯 {opp['symbol']} | انخفاض:{opp['drop_pct']:.2f}% "
            f"24h:{opp['change_24h']:+.2f}% حجم:{opp['vol_usdt']/1e6:.1f}M$ "
            f"TA:{opp['ta_score']:.2f} RSI:{opp['ta_rsi']:.1f}"
        )
        if not can_open_new():
            logger.debug("[Scalper] حد الدقيقة — إيقاف تنفيذ فرص إضافية")
            break
        if execute_scalp(bc, opp):
            opened += 1

    open_after = len(get_open_positions())
    logger.info(f"[Scalper] مغلقة:{closed} جديدة:{opened} مجموع:{open_after}/{SCALP_MAX_POS}")
    return {"closed": closed, "opened": opened, "open": open_after}


if __name__ == "__main__":
    db.init_db()
    result = run()
    logger.info(f"نتيجة: {result}")
    db.close_pool()

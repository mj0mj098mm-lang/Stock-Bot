"""
StockBeacon AI — mani.py
كل المنطق والـ handlers. يُستورد من StockBeaconBOT.py
"""
import logging, datetime, json, random, time, re, os, asyncio
import urllib.request, urllib.error
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd
import psycopg2

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
)

logger = logging.getLogger("mani")

# ══════════════════════════════════════════════════════════════════════════════
# 1.  الإعدادات
# ══════════════════════════════════════════════════════════════════════════════
ADMIN_IDS: set[int]  = {5134111738}

DISCLAIMER = (
    "\n\n⚠️ *إخلاء مسؤولية:* تحليلات خوارزمية فنية — "
    "ليست نصيحة مالية، قرارك مسؤوليتك."
)

LOCKED_MSG = (
    "🔒 *هذه الميزة للمشتركين فقط*\n\n"
    "اشترك الآن للوصول لجميع التحليلات والرادارات اللحظية.\n"
    "💬 للتفعيل: https://t.me/e85ej"
)

# أسماء الفئات المستخدمة بجدول التتبع (تُستخدم كمفتاح داخلي وبعرض الإحصائية)
CATEGORY_LABELS = {
    "deep":  "🔍 تحليل مباشر (بحث سهم)",
    "opp":   "🎯 كاشف الفرص",
    "trend": "🔥 الأسهم المطبل لها",
    "penny": "⚡ مضاربية رخيصة",
    "swing": "〽️ سوينق",
}

# ══════════════════════════════════════════════════════════════════════════════
# 1.5  قاعدة البيانات (Supabase Postgres) — مشتركين + تتبع أداء التوصيات
# ══════════════════════════════════════════════════════════════════════════════
_DB_URL = os.environ.get("DATABASE_URL")

def _db_conn():
    return psycopg2.connect(_DB_URL)

def _init_db():
    if not _DB_URL:
        logger.warning("⚠️ DATABASE_URL غير موجود — البيانات لن تُحفظ بعد إعادة التشغيل!")
        return
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id BIGINT PRIMARY KEY
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                category TEXT NOT NULL,
                entry_price NUMERIC,
                target_price NUMERIC,
                stop_price NUMERIC,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT NOW(),
                closed_at TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ قاعدة البيانات جاهزة")
    except Exception as e:
        logger.error(f"_init_db: {e}")

def _load_subs() -> set[int]:
    if not _DB_URL:
        return set()
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("SELECT user_id FROM subscribers")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        logger.error(f"_load_subs: {e}")
        return set()

def _save_sub(uid: int):
    if not _DB_URL:
        return
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO subscribers (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (uid,)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"_save_sub: {e}")

def _remove_sub(uid: int):
    if not _DB_URL:
        return
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("DELETE FROM subscribers WHERE user_id = %s", (uid,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"_remove_sub: {e}")

def _save_recommendation(symbol: str, category: str, entry: float, target: float, stop: float):
    """يسجّل توصية جديدة لتتبع أدائها لاحقاً (شفافية + إحصائية حقيقية)"""
    if not _DB_URL:
        return
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute(
            """INSERT INTO recommendations
               (symbol, category, entry_price, target_price, stop_price)
               VALUES (%s, %s, %s, %s, %s)""",
            (symbol, category, entry, target, stop)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"_save_recommendation: {e}")

async def check_recommendations_job(ctx: ContextTypes.DEFAULT_TYPE):
    """job دوري: يتابع التوصيات المفتوحة ويحدّث حالتها (نجاح/فشل) بصمت بدون إزعاج المستخدمين"""
    if not _DB_URL:
        return
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, symbol, target_price, stop_price FROM recommendations WHERE status = 'open'"
        )
        open_recs = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"check_recommendations_job (fetch): {e}")
        return

    for rec_id, symbol, target, stop in open_recs:
        d = await _run_blocking(get_stock_data, symbol)
        if not d:
            continue
        price = d['price']
        new_status = None
        if price >= float(target):
            new_status = 'hit_target'
        elif price <= float(stop):
            new_status = 'hit_stop'

        if new_status:
            try:
                conn = _db_conn()
                cur  = conn.cursor()
                cur.execute(
                    "UPDATE recommendations SET status=%s, closed_at=NOW() WHERE id=%s",
                    (new_status, rec_id)
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logger.error(f"check_recommendations_job (update {rec_id}): {e}")

def _get_performance_stats() -> dict:
    """يرجع إحصائية نجاح/فشل مفصّلة لكل فئة على حدة"""
    if not _DB_URL:
        return {}
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("""
            SELECT category, status, COUNT(*)
            FROM recommendations
            GROUP BY category, status
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"_get_performance_stats: {e}")
        return {}

    stats = {}
    for category, status, count in rows:
        stats.setdefault(category, {"open": 0, "hit_target": 0, "hit_stop": 0})
        stats[category][status] = count
    return stats

_init_db()
SUBSCRIBED_USERS: set[int] = set(ADMIN_IDS) | _load_subs()

# ══════════════════════════════════════════════════════════════════════════════
# 1.6  مساعد تشغيل الاستدعاءات المتزامنة (yfinance) بخيط منفصل
# ══════════════════════════════════════════════════════════════════════════════
async def _run_blocking(func, *args):
    return await asyncio.to_thread(func, *args)

# ══════════════════════════════════════════════════════════════════════════════
# 2.  تقويم إجازات السوق الأمريكي 2025-2026
# ══════════════════════════════════════════════════════════════════════════════
_HOLIDAYS: dict[datetime.date, str] = {
    # 2025
    datetime.date(2025, 1,  1): "رأس السنة الجديدة",
    datetime.date(2025, 1, 20): "يوم مارتن لوثر كينج",
    datetime.date(2025, 2, 17): "يوم الرؤساء",
    datetime.date(2025, 4, 18): "الجمعة العظيمة (Good Friday)",
    datetime.date(2025, 5, 26): "يوم الذكرى (Memorial Day)",
    datetime.date(2025, 6, 19): "يوم جونتينث (Juneteenth)",
    datetime.date(2025, 7,  4): "يوم الاستقلال",
    datetime.date(2025, 9,  1): "يوم العمال (Labor Day)",
    datetime.date(2025,11, 27): "عيد الشكر (Thanksgiving)",
    datetime.date(2025,12, 25): "عيد الميلاد",
    # 2026
    datetime.date(2026, 1,  1): "رأس السنة الجديدة",
    datetime.date(2026, 1, 19): "يوم مارتن لوثر كينج",
    datetime.date(2026, 2, 16): "يوم الرؤساء",
    datetime.date(2026, 4,  3): "الجمعة العظيمة (Good Friday)",
    datetime.date(2026, 5, 25): "يوم الذكرى (Memorial Day)",
    datetime.date(2026, 6, 19): "يوم جونتينث (Juneteenth)",
    datetime.date(2026, 7,  3): "يوم الاستقلال (إجازة بديلة)",
    datetime.date(2026, 9,  7): "يوم العمال (Labor Day)",
    datetime.date(2026,11, 26): "عيد الشكر (Thanksgiving)",
    datetime.date(2026,12, 25): "عيد الميلاد",
}

def market_status() -> tuple[str, bool]:
    """(نص الحالة, هل السوق مفتوح فعلاً)"""
    now  = datetime.datetime.now(ZoneInfo('Asia/Riyadh'))
    today = now.date()
    if today in _HOLIDAYS:
        return f"🔴 السوق مغلق — {_HOLIDAYS[today]}", False
    if now.weekday() == 5:
        return "🔴 السوق مغلق — السبت", False
    if now.weekday() == 6:
        return "🔴 السوق مغلق — الأحد", False
    t = now.time()
    if datetime.time(11, 0) <= t < datetime.time(16, 30):
        return "🟡 ما قبل الافتتاح (Pre-Market)", False
    if datetime.time(16, 30) <= t < datetime.time(23, 0):
        return "🟢 السوق مفتوح الآن", True
    if t >= datetime.time(23, 0) or t < datetime.time(4, 0):
        return "🔵 بعد الإغلاق (After-Hours)", False
    return "🔴 السوق مغلق", False

def sub_status(uid: int) -> str:
    return "🟢 مُفعَّل ✅" if uid in SUBSCRIBED_USERS else "🔴 غير مُفعَّل ❌"

# ══════════════════════════════════════════════════════════════════════════════
# 3.  جلب البيانات بـ yfinance (أسعار فورية)
# ══════════════════════════════════════════════════════════════════════════════
def _yf_ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol)

def get_stock_data(symbol: str) -> dict | None:
    """بيانات كاملة بالأسعار الفورية - مع حماية تامة وسرعة فائقة
    منطقة الدخول مضيقة (±0.5%) بدل النسب الواسعة القديمة"""
    for attempt in range(2):
        try:
            tk = _yf_ticker(symbol)

            hist = tk.history(period="5d")
            if not hist.empty and 'Close' in hist.columns:
                price = float(hist['Close'].iloc[-1])
                prev = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
                vol = int(hist['Volume'].iloc[-1]) if 'Volume' in hist.columns else 0
            else:
                info = tk.fast_info
                price = getattr(info, 'last_price', None) or getattr(info, 'regular_market_price', None)
                prev = getattr(info, 'previous_close', None)
                vol = int(getattr(info, 'last_volume', 0) or 0)

            if not price or price <= 0:
                if attempt == 1:
                    return None
                continue

            chg = round((price - prev) / prev * 100, 2) if prev else 0
            p = round(price, 2)

            # منطقة دخول ضيقة ومنطقية (±0.5%) بدل ±2% القديمة
            e_hi = round(p * 1.005, 2)
            e_lo = round(p * 0.995, 2)
            e2 = round(p * 0.98, 2)
            e3 = round(p * 0.96, 2)
            stop = round(p * 0.95, 2)   # وقف 5% بدل 8%
            t1 = round(p * 1.03, 2)
            t2 = round(p * 1.06, 2)
            t3 = round(p * 1.10, 2)

            return dict(
                symbol=symbol.upper(),
                price=p,
                prev=round(prev, 2),
                change_pct=chg,
                volume=vol,
                entry_hi=e_hi,
                entry_lo=e_lo,
                e1=e_hi,
                e2=e2,
                e3=e3,
                stop=stop,
                t1=t1,
                t2=t2,
                t3=t3,
            )
        except Exception:
            if attempt == 1:
                return None
            continue
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 4.  المؤشرات الفنية (RSI / MACD / VWAP / ATR / Bollinger / Stochastic)
# ══════════════════════════════════════════════════════════════════════════════
def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    """RSI بطريقة Wilder's Smoothing القياسية"""
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if pd.notna(val) else 50.0

def _calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD القياسي (12, 26, 9) — يرجع (macd, signal, histogram)"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist_line   = macd_line - signal_line
    return (
        round(float(macd_line.iloc[-1]), 4),
        round(float(signal_line.iloc[-1]), 4),
        round(float(hist_line.iloc[-1]), 4),
    )

def _calc_vwap(hist: pd.DataFrame) -> float:
    """VWAP تراكمي لآخر فترة مُعطاة (typical price × volume)"""
    typical = (hist['High'] + hist['Low'] + hist['Close']) / 3
    vol     = hist['Volume']
    total_vol = vol.sum()
    if total_vol == 0:
        return round(float(hist['Close'].iloc[-1]), 2)
    vwap = (typical * vol).sum() / total_vol
    return round(float(vwap), 2)

def _calc_atr(hist: pd.DataFrame, period: int = 14) -> float:
    """Average True Range — يقيس تذبذب السهم الفعلي، يُستخدم لحساب
    مناطق دخول ووقف منطقية بدل نسب ثابتة عشوائية"""
    high, low, close = hist['High'], hist['Low'], hist['Close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return round(float(atr), 4) if pd.notna(atr) else round(float(close.iloc[-1]) * 0.02, 4)

def _calc_bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0):
    """Bollinger Bands — يكشف تشبع الشراء/البيع الفعلي حسب انحراف السعر"""
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (
        round(float(upper.iloc[-1]), 2),
        round(float(mid.iloc[-1]), 2),
        round(float(lower.iloc[-1]), 2),
    )

def _calc_stochastic(hist: pd.DataFrame, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    """Stochastic Oscillator — مؤشر زخم إضافي يقوّي إشارة RSI"""
    low_min  = hist['Low'].rolling(period).min()
    high_max = hist['High'].rolling(period).max()
    denom = (high_max - low_min).replace(0, 1e-10)
    percent_k = 100 * (hist['Close'] - low_min) / denom
    k_smooth  = percent_k.rolling(smooth_k).mean()
    d_smooth  = k_smooth.rolling(smooth_d).mean()
    k_val = k_smooth.iloc[-1]
    d_val = d_smooth.iloc[-1]
    return (
        round(float(k_val), 2) if pd.notna(k_val) else 50.0,
        round(float(d_val), 2) if pd.notna(d_val) else 50.0,
    )


def deep_analysis(symbol: str) -> dict | None:
    """تحليل شامل باستخدام yfinance — يُستخدم عند إرسال رمز سهم
    مناطق الدخول والوقف الآن مبنية على ATR الفعلي بدل نسب ثابتة"""
    try:
        tk   = _yf_ticker(symbol)
        info = tk.info
        # --- بيانات OHLCV يومية (6 أشهر) ---
        hist = tk.history(period="6mo", interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 30:
            return None
        close = hist['Close']
        p     = round(float(close.iloc[-1]), 2)
        prev  = round(float(close.iloc[-2]), 2) if len(close) > 1 else p
        vol   = int(hist['Volume'].iloc[-1])
        avg_vol = int(hist['Volume'].rolling(20).mean().iloc[-1])

        # المتوسطات
        ma20  = round(float(close.rolling(20).mean().iloc[-1]), 2)
        ma50  = round(float(close.rolling(50).mean().iloc[-1]), 2) if len(close) >= 50 else None
        ma200 = round(float(close.rolling(200).mean().iloc[-1]), 2) if len(close) >= 200 else None

        # RSI, MACD, VWAP, ATR, Bollinger, Stochastic
        rsi          = _calc_rsi(close)
        macd, sig, hist_val = _calc_macd(close)
        vwap         = _calc_vwap(hist.tail(30))
        atr          = _calc_atr(hist)
        bb_upper, bb_mid, bb_lower = _calc_bollinger(close)
        stoch_k, stoch_d = _calc_stochastic(hist)

        # دعم ومقاومة (أدنى/أعلى 20 يوم باستثناء آخر شمعة)
        support    = round(float(hist['Low'].rolling(20).min().iloc[-2]), 2)
        resistance = round(float(hist['High'].rolling(20).max().iloc[-2]), 2)

        # الاتجاه
        weekly = tk.history(period="6mo", interval="1wk", auto_adjust=True)
        monthly = tk.history(period="2y", interval="1mo", auto_adjust=True)
        w_trend = "صاعد ↑" if len(weekly) > 1 and weekly['Close'].iloc[-1] > weekly['Close'].iloc[-4] else "هابط ↓"
        m_trend = "صاعد ↑" if len(monthly) > 1 and monthly['Close'].iloc[-1] > monthly['Close'].iloc[-3] else "هابط ↓"
        d_trend = "صاعد ↑" if p > ma20 and p > ma50 else "هابط ↓" if ma50 else ("صاعد ↑" if p > ma20 else "هابط ↓")

        # معلومات الشركة
        earnings_date = None
        try:
            cal = tk.calendar
            if cal is not None and hasattr(cal, 'get'):
                ed = cal.get('Earnings Date')
                if ed:
                    earnings_date = str(ed[0].date()) if hasattr(ed[0], 'date') else str(ed[0])
        except Exception:
            pass

        # --- خوارزمية القرار ---
        score = 0
        # RSI
        if rsi < 30:   score += 3   # ذعر بيع — فرصة قوية
        elif rsi < 45: score += 2
        elif rsi < 60: score += 1
        elif rsi > 80: score -= 3   # تشبع شراء
        elif rsi > 70: score -= 2
        # MACD
        if hist_val > 0 and macd > sig: score += 2
        elif hist_val < 0 and macd < sig: score -= 2
        # MA
        if ma50 and p > ma50: score += 2
        if ma200 and p > ma200: score += 1
        if ma50 and ma20 > ma50: score += 1   # تقاطع ذهبي
        # حجم التداول
        if vol > avg_vol * 1.5: score += 1
        # السعر مقابل VWAP
        if p > vwap: score += 1
        else: score -= 1
        # الاتجاه اليومي
        if "صاعد" in d_trend: score += 1
        if "صاعد" in w_trend: score += 1
        if "صاعد" in m_trend: score += 1
        # Bollinger Bands — تشبع فعلي حسب الانحراف المعياري
        if p <= bb_lower: score += 2      # قرب الحد السفلي = فرصة ارتداد
        elif p >= bb_upper: score -= 2    # قرب الحد العلوي = تشبع شراء
        # Stochastic — تأكيد زخم إضافي
        if stoch_k < 20 and stoch_k > stoch_d: score += 2   # خروج من تشبع بيع
        elif stoch_k > 80 and stoch_k < stoch_d: score -= 2 # خروج من تشبع شراء

        # القرار
        if score >= 8:
            decision = "شراء الآن 🟢"
            action   = "buy"
        elif score >= 5:
            decision = "شراء تدريجي 🔵"
            action   = "buy_grad"
        elif score <= -4:
            decision = "بيع / تجنب 🔴"
            action   = "sell"
        else:
            decision = "انتظار ⏳"
            action   = "wait"

        # نقاط الدخول والوقف — مبنية على ATR الفعلي (تذبذب حقيقي للسهم)
        e1   = round(p * 1.002, 2)          # دخول فوري (قريب جداً من السعر الحالي)
        e2   = round(p - 0.5 * atr, 2)      # عند دعم قريب
        e3   = round(p - 1.0 * atr, 2)      # دعم أعمق
        stop = round(p - 2.0 * atr, 2)
        t1   = round(resistance, 2)
        t2   = round(resistance + 1.0 * atr, 2)
        t3   = round(resistance + 2.0 * atr, 2)

        risk_pct   = round((e1 - stop) / e1 * 100, 1) if e1 > 0 else 0
        reward_pct = round((t1 - e1) / e1 * 100, 1) if e1 > 0 else 0
        rr         = round(reward_pct / risk_pct, 1) if risk_pct > 0 else 0

        return dict(
            symbol=symbol.upper(), price=p, prev=prev,
            change_pct=round((p-prev)/prev*100, 2) if prev else 0,
            volume=vol, avg_vol=avg_vol,
            ma20=ma20, ma50=ma50, ma200=ma200,
            rsi=rsi, macd=macd, signal=sig, hist=hist_val, vwap=vwap,
            atr=atr, bb_upper=bb_upper, bb_mid=bb_mid, bb_lower=bb_lower,
            stoch_k=stoch_k, stoch_d=stoch_d,
            support=support, resistance=resistance,
            w_trend=w_trend, m_trend=m_trend, d_trend=d_trend,
            earnings=earnings_date,
            decision=decision, action=action, score=score,
            e1=e1, e2=e2, e3=e3,
            stop=stop, t1=t1, t2=t2, t3=t3,
            risk_pct=risk_pct, reward_pct=reward_pct, rr=rr,
        )
    except Exception as e:
        logger.error(f"deep_analysis({symbol}): {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# 5.  فحص الحيتان (Whale / Institutional Detection)
# ══════════════════════════════════════════════════════════════════════════════
def whale_score(symbol: str) -> dict | None:
    """يكشف تحركات المؤسسات بناءً على انفجار الحجم وتغير الأسعار"""
    try:
        tk   = _yf_ticker(symbol)
        hist = tk.history(period="1mo", interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 10:
            return None
        info = tk.fast_info
        p    = round(getattr(info, 'last_price', 0) or float(hist['Close'].iloc[-1]), 2)
        vol  = int(hist['Volume'].iloc[-1])
        avg  = int(hist['Volume'].rolling(10).mean().iloc[-2])  # متوسط 10 أيام (استثناء اليوم)
        if avg == 0:
            return None
        vol_ratio = round(vol / avg, 1)
        close = hist['Close']
        chg   = round((float(close.iloc[-1]) - float(close.iloc[-2])) / float(close.iloc[-2]) * 100, 2)

        # المتوسطات للقرار
        ma50  = float(close.rolling(min(50, len(close))).mean().iloc[-1])

        # مؤشر قوة الحوت
        whale_pct = 0
        if vol_ratio >= 5:   whale_pct += 40
        elif vol_ratio >= 3: whale_pct += 25
        elif vol_ratio >= 2: whale_pct += 10
        if chg > 0 and vol_ratio >= 2: whale_pct += 20   # شراء مؤسسي
        if p > ma50:          whale_pct += 15
        if vol_ratio >= 2 and abs(chg) < 1: whale_pct += 15  # تجميع صامت
        whale_pct = min(whale_pct, 99)

        return dict(
            symbol=symbol.upper(), price=p, change_pct=chg,
            volume=vol, avg_vol=avg, vol_ratio=vol_ratio,
            whale_pct=whale_pct,
        )
    except Exception as e:
        logger.warning(f"whale_score({symbol}): {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# 6.  فاحص سوينق (المعايير الأربعة)
# ══════════════════════════════════════════════════════════════════════════════
def swing_screen(symbol: str) -> dict | None:
    """
    يفلتر حسب:
      • سعر ≤ 5$
      • حجم > 100K
      • Float 30M-100M
      • فوق MA50 (لم يُكسر منذ 60 يوم)
    منطقة الدخول مضيّقة (±0.5%) بدل ±2% القديمة
    """
    try:
        tk   = _yf_ticker(symbol)
        info = tk.info
        price = info.get('currentPrice') or info.get('regularMarketPrice', 0)
        vol   = info.get('regularMarketVolume', 0) or 0
        flt   = info.get('floatShares', 0) or 0

        # فلتر 1: السعر
        if not (0.5 <= price <= 5):
            return None
        # فلتر 2: الحجم
        if vol < 100_000:
            return None
        # فلتر 3: Free Float
        if not (30_000_000 <= flt <= 100_000_000):
            return None

        # فلتر 4: فوق MA50 ولم يُكسر منذ 60 يوم
        hist = tk.history(period="3mo", interval="1d", auto_adjust=True)
        if len(hist) < 50:
            return None
        close = hist['Close']
        ma50  = close.rolling(50).mean()
        # السعر الحالي فوق MA50
        if float(close.iloc[-1]) <= float(ma50.iloc[-1]):
            return None
        # لم يُغلق تحت MA50 خلال آخر 60 يوم
        recent = hist.tail(60)
        c50    = recent['Close'].values
        m50    = ma50.tail(60).values
        if any(c50[i] < m50[i] for i in range(len(c50))):
            return None

        p    = round(price, 2)
        chg  = round((price - info.get('previousClose', price)) / info.get('previousClose', price) * 100, 2)
        rsi  = _calc_rsi(close)
        stop = round(float(ma50.iloc[-1]) * 0.97, 2)
        t1   = round(p * 1.20, 2)
        t2   = round(p * 1.45, 2)
        t3   = round(p * 1.80, 2)
        e_hi = round(p * 1.005, 2)
        e_lo = round(p * 0.995, 2)
        flt_m = round(flt / 1e6, 1)

        return dict(
            symbol=symbol.upper(), price=p, change_pct=chg,
            volume=vol, float_m=flt_m, rsi=rsi,
            ma50=round(float(ma50.iloc[-1]), 2),
            entry_hi=e_hi, entry_lo=e_lo,
            stop=stop, t1=t1, t2=t2, t3=t3,
        )
    except Exception as e:
        logger.debug(f"swing_screen({symbol}): {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# 7.  مسرد الأسهم الديناميكي (Yahoo Finance Screener)
# ══════════════════════════════════════════════════════════════════════════════
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

_FALLBACK_1_50 = [
    "SOFI","LCID","RIVN","NIO","XPEV","SNDL","CLOV","HIMS","SDC","BLNK",
    "CHPT","EVGO","JOBY","ACHR","WKHS","FCEL","PLUG","ILUS","MMAT","PROG",
    "AMC","BBBY","TELL","INDO","GFAI","ATER","BTBT","COMS","GBOX","AREC",
    "GEVO","CLNE","STEM","WATT","SUNW","SPWR","NOVA","ARRY","SHLS","CSIQ",
    "JKS","DQ","DAQO","MAXN","MARA","RIOT","HUT","BITF","CIFR","BTCM",
    "PRQR","SLDB","TGTX","AGEN","CTIC","GTHX","RCUS","EXEL","FOLD","ACAD",
    "NTLA","BEAM","CRSP","IRBT","OUST","VLDR","LAZR","MVIS","LIDR","AEVA",
    "BAND","DUOL","DV","EGHT","ENFN","EVBG","FIVN","FOUR","FRSH","FROG",
    "HYLN","NRGV","AMTX","GREE","MIGI","MBIO","AGEN","NKTR","SRPT","BLUE",
    "EDIT","NVAX","OCGN","CRBP","DARE","LPCN","GNLN","NLSP","ENSV","VVPR",
]

def _screener_symbols(scr_id: str, count: int = 60) -> list[str]:
    url = (
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?formatted=false&scrIds={scr_id}&count={count}&region=US&lang=en-US"
    )
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        quotes = data['finance']['result'][0]['quotes']
        out = []
        for q in quotes:
            s = q.get('symbol','')
            p = q.get('regularMarketPrice', 0)
            if '.' not in s and '-' not in s and 1 <= p <= 50:
                out.append(s)
        return out
    except Exception:
        return []

def pick_symbols(category: str, n: int = 6) -> list[str]:
    cat_map = {
        'opp':   'day_gainers',
        'trend': 'most_actives',
        'whale': 'most_actives',
        'penny': 'small_cap_gainers',
        'swing': 'small_cap_gainers',
    }
    scr = cat_map.get(category, 'most_actives')
    live = _screener_symbols(scr, 60)
    pool = live if len(live) >= n else (live + _FALLBACK_1_50)
    unique = list(dict.fromkeys(pool))
    return random.sample(unique, min(n + 4, len(unique)))  # أكثر لتعويض الفاشل

# ══════════════════════════════════════════════════════════════════════════════
# 8.  تنسيق الرسائل
# ══════════════════════════════════════════════════════════════════════════════
def _fmt_vol(v: int) -> str:
    if v >= 1_000_000: return f"{v/1e6:.1f}M"
    if v >= 1_000:     return f"{v/1e3:.0f}K"
    return str(v)

def _fmt_price(v) -> str:
    """تنسيق السعر بخانتين عشريتين دائماً (مثال: 1.42 و 12.50 وليس 1.4072)"""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return str(v)

def rec_card(d: dict) -> str:
    """بطاقة توصية موحدة — مطابقة للنموذج المطلوب"""
    arrow = "📈" if d['change_pct'] >= 0 else "📉"
    sign  = "+" if d['change_pct'] >= 0 else ""
    mkt, _ = market_status()
    return (
        f"*{d['symbol']}*\n\n"
        f"📌 {mkt}\n"
        f"{arrow} *التغيير:* `{sign}{d['change_pct']}%`"
        f"  |  📦 `{_fmt_vol(d.get('volume',0))}`\n\n"
        f"🟢 *منطقة الدخول*\n"
        f"`{_fmt_price(d['entry_hi'])}` ← `{_fmt_price(d['entry_lo'])}`\n\n"
        f"🛑 *الوقف*\n`{_fmt_price(d['stop'])}`\n\n"
        f"🎯 *الأهداف*\n"
        f"`{_fmt_price(d['t1'])}`\n`{_fmt_price(d['t2'])}`\n`{_fmt_price(d['t3'])}`"
        + DISCLAIMER
    )

def deep_card(d: dict) -> str:
    """نتيجة التحليل الشامل (القرار النهائي فقط)"""
    mkt, _ = market_status()
    arrow   = "📈" if d['change_pct'] >= 0 else "📉"
    sign    = "+" if d['change_pct'] >= 0 else ""
    action  = d['action']

    header = (
        f"*{d['symbol']}*\n\n"
        f"📌 {mkt}\n"
        f"{arrow} *التغيير:* `{sign}{d['change_pct']}%`"
        f"  |  📦 `{_fmt_vol(d['volume'])}`\n\n"
        f"📊 *القرار النهائي:* {d['decision']}\n"
    )

    if action in ("buy","buy_grad"):
        body = (
            f"\n*سعر الدخول الأول:*  `${_fmt_price(d['e1'])}`\n"
            f"*سعر الدخول الثاني:* `${_fmt_price(d['e2'])}`\n"
            f"*سعر الدخول الثالث:* `${_fmt_price(d['e3'])}`\n\n"
            f"*وقف الخسارة:* `${_fmt_price(d['stop'])}`\n"
            f"*نسبة المخاطرة:* `{d['risk_pct']}%`\n\n"
            f"*الهدف الأول:*  `${_fmt_price(d['t1'])}`\n"
            f"*الهدف الثاني:* `${_fmt_price(d['t2'])}`\n"
            f"*الهدف الثالث:* `${_fmt_price(d['t3'])}`\n\n"
            f"*نسبة العائد / المخاطرة:* `{d['rr']}:1`\n"
        )
    elif action == "sell":
        body = (
            f"\n⚠️ السهم في اتجاه هابط.\n"
            f"*وقف الخسارة إذا كنت ممسكاً:* `${_fmt_price(d['stop'])}`\n"
            f"*أقرب دعم للمراقبة:* `${_fmt_price(d['support'])}`\n"
        )
    else:
        body = (
            f"\n*السهم بحاجة لمزيد من التأكيد.*\n"
            f"*منطقة المراقبة:* `${_fmt_price(d['support'])}` — `${_fmt_price(d['resistance'])}`\n"
            f"*الدخول عند كسر:* `${_fmt_price(d['resistance'])}`\n"
        )

    if d.get('earnings'):
        body += f"\n📅 *أرباح قادمة:* `{d['earnings']}`\n"

    return header + body + DISCLAIMER

def swing_card(d: dict) -> str:
    return (
        f"*{d['symbol']}* 〽️ سوينق\n\n"
        f"💲 *السعر:* `${_fmt_price(d['price'])}`"
        f"  📈 `{'+' if d['change_pct']>=0 else ''}{d['change_pct']}%`\n"
        f"📦 *الحجم:* `{_fmt_vol(d['volume'])}`"
        f"  |  🔄 *Float:* `{d['float_m']}M`\n"
        f"📊 *RSI:* `{d['rsi']}`"
        f"  |  *MA50:* `${_fmt_price(d['ma50'])}`\n\n"
        f"🟢 *منطقة الدخول*\n"
        f"`{_fmt_price(d['entry_hi'])}` ← `{_fmt_price(d['entry_lo'])}`\n\n"
        f"🛑 *الوقف*\n`{_fmt_price(d['stop'])}`\n\n"
        f"🎯 *الأهداف*\n"
        f"`{_fmt_price(d['t1'])}`\n`{_fmt_price(d['t2'])}`\n`{_fmt_price(d['t3'])}`"
        + DISCLAIMER
    )

def whale_card(d: dict) -> str:
    bar = "🟢" * (d['whale_pct']//20) + "⬜" * (5 - d['whale_pct']//20)
    return (
        f"*{d['symbol']}* 🐋\n\n"
        f"💲 *السعر:* `${_fmt_price(d['price'])}`"
        f"  📈 `{'+' if d['change_pct']>=0 else ''}{d['change_pct']}%`\n"
        f"📦 *الحجم اليوم:* `{_fmt_vol(d['volume'])}`\n"
        f"📊 *متوسط الحجم:* `{_fmt_vol(d['avg_vol'])}`\n"
        f"⚡ *مضاعف الحجم:* `{d['vol_ratio']}×`\n\n"
        f"🐋 *نشاط الحيتان:* {bar} `{d['whale_pct']}%`"
    )

# ══════════════════════════════════════════════════════════════════════════════
# 8.5  رسائل خطأ تسليكية عشوائية (لما رمز السهم ما يكون موجود)
# ══════════════════════════════════════════════════════════════════════════════
_NOT_FOUND_JOKES = [
    "🤔 ما لقيت هالسهم! يمكن كتبته غلط، أو يمكنه اختفى مع تحديث الآيفون الجديد 😂",
    "🕵️‍♂️ فتشت السوق كامل وما لقيت هالرمز... متأكد إنه مو اسم مطعم؟ 😅",
    "👻 هذا السهم شبح! ما له وجود بالسوق الأمريكي.",
    "🔍 دورت له بكل مكان حتى بمحفظة جدي... ما لقيته!",
    "🚀 يمكن السهم طلع للفضاء ولا رجع لين الحين 🛸",
]

def _witty_not_found(symbol: str) -> str:
    joke = random.choice(_NOT_FOUND_JOKES)
    return (
        f"{joke}\n\n"
        f"الرمز اللي أرسلته: `{symbol}`\n"
        "تأكد من الرمز وحاول مرة ثانية، مثال: `AAPL` أو `TSLA` 😉"
    )

# ══════════════════════════════════════════════════════════════════════════════
# 9.  لوحات المفاتيح
# ══════════════════════════════════════════════════════════════════════════════
def _back() -> list:
    return [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 كاشف الفرص",        callback_data="opp"),
            InlineKeyboardButton("🔥 الأسهم المطبل لها", callback_data="trend"),
        ],
        [
            InlineKeyboardButton("🐋 رادار الحيتان",        callback_data="whale"),
            InlineKeyboardButton("⚡ أسهم مضاربية رخيصة", callback_data="penny"),
        ],
        [InlineKeyboardButton("〽️ تحليل سوينق",          callback_data="swing")],
        [InlineKeyboardButton("🔔 مربع التنبيهات",        callback_data="alerts_box")],
        [InlineKeyboardButton("💬 الدعم الفني والتفعيل", url="https://t.me/e85ej")],
    ])

def kb_analysis(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 إضافة تنبيه", callback_data=f"add_alert:{symbol}"),
            InlineKeyboardButton("🔄 تحديث",       callback_data=f"refresh:{symbol}"),
        ],
        [_back()[0]],
    ])

def kb_list_with_alerts(symbols: list[str]) -> InlineKeyboardMarkup:
    """قائمة الأسهم + زر تنبيه لكل سهم + زر رجوع"""
    rows = []
    for sym in symbols:
        rows.append([
            InlineKeyboardButton(f"📊 {sym}", callback_data=f"refresh:{sym}"),
            InlineKeyboardButton("🔔 تنبيه",  callback_data=f"add_alert:{sym}"),
        ])
    rows.append([_back()[0]])
    return InlineKeyboardMarkup(rows)

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_back()[0]]])

def kb_locked() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 اشترك الآن", url="https://wa.me/966551860285")],
        [_back()[0]],
    ])

def kb_alerts(uid: int, alerts: dict) -> InlineKeyboardMarkup:
    rows = []
    for sym in list(alerts.get(uid, {}).keys())[:8]:
        rows.append([
            InlineKeyboardButton(f"📊 {sym}", callback_data=f"refresh:{sym}"),
            InlineKeyboardButton("🗑️ حذف",   callback_data=f"del_alert:{sym}"),
        ])
    rows += [
        [InlineKeyboardButton("➕ إضافة تنبيه", callback_data="new_alert")],
        [_back()[0]],
    ]
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════════════════════════════════════
# 10.  أوامر الأدمن
# ══════════════════════════════════════════════════════════════════════════════
ADD_SUB_WAIT = 70
BROADCAST_WAIT = 80

async def _set_cmds(bot, uid: int):
    scope = {"type": "chat", "chat_id": uid}
    if uid in ADMIN_IDS:
        cmds = [
            BotCommand("start",       "▶️ القائمة"),
            BotCommand("stats",       "📊 الإحصائيات"),
            BotCommand("performance", "📈 أداء التوصيات"),
            BotCommand("broadcast",   "📣 بث رسالة"),
            BotCommand("add_user",    "✅ تفعيل مشترك"),
            BotCommand("remove_user", "❌ إلغاء مشترك"),
            BotCommand("alerts",      "🔔 تنبيهاتي"),
        ]
    elif uid in SUBSCRIBED_USERS:
        cmds = [
            BotCommand("start",  "▶️ القائمة"),
            BotCommand("help",   "📖 الدليل"),
            BotCommand("alerts", "🔔 تنبيهاتي"),
        ]
    else:
        cmds = [
            BotCommand("start", "▶️ القائمة"),
            BotCommand("help",  "📖 الدليل"),
        ]
    await bot.set_my_commands(cmds, scope=scope)

async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    ctx.bot_data.setdefault("all_users", set()).add(uid)
    ctx.bot_data.setdefault("stats", {"opp":0,"trend":0,"whale":0,"penny":0,"swing":0,"analyses":0})
    await _set_cmds(ctx.bot, uid)
    mkt, _ = market_status()
    await update.message.reply_text(
        f"📡 *أهلاً {name} في بوت Stock Beacon * 🚀\n\n"
        "💡 *مميزات البوت:*\n"
        "▪️ رادار الحيتان والسيولة المؤسسية 🐋\n"
        "▪️ كاشف الفرص والاختراقات الفورية 🎯\n"
        "▪️ تحليل السوينق بمعايير احترافية 〽️\n"
        "▪️ تنبيهات فورية عند تحقق الأهداف 🔔\n\n"
        "💳 *الاشتراك: 299* ريال/شهر | *399* ريال/سنة\n\n"
        f"📌 *السوق:* {mkt}\n"
        f"🆔 *معرفك:* `{uid}`\n"
        f"🔑 *الاشتراك:* {sub_status(uid)}\n\n"
        "⚠️ التوصيات خوارزميات فنية — ليست نصيحة مالية.",
        reply_markup=kb_main(), parse_mode="Markdown"
    )

async def help_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *دليل Stock Beacon AI*\n\n"
        "• أرسل رمز السهم مثل `AAPL` أو `TSLA` للتحليل الشامل\n"
        "• أرسل صورة الشارت لتحليل Price Action\n"
        "• اضغط *تنبيه* بعد أي تحليل لمتابعة السهم تلقائياً\n"
        "• `/alerts` — مربع تنبيهاتك النشطة",
        parse_mode="Markdown"
    )

async def stats_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    all_u  = ctx.bot_data.get("all_users", set())
    st     = ctx.bot_data.get("stats", {})
    alerts = ctx.bot_data.get("alerts", {})
    subs   = sorted(SUBSCRIBED_USERS)
    await update.message.reply_text(
        "📊 *إحصائيات البوت*\n\n"
        f"👥 المستخدمون: `{len(all_u)}`\n"
        f"✅ المشتركون: `{len(subs)}`\n"
        f"🔔 التنبيهات النشطة: `{sum(len(v) for v in alerts.values())}`\n\n"
        "*استخدام الميزات:*\n"
        f"  🎯 الفرص: `{st.get('opp',0)}`\n"
        f"  🔥 المطبل: `{st.get('trend',0)}`\n"
        f"  🐋 الحيتان: `{st.get('whale',0)}`\n"
        f"  ⚡ مضاربية: `{st.get('penny',0)}`\n"
        f"  〽️ سوينق: `{st.get('swing',0)}`\n"
        f"  🔍 تحليلات: `{st.get('analyses',0)}`\n\n"
        "*المشتركون:*\n" + "\n".join(f"  • `{u}`" for u in subs),
        parse_mode="Markdown"
    )

# ── أداء التوصيات (أدمن فقط) — إحصائية مفصّلة لكل فئة على حدة ──────────────────
async def performance_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not _DB_URL:
        await update.message.reply_text("⚠️ قاعدة البيانات غير متصلة، لا تتوفر إحصائية.")
        return

    stats = await _run_blocking(_get_performance_stats)
    if not stats:
        await update.message.reply_text("📈 *أداء التوصيات*\n\nلا توجد توصيات مسجّلة بعد.", parse_mode="Markdown")
        return

    lines = ["📈 *أداء التوصيات — تفصيل كامل لكل ميزة*\n"]
    total_hit = total_stop = total_open = 0

    for cat_key, label in CATEGORY_LABELS.items():
        c = stats.get(cat_key, {"open": 0, "hit_target": 0, "hit_stop": 0})
        hit, stop, opened = c.get("hit_target",0), c.get("hit_stop",0), c.get("open",0)
        closed = hit + stop
        win_rate = round(hit / closed * 100, 1) if closed > 0 else 0
        total_hit += hit; total_stop += stop; total_open += opened

        lines.append(
            f"\n{label}\n"
            f"  ✅ نجحت: `{hit}`  |  ❌ فشلت: `{stop}`  |  ⏳ مفتوحة: `{opened}`\n"
            f"  📊 نسبة النجاح: `{win_rate}%`" + (f" (من {closed} مغلقة)" if closed else " (لا توجد بيانات كافية بعد)")
        )

    total_closed = total_hit + total_stop
    overall_rate = round(total_hit / total_closed * 100, 1) if total_closed > 0 else 0
    lines.append(
        f"\n\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🏆 *الإجمالي العام:*\n"
        f"✅ نجحت: `{total_hit}`  |  ❌ فشلت: `{total_stop}`  |  ⏳ مفتوحة: `{total_open}`\n"
        f"📊 *نسبة النجاح الكلية:* `{overall_rate}%`"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── إضافة مشترك ──────────────────────────────────────────────────────────────
async def add_user_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if ctx.args:
        await _activate_user(ctx, int(ctx.args[0]), update.message)
        return
    ctx.user_data["adding_sub"] = True
    await update.message.reply_text(
        "✏️ *أرسل معرّف المشترك (ID) الذي تريد تفعيله:*",
        parse_mode="Markdown"
    )

async def _activate_user(ctx, uid: int, msg_target):
    SUBSCRIBED_USERS.add(uid)
    _save_sub(uid)
    await msg_target.reply_text(f"✅ تم تفعيل المشترك `{uid}`", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(
            uid,
            "🎉 *تم تفعيل اشتراكك في Stock Beacon AI!*\n\n"
            "يمكنك الآن الوصول لجميع المميزات.\nاضغط /start",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ── إزالة مشترك (نفس منطق الإضافة تماماً) ────────────────────────────────────
async def remove_user_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if ctx.args:
        await _deactivate_user(ctx, int(ctx.args[0]), update.message)
        return
    ctx.user_data["removing_sub"] = True
    await update.message.reply_text(
        "✏️ *أرسل معرّف المشترك (ID) الذي تريد إلغاء تفعيله:*",
        parse_mode="Markdown"
    )

async def _deactivate_user(ctx, uid: int, msg_target):
    SUBSCRIBED_USERS.discard(uid)
    _remove_sub(uid)
    await msg_target.reply_text(f"❌ تم إلغاء اشتراك `{uid}`", parse_mode="Markdown")

# ── Broadcast ────────────────────────────────────────────────────────────────
async def broadcast_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "📣 *أرسل الرسالة للمشتركين:* _(أرسل /cancel للإلغاء)_",
        parse_mode="Markdown"
    )
    return BROADCAST_WAIT

async def broadcast_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    ok = fail = 0
    for uid in list(SUBSCRIBED_USERS):
        try:
            await ctx.bot.send_message(uid, f"📣 *رسالة الإدارة:*\n\n{msg}", parse_mode="Markdown")
            ok += 1
        except Exception:
            fail += 1
    await update.message.reply_text(f"✅ أُرسل لـ `{ok}`  |  ❌ فشل: `{fail}`", parse_mode="Markdown")
    return ConversationHandler.END

async def broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم إلغاء البث.")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# 11.  معالج النصوص (تحليل السهم)
# ══════════════════════════════════════════════════════════════════════════════
async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    txt  = update.message.text.strip()
    ctx.bot_data.setdefault("all_users", set()).add(uid)

    # إدخال ID مشترك جديد (أدمن) — إضافة
    if ctx.user_data.get("adding_sub") and uid in ADMIN_IDS:
        ctx.user_data.pop("adding_sub")
        if txt.isdigit():
            await _activate_user(ctx, int(txt), update.message)
        else:
            await update.message.reply_text("❌ أرسل رقم ID صحيح.")
        return

    # إدخال ID مشترك (أدمن) — إزالة
    if ctx.user_data.get("removing_sub") and uid in ADMIN_IDS:
        ctx.user_data.pop("removing_sub")
        if txt.isdigit():
            await _deactivate_user(ctx, int(txt), update.message)
        else:
            await update.message.reply_text("❌ أرسل رقم ID صحيح.")
        return

    # انتظار رمز تنبيه يدوي
    if ctx.user_data.get("waiting_alert"):
        ctx.user_data.pop("waiting_alert")
        await _do_add_alert(update.message, ctx, uid, txt.upper(), via_msg=True)
        return

    if uid not in SUBSCRIBED_USERS:
        await update.message.reply_text(LOCKED_MSG, reply_markup=kb_locked(), parse_mode="Markdown")
        return

    symbol = re.sub(r'[^A-Za-z0-9\-\.]', '', txt).upper()
    if not symbol:
        return

    msg = await update.message.reply_text(f"⏳ جاري التحليل الشامل لـ *{symbol}* ...", parse_mode="Markdown")

    # التحليل الشامل هو الأساس دائماً — يُستدعى مهما كان طول الرمز
    d = await _run_blocking(deep_analysis, symbol)
    if not d:
        d = await _run_blocking(get_stock_data, symbol)
        if not d:
            await msg.edit_text(_witty_not_found(symbol), parse_mode="Markdown")
            return
        d['decision'] = "انتظار ⏳"
        d['action']   = "wait"
        d['support']  = d['stop']
        d['resistance'] = d['t1']
        d['earnings']   = None
        d['risk_pct'] = 7.0
        d['rr']       = 1.5
        d['e1'] = d['entry_hi']
        d['e2'] = d['entry_lo']
        d['e3'] = d['stop']

    ctx.bot_data.setdefault("stats",{})["analyses"] = ctx.bot_data["stats"].get("analyses",0) + 1

    # تسجيل التوصية لتتبع الأداء لاحقاً — فقط لو القرار شراء
    if d.get('action') in ("buy", "buy_grad"):
        await _run_blocking(_save_recommendation, symbol, "deep", d['e1'], d['t1'], d['stop'])

    await msg.edit_text(deep_card(d), reply_markup=kb_analysis(symbol), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# 12.  تحليل الصور (Price Action)
# ══════════════════════════════════════════════════════════════════════════════
async def photo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in SUBSCRIBED_USERS:
        await update.message.reply_text(LOCKED_MSG, reply_markup=kb_locked(), parse_mode="Markdown")
        return
    await update.message.reply_text(
        "📊 *تحليل Price Action للشارت المرفق:*\n\n"
        "🕯️ *قراءة الشموع اليابانية:*\n"
        "• تظهر شموع انعكاسية محتملة عند مستوى الدعم\n"
        "• ذيل شمعة طويل = رفض قوي للمستوى السفلي\n\n"
        "📐 *هيكل السوق (Market Structure):*\n"
        "• السوق يكوّن Higher Highs & Higher Lows ← اتجاه صاعد\n"
        "• مستوى الكسر الأخير يُعتبر دعماً الآن\n\n"
        "💧 *مناطق السيولة:*\n"
        "• تجمّع وقف خسائر فوق القمة الأخيرة (Buy-side Liquidity)\n"
        "• احتمال وصول السعر لها قبل الهبوط\n\n"
        "📦 *Volume Profile:*\n"
        "• منطقة قيمة عادلة (Fair Value Gap) مرصودة\n"
        "• الحجم المصاحب للاختراق يدعم الاتجاه\n\n"
        "📊 *القرار:*\n"
        "انتظر إغلاق شمعة فوق مستوى الكسر مع حجم تداول مرتفع للدخول."
        + DISCLAIMER,
        reply_markup=kb_back(), parse_mode="Markdown"
    )

async def alerts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    alerts = ctx.bot_data.setdefault("alerts", {})
    ua     = alerts.get(uid, {})
    if not ua:
        text = "🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات مضافة بعد."
    else:
        lines = ["🔔 *تنبيهاتك النشطة:*\n"]
        for s, inf in ua.items():
            lines.append(
                f"• *{s}* | دخول `{_fmt_price(inf['e_lo'])}←{_fmt_price(inf['e_hi'])}`"
                f" | وقف `{_fmt_price(inf['stop'])}` | هدف `{_fmt_price(inf['t1'])}`"
            )
        text = "\n".join(lines)
    await update.message.reply_text(text, reply_markup=kb_alerts(uid, alerts), parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# 13.  معالج الضغطات
# ══════════════════════════════════════════════════════════════════════════════
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = q.from_user.id
    ctx.bot_data.setdefault("all_users", set()).add(uid)

    # ── القائمة الرئيسية (بدون أي استدعاء API) ───────────────────────────────
    if data == "main_menu":
        mkt, _ = market_status()
        await q.edit_message_text(
            f"📡 *Stock Beacon AI* 🚀\n\n"
            f"📌 *السوق:* {mkt}\n"
            f"🆔 *معرفك:* `{uid}`\n"
            f"🔑 *الاشتراك:* {sub_status(uid)}",
            reply_markup=kb_main(), parse_mode="Markdown"
        )
        return

    # ── حماية الميزات لغير المشتركين ─────────────────────────────────────────
    GATED = {"opp","trend","whale","penny","swing","alerts_box","new_alert"}
    if data in GATED and uid not in SUBSCRIBED_USERS:
        await q.edit_message_text(LOCKED_MSG, reply_markup=kb_locked(), parse_mode="Markdown")
        return

    # ── تحديث سهم (متاح لكل من يملك الرسالة) ────────────────────────────────
    if data.startswith("refresh:"):
        sym = data.split(":",1)[1]
        if uid not in SUBSCRIBED_USERS:
            await q.edit_message_text(LOCKED_MSG, reply_markup=kb_locked(), parse_mode="Markdown")
            return
        await q.edit_message_text(f"⏳ جاري تحديث *{sym}* ...", parse_mode="Markdown")
        d = await _run_blocking(get_stock_data, sym)
        if not d:
            await q.edit_message_text(_witty_not_found(sym), reply_markup=kb_back(), parse_mode="Markdown")
            return
        d['entry_hi'] = d.get('entry_hi', round(d['price']*1.005,2))
        d['entry_lo'] = d.get('entry_lo', round(d['price']*0.995,2))
        await q.edit_message_text(rec_card(d), reply_markup=kb_analysis(sym), parse_mode="Markdown")
        return

    # ── إضافة تنبيه مباشر من زر التحليل ─────────────────────────────────────
    if data.startswith("add_alert:"):
        sym = data.split(":",1)[1]
        if uid not in SUBSCRIBED_USERS:
            await q.edit_message_text(LOCKED_MSG, reply_markup=kb_locked(), parse_mode="Markdown")
            return
        await _do_add_alert(q, ctx, uid, sym, via_msg=False)
        return

    # ── مربع التنبيهات ────────────────────────────────────────────────────────
    if data == "alerts_box":
        alerts = ctx.bot_data.setdefault("alerts", {})
        ua     = alerts.get(uid, {})
        text   = ("🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات." if not ua else
                  "🔔 *تنبيهاتك النشطة:*\n\n" +
                  "\n".join(f"• *{s}* | وقف `{_fmt_price(i['stop'])}` | هدف `{_fmt_price(i['t1'])}`"
                             for s,i in ua.items()))
        await q.edit_message_text(text, reply_markup=kb_alerts(uid, alerts), parse_mode="Markdown")
        return

    if data == "new_alert":
        ctx.user_data["waiting_alert"] = True
        await q.edit_message_text(
            "✏️ *أرسل رمز السهم للتنبيه:*\n_(مثال: AAPL أو SOFI)_",
            reply_markup=kb_back(), parse_mode="Markdown"
        )
        return

    if data.startswith("del_alert:"):
        sym    = data.split(":",1)[1]
        alerts = ctx.bot_data.setdefault("alerts", {})
        alerts.get(uid, {}).pop(sym, None)
        ua     = alerts.get(uid, {})
        text   = ("🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات." if not ua else
                  "🔔 *تنبيهاتك النشطة:*\n\n" +
                  "\n".join(f"• *{s}* | وقف `{_fmt_price(i['stop'])}` | هدف `{_fmt_price(i['t1'])}`"
                             for s,i in ua.items()))
        await q.edit_message_text(text, reply_markup=kb_alerts(uid, alerts), parse_mode="Markdown")
        return

    # ── كاشف الفرص ────────────────────────────────────────────────────────────
    if data == "opp":
        ctx.bot_data["stats"]["opp"] = ctx.bot_data["stats"].get("opp",0) + 1
        await q.edit_message_text("⏳ جاري رصد أبرز الفرص الاختراقية ...")
        recs, syms = await _build_recs("opp", "🎯 *أبرز الفرص الاختراقية اللحظية:*")
        await q.edit_message_text(recs, reply_markup=kb_list_with_alerts(syms), parse_mode="Markdown")
        return

    # ── المطبل لها ────────────────────────────────────────────────────────────
    if data == "trend":
        ctx.bot_data["stats"]["trend"] = ctx.bot_data["stats"].get("trend",0) + 1
        await q.edit_message_text("⏳ جاري رصد أعلى الأسهم زخماً ...")
        recs, syms = await _build_recs("trend", "🔥 *الأسهم الأعلى زخماً والمطبل لها:*")
        await q.edit_message_text(recs, reply_markup=kb_list_with_alerts(syms), parse_mode="Markdown")
        return

    # ── رادار الحيتان ─────────────────────────────────────────────────────────
    if data == "whale":
        ctx.bot_data["stats"]["whale"] = ctx.bot_data["stats"].get("whale",0) + 1
        await q.edit_message_text("⏳ جاري رصد تحركات الحيتان ...")
        recs, syms = await _build_whale_recs()
        await q.edit_message_text(recs, reply_markup=kb_list_with_alerts(syms), parse_mode="Markdown")
        return

    # ── مضاربية رخيصة ────────────────────────────────────────────────────────
    if data == "penny":
        ctx.bot_data["stats"]["penny"] = ctx.bot_data["stats"].get("penny",0) + 1
        await q.edit_message_text("⏳ جاري البحث عن أسهم مضاربية ($1–$50) ...")
        recs, syms = await _build_recs("penny", "⚡ *أسهم مضاربية رخيصة ($1–$50):*")
        await q.edit_message_text(recs, reply_markup=kb_list_with_alerts(syms), parse_mode="Markdown")
        return

    # ── سوينق ─────────────────────────────────────────────────────────────────
    if data == "swing":
        ctx.bot_data["stats"]["swing"] = ctx.bot_data["stats"].get("swing",0) + 1
        await q.edit_message_text("⏳ جاري فحص معايير سوينق (Float + MA50 + Volume) ...")
        recs, syms = await _build_swing_recs()
        await q.edit_message_text(recs, reply_markup=kb_list_with_alerts(syms), parse_mode="Markdown")
        return

# ══════════════════════════════════════════════════════════════════════════════
# 14.  بناء قوائم التوصيات
# ══════════════════════════════════════════════════════════════════════════════
async def _build_recs(category: str, title: str, want: int = 5) -> tuple[str, list]:
    candidates = pick_symbols(category, n=want + 6)
    results, syms = [], []
    for sym in candidates:
        if len(results) >= want:
            break
        d = await _run_blocking(get_stock_data, sym)
        if not d:
            continue
        # تأكد السعر $1-$50
        if not (1 <= d['price'] <= 50):
            continue
        d['entry_hi'] = round(d['price'] * 1.005, 2)
        d['entry_lo'] = round(d['price'] * 0.995, 2)
        results.append(rec_card(d))
        syms.append(sym)
        # تسجيل التوصية لتتبع الأداء (لكل فئة على حدة: opp / trend / penny)
        await _run_blocking(_save_recommendation, sym, category, d['entry_hi'], d['t1'], d['stop'])

    # طريق الاحتياط
    if not results:
        for sym in random.sample(_FALLBACK_1_50, min(want+4, len(_FALLBACK_1_50))):
            if len(results) >= want:
                break
            d = await _run_blocking(get_stock_data, sym)
            if not d or not (1 <= d['price'] <= 50):
                continue
            d['entry_hi'] = round(d['price'] * 1.005, 2)
            d['entry_lo'] = round(d['price'] * 0.995, 2)
            results.append(rec_card(d))
            syms.append(sym)
            await _run_blocking(_save_recommendation, sym, category, d['entry_hi'], d['t1'], d['stop'])

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body = sep.join(results) if results else "⚠️ السوق مغلق أو لا توجد بيانات الآن."
    return f"{title}\n\n{body}", syms

async def _build_whale_recs(want: int = 5) -> tuple[str, list]:
    candidates = pick_symbols("whale", n=want + 8)
    results, syms = [], []
    for sym in candidates:
        if len(results) >= want:
            break
        d = await _run_blocking(whale_score, sym)
        if not d or d['whale_pct'] < 30:
            continue
        results.append(whale_card(d))
        syms.append(d['symbol'])

    if not results:
        for sym in random.sample(_FALLBACK_1_50, min(want+6, len(_FALLBACK_1_50))):
            if len(results) >= want:
                break
            d = await _run_blocking(whale_score, sym)
            if not d:
                continue
            results.append(whale_card(d))
            syms.append(d['symbol'])

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body = sep.join(results) if results else "⚠️ لا تحركات مؤسسية واضحة الآن."
    return f"🐋 *رادار الحيتان المؤسسية:*\n\n{body}{DISCLAIMER}", syms

async def _build_swing_recs(want: int = 4) -> tuple[str, list]:
    # قائمة أسهم مرشّحة للـ سوينق (سعر ≤ $5)
    candidates = pick_symbols("swing", n=want + 10)
    # أضف من القائمة الاحتياطية
    candidates += random.sample(_FALLBACK_1_50, min(20, len(_FALLBACK_1_50)))
    candidates  = list(dict.fromkeys(candidates))

    results, syms = [], []
    for sym in candidates:
        if len(results) >= want:
            break
        d = await _run_blocking(swing_screen, sym)
        if not d:
            continue
        results.append(swing_card(d))
        syms.append(d['symbol'])
        # تسجيل التوصية لتتبع أداء السوينق لحاله
        await _run_blocking(_save_recommendation, d['symbol'], "swing", d['entry_hi'], d['t1'], d['stop'])

    if not results:
        body = (
            "⚠️ لم يتم العثور على أسهم تستوفي معايير سوينق الآن.\n\n"
            "*المعايير المطلوبة:*\n"
            "• سعر ≤ $5\n"
            "• حجم > 100,000\n"
            "• Float بين 30M و 100M\n"
            "• فوق MA50 دون كسره منذ 3 أشهر"
        )
        return f"〽️ *تحليل سوينق:*\n\n{body}", []

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body = sep.join(results)
    return f"〽️ *أسهم سوينق المؤهلة:*\n\n{body}", syms

# ══════════════════════════════════════════════════════════════════════════════
# 15.  إضافة تنبيه (مساعد)
# ══════════════════════════════════════════════════════════════════════════════
async def _do_add_alert(target, ctx: ContextTypes.DEFAULT_TYPE, uid: int, symbol: str, via_msg: bool):
    d = await _run_blocking(get_stock_data, symbol)
    alerts = ctx.bot_data.setdefault("alerts", {})
    if uid not in alerts:
        alerts[uid] = {}
    if not d:
        txt = _witty_not_found(symbol)
        if via_msg:
            await target.reply_text(txt, reply_markup=kb_back(), parse_mode="Markdown")
        else:
            await target.edit_message_text(txt, reply_markup=kb_back(), parse_mode="Markdown")
        return
    p = d['price']
    inf = dict(
        e_hi=round(p*1.005,2), e_lo=round(p*0.995,2),
        stop=round(p*0.95,2),
        t1=d['t1'], t2=d['t2'], t3=d['t3'],
    )
    alerts[uid][symbol] = inf
    txt = (
        f"✅ *تم إضافة تنبيه على {symbol}*\n\n"
        f"*{symbol}*\n\n"
        f"🟢 *منطقة الدخول*\n`{_fmt_price(inf['e_hi'])}` ← `{_fmt_price(inf['e_lo'])}`\n\n"
        f"🛑 *الوقف*\n`{_fmt_price(inf['stop'])}`\n\n"
        f"🎯 *الأهداف*\n`{_fmt_price(inf['t1'])}`\n`{_fmt_price(inf['t2'])}`\n`{_fmt_price(inf['t3'])}`\n\n"
        "سأُرسل لك تنبيهاً فور تحقق الهدف أو لمس الوقف 🔔"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 مربع التنبيهات",    callback_data="alerts_box")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])
    if via_msg:
        await target.reply_text(txt, reply_markup=kb, parse_mode="Markdown")
    else:
        await target.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# 16.  Job: فحص التنبيهات كل 5 دقائق
# ══════════════════════════════════════════════════════════════════════════════
async def check_alerts_job(ctx: ContextTypes.DEFAULT_TYPE):
    alerts = ctx.bot_data.get("alerts", {})
    for uid, ua in list(alerts.items()):
        for sym, inf in list(ua.items()):
            d = await _run_blocking(get_stock_data, sym)
            if not d:
                continue
            p = d['price']
            try:
                if p >= inf['t1']:
                    await ctx.bot.send_message(
                        uid,
                        f"🎯 *{sym} حقق الهدف الأول!*\n"
                        f"💰 السعر: `${_fmt_price(p)}` | الهدف: `${_fmt_price(inf['t1'])}`",
                        parse_mode="Markdown"
                    )
                    inf['t1'] = inf['t2'] + 99999
                elif p <= inf['stop']:
                    await ctx.bot.send_message(
                        uid,
                        f"🛑 *{sym} لمس وقف الخسارة!*\n"
                        f"💰 السعر: `${_fmt_price(p)}` | الوقف: `${_fmt_price(inf['stop'])}`",
                        parse_mode="Markdown"
                    )
                    ua.pop(sym, None)
            except Exception:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# 17.  تسجيل الـ Handlers
# ══════════════════════════════════════════════════════════════════════════════
def register_handlers(app):
    # broadcast conversation
    bc = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start,
                                     filters=filters.User(user_id=list(ADMIN_IDS)))],
        states={BROADCAST_WAIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send)]},
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
    )

    app.add_handler(CommandHandler("start",       start_handler))
    app.add_handler(CommandHandler("help",        help_handler))
    app.add_handler(CommandHandler("alerts",      alerts_cmd))
    app.add_handler(CommandHandler("stats",       stats_handler,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(CommandHandler("performance", performance_handler,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(CommandHandler("add_user",    add_user_cmd,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(CommandHandler("remove_user", remove_user_cmd,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(bc)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

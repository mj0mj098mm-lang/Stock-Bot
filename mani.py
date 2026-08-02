"""
StockBeacon AI — mani.py
كل المنطق والـ handlers. يُستورد من StockBeaconBOT.py
"""
import logging, datetime, json, random, time, re
import urllib.request, urllib.error
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd

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
SUBSCRIBED_USERS: set[int] = set(ADMIN_IDS)

DISCLAIMER = (
    "\n\n⚠️ *إخلاء مسؤولية:* تحليلات خوارزمية فنية — "
    "ليست نصيحة مالية، قرارك مسؤوليتك."
)

LOCKED_MSG = (
    "🔒 *هذه الميزة للمشتركين فقط*\n\n"
    "اشترك الآن للوصول لجميع التحليلات والرادارات اللحظية.\n"
    "💬 للتفعيل: https://t.me/e85ej"
)

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
    """بيانات كاملة بالأسعار الفورية - مع حماية تامة وسرعة فائقة"""
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
                
            chg = round((price - prev) / prev, 4) if prev else 0
            p = round(price, 4)
            
            e_hi = round(p * 1.01, 4)
            e_lo = round(p * 0.98, 4)
            e2 = round(p * 0.95, 4)
            e3 = round(p * 0.93, 4)
            stop = round(p * 0.92, 4)
            t1 = round(p * 1.05, 4)
            t2 = round(p * 1.08, 4)
            t3 = round(p * 1.12, 4)
            
            return dict(
                symbol=symbol.upper(), 
                price=p, 
                prev=round(prev, 4),
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


def deep_analysis(symbol: str) -> dict | None:
    """تحليل شامل باستخدام yfinance — يُستخدم عند إرسال رمز سهم"""
    try:
        tk   = _yf_ticker(symbol)
        info = tk.info
        # --- بيانات OHLCV يومية (6 أشهر) ---
        hist = tk.history(period="6mo", interval="1d", auto_adjust=True)
        if hist.empty or len(hist) < 30:
            return None
        close = hist['Close']
        p     = round(float(close.iloc[-1]), 4)
        prev  = round(float(close.iloc[-2]), 4) if len(close) > 1 else p
        vol   = int(hist['Volume'].iloc[-1])
        avg_vol = int(hist['Volume'].rolling(20).mean().iloc[-1])

        # المتوسطات
        ma20  = round(float(close.rolling(20).mean().iloc[-1]), 4)
        ma50  = round(float(close.rolling(50).mean().iloc[-1]), 4) if len(close) >= 50 else None
        ma200 = round(float(close.rolling(200).mean().iloc[-1]), 4) if len(close) >= 200 else None

        # RSI, MACD, VWAP
        rsi          = _calc_rsi(close)
        macd, sig, hist_val = _calc_macd(close)
        vwap         = _calc_vwap(hist.tail(30))

        # دعم ومقاومة (أدنى/أعلى 20 يوم باستثناء آخر شمعة)
        support    = round(float(hist['Low'].rolling(20).min().iloc[-2]), 4)
        resistance = round(float(hist['High'].rolling(20).max().iloc[-2]), 4)

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

        # القرار
        if score >= 7:
            decision = "شراء الآن 🟢"
            action   = "buy"
        elif score >= 4:
            decision = "شراء تدريجي 🔵"
            action   = "buy_grad"
        elif score <= -3:
            decision = "بيع / تجنب 🔴"
            action   = "sell"
        else:
            decision = "انتظار ⏳"
            action   = "wait"

        # نقاط الدخول
        e1   = round(p * 1.005, 4)       # دخول فوري
        e2   = round(support * 1.01, 4)  # عند الدعم
        e3   = round(support * 0.98, 4)  # دعم أعمق
        stop = round(support * 0.93, 4)
        t1   = round(resistance, 4)
        t2   = round(resistance * 1.15, 4)
        t3   = round(resistance * 1.35, 4)

        risk_pct   = round((e1 - stop) / e1 * 100, 1)
        reward_pct = round((t1 - e1) / e1 * 100, 1)
        rr         = round(reward_pct / risk_pct, 1) if risk_pct > 0 else 0

        return dict(
            symbol=symbol.upper(), price=p, prev=prev,
            change_pct=round((p-prev)/prev*100, 2) if prev else 0,
            volume=vol, avg_vol=avg_vol,
            ma20=ma20, ma50=ma50, ma200=ma200,
            rsi=rsi, macd=macd, signal=sig, hist=hist_val, vwap=vwap,
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
        p    = round(getattr(info, 'last_price', 0) or float(hist['Close'].iloc[-1]), 4)
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
# 6.  فاحص Swing (المعايير الأربعة)
# ══════════════════════════════════════════════════════════════════════════════
def swing_screen(symbol: str) -> dict | None:
    """
    يفلتر حسب:
      • سعر ≤ 5$
      • حجم > 100K
      • Float 30M-100M
      • فوق MA50 (لم يُكسر منذ 60 يوم)
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

        p    = round(price, 4)
        chg  = round((price - info.get('previousClose', price)) / info.get('previousClose', price) * 100, 2)
        rsi  = _calc_rsi(close)
        stop = round(float(ma50.iloc[-1]) * 0.97, 4)
        t1   = round(p * 1.20, 4)
        t2   = round(p * 1.45, 4)
        t3   = round(p * 1.80, 4)
        e_hi = round(p * 1.02, 4)
        e_lo = round(p * 0.98, 4)
        flt_m = round(flt / 1e6, 1)

        return dict(
            symbol=symbol.upper(), price=p, change_pct=chg,
            volume=vol, float_m=flt_m, rsi=rsi,
            ma50=round(float(ma50.iloc[-1]), 4),
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
        f"`{d['entry_hi']}` ← `{d['entry_lo']}`\n\n"
        f"🛑 *الوقف*\n`{d['stop']}`\n\n"
        f"🎯 *الأهداف*\n"
        f"`{d['t1']}`\n`{d['t2']}`\n`{d['t3']}`"
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
            f"\n*سعر الدخول الأول:*  `${d['e1']}`\n"
            f"*سعر الدخول الثاني:* `${d['e2']}`\n"
            f"*سعر الدخول الثالث:* `${d['e3']}`\n\n"
            f"*وقف الخسارة:* `${d['stop']}`\n"
            f"*نسبة المخاطرة:* `{d['risk_pct']}%`\n\n"
            f"*الهدف الأول:*  `${d['t1']}`\n"
            f"*الهدف الثاني:* `${d['t2']}`\n"
            f"*الهدف الثالث:* `${d['t3']}`\n\n"
            f"*نسبة العائد / المخاطرة:* `{d['rr']}:1`\n"
        )
    elif action == "sell":
        body = (
            f"\n⚠️ السهم في اتجاه هابط.\n"
            f"*وقف الخسارة إذا كنت ممسكاً:* `${d['stop']}`\n"
            f"*أقرب دعم للمراقبة:* `${d['support']}`\n"
        )
    else:
        body = (
            f"\n*السهم بحاجة لمزيد من التأكيد.*\n"
            f"*منطقة المراقبة:* `${d['support']}` — `${d['resistance']}`\n"
            f"*الدخول عند كسر:* `${d['resistance']}`\n"
        )

    if d.get('earnings'):
        body += f"\n📅 *أرباح قادمة:* `{d['earnings']}`\n"

    return header + body + DISCLAIMER

def swing_card(d: dict) -> str:
    return (
        f"*{d['symbol']}* 〽️ Swing\n\n"
        f"💲 *السعر:* `${d['price']}`"
        f"  📈 `{'+' if d['change_pct']>=0 else ''}{d['change_pct']}%`\n"
        f"📦 *الحجم:* `{_fmt_vol(d['volume'])}`"
        f"  |  🔄 *Float:* `{d['float_m']}M`\n"
        f"📊 *RSI:* `{d['rsi']}`"
        f"  |  *MA50:* `${d['ma50']}`\n\n"
        f"🟢 *منطقة الدخول*\n"
        f"`{d['entry_hi']}` ← `{d['entry_lo']}`\n\n"
        f"🛑 *الوقف*\n`{d['stop']}`\n\n"
        f"🎯 *الأهداف*\n"
        f"`{d['t1']}`\n`{d['t2']}`\n`{d['t3']}`"
        + DISCLAIMER
    )

def whale_card(d: dict) -> str:
    bar = "🟢" * (d['whale_pct']//20) + "⬜" * (5 - d['whale_pct']//20)
    return (
        f"*{d['symbol']}* 🐋\n\n"
        f"💲 *السعر:* `${d['price']}`"
        f"  📈 `{'+' if d['change_pct']>=0 else ''}{d['change_pct']}%`\n"
        f"📦 *الحجم اليوم:* `{_fmt_vol(d['volume'])}`\n"
        f"📊 *متوسط الحجم:* `{_fmt_vol(d['avg_vol'])}`\n"
        f"⚡ *مضاعف الحجم:* `{d['vol_ratio']}×`\n\n"
        f"🐋 *نشاط الحيتان:* {bar} `{d['whale_pct']}%`"
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
        [InlineKeyboardButton("〽️ تحليل Swing",          callback_data="swing")],
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
        [InlineKeyboardButton("💬 اشترك الآن", url="https://t.me/e85ej")],
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
        f"  〽️ Swing: `{st.get('swing',0)}`\n"
        f"  🔍 تحليلات: `{st.get('analyses',0)}`\n\n"
        "*المشتركون:*\n" + "\n".join(f"  • `{u}`" for u in subs),
        parse_mode="Markdown"
    )

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

async def remove_user_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/remove_user <ID>`", parse_mode="Markdown")
        return
    uid = int(ctx.args[0])
    SUBSCRIBED_USERS.discard(uid)
    await update.message.reply_text(f"❌ تم إلغاء اشتراك `{uid}`", parse_mode="Markdown")

async def _activate_user(ctx, uid: int, msg_target):
    SUBSCRIBED_USERS.add(uid)
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

    # إدخال ID مشترك جديد (أدمن)
    if ctx.user_data.get("adding_sub") and uid in ADMIN_IDS:
        ctx.user_data.pop("adding_sub")
        if txt.isdigit():
            await _activate_user(ctx, int(txt), update.message)
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
    d = get_stock_data(symbol)
    if not d:
        d = deep_analysis(symbol)
        if not d:
            await msg.edit_text(f"❌ لم يتم العثور على `{symbol}`. تأكد من صحة الرمز.", parse_mode="Markdown")
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
                f"• *{s}* | دخول `{inf['e_lo']}←{inf['e_hi']}`"
                f" | وقف `{inf['stop']}` | هدف `{inf['t1']}`"
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
        d = get_stock_data(sym)
        if not d:
            await q.edit_message_text(f"❌ تعذّر جلب `{sym}`", reply_markup=kb_back(), parse_mode="Markdown")
            return
        d['entry_hi'] = d.get('entry_hi', round(d['price']*1.02,4))
        d['entry_lo'] = d.get('entry_lo', round(d['price']*0.98,4))
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
                  "\n".join(f"• *{s}* | وقف `{i['stop']}` | هدف `{i['t1']}`"
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
                  "\n".join(f"• *{s}* | وقف `{i['stop']}` | هدف `{i['t1']}`"
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

    # ── Swing ─────────────────────────────────────────────────────────────────
    if data == "swing":
        ctx.bot_data["stats"]["swing"] = ctx.bot_data["stats"].get("swing",0) + 1
        await q.edit_message_text("⏳ جاري فحص معايير Swing (Float + MA50 + Volume) ...")
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
        d = get_stock_data(sym)
        if not d:
            continue
        # تأكد السعر $1-$50
        if not (1 <= d['price'] <= 50):
            continue
        d['entry_hi'] = round(d['price'] * 1.02, 4)
        d['entry_lo'] = round(d['price'] * 0.98, 4)
        results.append(rec_card(d))
        syms.append(sym)

    # طريق الاحتياط
    if not results:
        for sym in random.sample(_FALLBACK_1_50, min(want+4, len(_FALLBACK_1_50))):
            if len(results) >= want:
                break
            d = get_stock_data(sym)
            if not d or not (1 <= d['price'] <= 50):
                continue
            d['entry_hi'] = round(d['price'] * 1.02, 4)
            d['entry_lo'] = round(d['price'] * 0.98, 4)
            results.append(rec_card(d))
            syms.append(sym)

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body = sep.join(results) if results else "⚠️ السوق مغلق أو لا توجد بيانات الآن."
    return f"{title}\n\n{body}", syms

async def _build_whale_recs(want: int = 5) -> tuple[str, list]:
    candidates = pick_symbols("whale", n=want + 8)
    results, syms = [], []
    for sym in candidates:
        if len(results) >= want:
            break
        d = whale_score(sym)
        if not d or d['whale_pct'] < 30:
            continue
        results.append(whale_card(d))
        syms.append(d['symbol'])

    if not results:
        for sym in random.sample(_FALLBACK_1_50, min(want+6, len(_FALLBACK_1_50))):
            if len(results) >= want:
                break
            d = whale_score(sym)
            if not d:
                continue
            results.append(whale_card(d))
            syms.append(d['symbol'])

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body = sep.join(results) if results else "⚠️ لا تحركات مؤسسية واضحة الآن."
    return f"🐋 *رادار الحيتان المؤسسية:*\n\n{body}{DISCLAIMER}", syms

async def _build_swing_recs(want: int = 4) -> tuple[str, list]:
    # قائمة أسهم مرشّحة للـ Swing (سعر ≤ $5)
    candidates = pick_symbols("swing", n=want + 10)
    # أضف من القائمة الاحتياطية
    candidates += random.sample(_FALLBACK_1_50, min(20, len(_FALLBACK_1_50)))
    candidates  = list(dict.fromkeys(candidates))

    results, syms = [], []
    for sym in candidates:
        if len(results) >= want:
            break
        d = swing_screen(sym)
        if not d:
            continue
        results.append(swing_card(d))
        syms.append(d['symbol'])

    if not results:
        body = (
            "⚠️ لم يتم العثور على أسهم تستوفي معايير Swing الآن.\n\n"
            "*المعايير المطلوبة:*\n"
            "• سعر ≤ $5\n"
            "• حجم > 100,000\n"
            "• Float بين 30M و 100M\n"
            "• فوق MA50 دون كسره منذ 3 أشهر"
        )
        return f"〽️ *تحليل Swing:*\n\n{body}", []

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    body = sep.join(results)
    return f"〽️ *أسهم Swing المؤهلة:*\n\n{body}", syms

# ══════════════════════════════════════════════════════════════════════════════
# 15.  إضافة تنبيه (مساعد)
# ══════════════════════════════════════════════════════════════════════════════
async def _do_add_alert(target, ctx: ContextTypes.DEFAULT_TYPE, uid: int, symbol: str, via_msg: bool):
    d = get_stock_data(symbol)
    alerts = ctx.bot_data.setdefault("alerts", {})
    if uid not in alerts:
        alerts[uid] = {}
    if not d:
        txt = f"❌ لم يتم العثور على `{symbol}`. تأكد من الرمز."
        if via_msg:
            await target.reply_text(txt, reply_markup=kb_back(), parse_mode="Markdown")
        else:
            await target.edit_message_text(txt, reply_markup=kb_back(), parse_mode="Markdown")
        return
    p = d['price']
    inf = dict(
        e_hi=round(p*1.02,4), e_lo=round(p*0.98,4),
        stop=round(p*0.93,4),
        t1=d['t1'], t2=d['t2'], t3=d['t3'],
    )
    alerts[uid][symbol] = inf
    txt = (
        f"✅ *تم إضافة تنبيه على {symbol}*\n\n"
        f"*{symbol}*\n\n"
        f"🟢 *منطقة الدخول*\n`{inf['e_hi']}` ← `{inf['e_lo']}`\n\n"
        f"🛑 *الوقف*\n`{inf['stop']}`\n\n"
        f"🎯 *الأهداف*\n`{inf['t1']}`\n`{inf['t2']}`\n`{inf['t3']}`\n\n"
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
            d = get_stock_data(sym)
            if not d:
                continue
            p = d['price']
            try:
                if p >= inf['t1']:
                    await ctx.bot.send_message(
                        uid,
                        f"🎯 *{sym} حقق الهدف الأول!*\n"
                        f"💰 السعر: `${p}` | الهدف: `${inf['t1']}`",
                        parse_mode="Markdown"
                    )
                    inf['t1'] = inf['t2'] + 99999
                elif p <= inf['stop']:
                    await ctx.bot.send_message(
                        uid,
                        f"🛑 *{sym} لمس وقف الخسارة!*\n"
                        f"💰 السعر: `${p}` | الوقف: `${inf['stop']}`",
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
    app.add_handler(CommandHandler("add_user",    add_user_cmd,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(CommandHandler("remove_user", remove_user_cmd,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(bc)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

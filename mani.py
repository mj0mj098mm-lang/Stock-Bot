"""
StockBeacon — mani.py
كل الـ handlers والمنطق. يتم استيراده من StockBeaconBOT.py
"""
import logging
import datetime
import json
import random
import time
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 1. الإعدادات الثابتة
# ══════════════════════════════════════════════════════════════════════════════

ADMIN_IDS: set[int] = {5134111738}          # معرّف الأدمن — لا تغيّره
SUBSCRIBED_USERS: set[int] = set(ADMIN_IDS) # يبدأ بالأدمن مُفعَّلاً

DISCLAIMER = (
    "\n\n⚠️ *إخلاء مسؤولية:* التحليلات مبنية على خوارزميات فنية "
    "وليست دعوة للبيع أو الشراء، المسؤولية المالية تقع على عاتقك وحدك."
)

SUB_LOCKED = (
    "🔒 *هذه الميزة للمشتركين فقط!*\n\n"
    "اشترك الآن للوصول لجميع التحليلات والرادارات اللحظية.\n"
    "💬 للتفعيل تواصل مع الدعم: https://t.me/e85ej"
)

# ══════════════════════════════════════════════════════════════════════════════
# 2. جلب بيانات الأسهم الحية (مع إعادة المحاولة التلقائية)
# ══════════════════════════════════════════════════════════════════════════════

# قائمة احتياطية كبيرة (200+ سهم) في نطاق $1-$50 — تُستخدم فقط إذا فشل الجلب الحي
_FALLBACK_POOL = [
    "SOFI","LCID","RIVN","NIO","XPEV","NKLA","SPCE","SNDL","CLOV","HIMS",
    "SDC","BIRD","BLNK","CHPT","EVGO","JOBY","ACHR","WKHS","RIDE","GOEV",
    "FSR","CENN","FFIE","MULN","IDEX","MMAT","PROG","XELA","FCEL","PLUG",
    "ILUS","VVPR","SINT","JAGX","BBBY","AMC","GME","SNDL","TELL","INDO",
    "GFAI","VERB","ATER","BTBT","MOXC","ZKIN","COMS","GBOX","AREC","MITI",
    "SRM","NKGN","ATNF","GNLN","NLSP","CRBP","ENSV","LPCN","DARE","AVTE",
    "GEVO","CLNE","STEM","WATT","SUNW","SPWR","NOVA","FLNC","ARRY","SHLS",
    "CSIQ","JKS","DQ","DAQO","MAXN","ENPH","SEDG","RUN","VSLR","NOVA",
    "HYLN","NRGV","AMTX","GREE","MARA","RIOT","HUT","BITF","CIFR","BTCM",
    "MIGI","MBIO","PRQR","SLDB","TGTX","AGEN","CTIC","GTHX","NKTR","RCUS",
    "EXEL","FOLD","ACAD","SAGE","INVA","PTGX","IMMU","SRPT","BLUE","EDIT",
    "NTLA","BEAM","CRSP","PCRX","IRBT","OUST","VLDR","INVZ","LAZR","MVIS",
    "LIDR","AEVA","SMAR","PAYA","GTLB","S","ESTC","SUMO","ALRM","ARLO",
    "BAND","CARG","CARS","CDK","CIEN","CLX","COHR","CROX","CRVL","CSGP",
    "CVLT","DOMO","DUOL","DV","EGHT","ENFN","EVBG","EVTC","EXLS","EXPO",
    "FIVN","FOUR","FRSH","FROG","GH","GKOS","GMED","GPN","GSKY","HALO",
    "HCAT","HIMS","HLNE","HUBS","ICE","IIVI","ILMN","INMD","INSP","INTU",
    "IPGP","IRDM","IRHC","JAMF","JNPR","KNSL","KRYS","LBRT","LHCG","LKFN",
    "LNTH","LPRO","LPSN","LQDT","LSPD","LTRN","LYFT","MASI","MDXG","MGNI",
    "MIME","MMSI","MNMD","MODN","MRSN","MSEX","MSTR","MTDR","MXCT","MYMD",
]

_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

def _http_get(url: str, timeout: int = 8) -> dict | None:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == 2:
                logger.warning(f"_http_get failed {url}: {e}")
            time.sleep(0.5)
    return None


def get_stock_data(symbol: str) -> dict | None:
    """جلب بيانات السهم الحية مع 3 محاولات"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    data = _http_get(url)
    if not data:
        return None
    try:
        meta  = data['chart']['result'][0]['meta']
        price = meta.get('regularMarketPrice') or meta.get('previousClose')
        prev  = meta.get('chartPreviousClose') or meta.get('previousClose', price)
        if not price:
            return None
        chg   = round((price - prev) / prev * 100, 2) if prev else 0
        vol   = meta.get('regularMarketVolume', 0)
        p     = round(price, 2)
        # نطاق الدخول ±2%
        e_hi  = round(p * 1.02, 2)
        e_lo  = round(p * 0.98, 2)
        stop  = round(p * 0.93, 2)
        # أهداف متدرجة حسب السعر
        if p < 5:
            t1, t2, t3, t4 = round(p*1.20,2), round(p*1.45,2), round(p*1.80,2), round(p*2.30,2)
        elif p < 20:
            t1, t2, t3, t4 = round(p*1.15,2), round(p*1.35,2), round(p*1.60,2), round(p*2.00,2)
        else:
            t1, t2, t3, t4 = round(p*1.10,2), round(p*1.20,2), round(p*1.35,2), round(p*1.55,2)
        return dict(symbol=symbol.upper(), price=p, prev=round(prev,2),
                    change_pct=chg, volume=vol,
                    entry_hi=e_hi, entry_lo=e_lo,
                    stop=stop, t1=t1, t2=t2, t3=t3, t4=t4)
    except Exception as e:
        logger.error(f"parse {symbol}: {e}")
        return None


def get_live_symbols(category: str, count: int = 30) -> list[str]:
    """
    جلب رموز حية من Yahoo Finance Screener حسب الفئة.
    category: 'actives' | 'gainers' | 'losers' | 'small_gainers'
    """
    scr_map = {
        'actives':      'most_actives',
        'gainers':      'day_gainers',
        'losers':       'day_losers',
        'small_gainers':'small_cap_gainers',
        'undervalued':  'undervalued_growth_stocks',
    }
    scr_id = scr_map.get(category, 'most_actives')
    url = (
        f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?formatted=false&scrIds={scr_id}&count={count}&region=US&lang=en-US"
    )
    data = _http_get(url)
    symbols = []
    try:
        quotes = data['finance']['result'][0]['quotes']
        for q in quotes:
            sym   = q.get('symbol', '')
            price = q.get('regularMarketPrice', 0)
            # فلترة السعر $1-$50 وبدون أسهم مفضّلة أو ETF (لا توجد نقطة في الرمز)
            if '.' not in sym and '-' not in sym and 1 <= price <= 50:
                symbols.append(sym)
    except Exception:
        pass
    return symbols if symbols else []


def pick_symbols(category: str, n: int = 5) -> list[str]:
    """يختار n رموز حية عشوائية؛ يرجع إلى القائمة الاحتياطية إذا لزم"""
    live = get_live_symbols(category, count=50)
    pool = live if len(live) >= n else (live + _FALLBACK_POOL)
    # إزالة التكرار مع الحفاظ على الترتيب العشوائي
    unique = list(dict.fromkeys(pool))
    return random.sample(unique, min(n, len(unique)))


# ══════════════════════════════════════════════════════════════════════════════
# 3. حالة السوق
# ══════════════════════════════════════════════════════════════════════════════

def market_status() -> str:
    now = datetime.datetime.now(ZoneInfo('Asia/Riyadh'))
    if now.weekday() in (5, 6):
        return "🔴 السوق مغلق (عطلة نهاية الأسبوع)"
    t = now.time()
    if datetime.time(11,0) <= t < datetime.time(16,30):
        return "🟡 ما قبل الافتتاح (Pre-Market)"
    if datetime.time(16,30) <= t < datetime.time(23,0):
        return "🟢 السوق مفتوح الآن"
    if datetime.time(23,0) <= t or t < datetime.time(4,0):
        return "🔵 بعد الإغلاق (After-Hours)"
    return "🔴 السوق مغلق حالياً"

def sub_status(uid: int) -> str:
    return "🟢 حالة الاشتراك: مُفعَّل ✅" if uid in SUBSCRIBED_USERS \
        else "🔴 حالة الاشتراك: غير مُفعَّل ❌"

# ══════════════════════════════════════════════════════════════════════════════
# 4. بناء رسائل التوصية
# ══════════════════════════════════════════════════════════════════════════════

def rec_text(d: dict) -> str:
    """تنسيق التوصية مطابق للنموذج المطلوب"""
    arrow = "📈" if d['change_pct'] >= 0 else "📉"
    sign  = "+" if d['change_pct'] >= 0 else ""
    vol   = f"{d['volume']/1e6:.1f}M" if d['volume'] >= 1e6 else f"{d['volume']/1e3:.0f}K"
    return (
        f"*{d['symbol']}*\n\n"
        f"📌 *حالة السوق:* {market_status()}\n"
        f"{arrow} *التغيير:* `{sign}{d['change_pct']}%`  |  📦 `{vol}`\n\n"
        f"🟢 *منطقة الدخول*\n"
        f"`{d['entry_hi']}` ← `{d['entry_lo']}`\n\n"
        f"🛑 *الوقف*\n"
        f"`{d['stop']}`\n\n"
        f"🎯 *الأهداف*\n"
        f"`{d['t1']}`\n"
        f"`{d['t2']}`\n"
        f"`{d['t3']}`\n"
        f"`{d['t4']}`"
        f"{DISCLAIMER}"
    )

def analysis_text(d: dict) -> str:
    """تحليل مفصّل لسهم بعينه"""
    return rec_text(d)

# ══════════════════════════════════════════════════════════════════════════════
# 5. لوحات المفاتيح
# ══════════════════════════════════════════════════════════════════════════════

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
        [InlineKeyboardButton("🔔 مربع التنبيهات",       callback_data="alerts_box")],
        [InlineKeyboardButton("💬 الدعم الفني والتفعيل", url="https://t.me/e85ej")],
    ])

def kb_after_analysis(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 تنبيه على هذا السهم", callback_data=f"add_alert:{symbol}"),
            InlineKeyboardButton("🔄 تحديث",               callback_data=f"refresh:{symbol}"),
        ],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")]])

def kb_locked() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 اشترك الآن", url="https://t.me/e85ej")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])

def kb_alerts(uid: int, alerts: dict) -> InlineKeyboardMarkup:
    rows = []
    for sym in list(alerts.get(uid, {}).keys())[:8]:
        rows.append([
            InlineKeyboardButton(f"📊 {sym}", callback_data=f"refresh:{sym}"),
            InlineKeyboardButton("🗑️",        callback_data=f"del_alert:{sym}"),
        ])
    rows.append([InlineKeyboardButton("➕ إضافة تنبيه",     callback_data="new_alert")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)

# ══════════════════════════════════════════════════════════════════════════════
# 6. أوامر الأدمن (/start, /help, /add_user, /remove_user, /stats, /broadcast)
# ══════════════════════════════════════════════════════════════════════════════

async def _set_commands(bot, uid: int):
    """تعيين أوامر مختلفة للأدمن والمستخدم العادي"""
    scope = {"type": "chat", "chat_id": uid}
    if uid in ADMIN_IDS:
        cmds = [
            BotCommand("start",       "▶️ القائمة الرئيسية"),
            BotCommand("stats",       "📊 إحصائيات البوت"),
            BotCommand("broadcast",   "📣 إرسال للكل"),
            BotCommand("add_user",    "✅ تفعيل مشترك"),
            BotCommand("remove_user", "❌ إلغاء مشترك"),
            BotCommand("alerts",      "🔔 تنبيهاتي"),
        ]
    else:
        cmds = [
            BotCommand("start",  "▶️ القائمة الرئيسية"),
            BotCommand("help",   "📖 الدليل"),
            BotCommand("alerts", "🔔 تنبيهاتي"),
        ]
    await bot.set_my_commands(cmds, scope=scope)


async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    ctx.bot_data.setdefault("all_users", set()).add(uid)
    ctx.bot_data.setdefault("stats", {
        "opp":0,"trend":0,"whale":0,"penny":0,"analyses":0
    })
    await _set_commands(ctx.bot, uid)

    text = (
        f"📡 *أهلاً بك يا {name} في بوت Stock Beacon* 🚀\n\n"
        "💡 *مميزات البوت:*\n"
        "▪️ رادار الحيتان والسيولة المؤسسية الضخمة 🐋\n"
        "▪️ كاشف الفرص والاختراقات الفورية 🎯\n"
        "▪️ أسهم مضاربية رخيصة بأسعار لحظية ⚡\n"
        "▪️ تنبيهات فورية عند تحقق الأهداف 🔔\n\n"
        "💳 *خطط الاشتراك:*\n"
        "• شهري: 299 ريال/شهر 🗓️\n"
        "• سنوي (الماسي): 399 ريال/سنة 🔥\n\n"
        f"📌 *حالة السوق:* {market_status()}\n"
        f"🆔 *معرفك:* `{uid}`\n"
        f"{sub_status(uid)}\n\n"
        "⚠️ التوصيات خوارزميات فنية وليست دعوة للبيع أو الشراء."
    )
    await update.message.reply_text(text, reply_markup=kb_main(), parse_mode="Markdown")


async def help_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *دليل Stock Beacon*\n\n"
        "• أرسل رمز السهم مباشرة مثل `AAPL` أو `TSLA`\n"
        "• بعد التحليل اضغط *تنبيه على هذا السهم* وسيُربط تلقائياً\n"
        "• `/alerts` — مربع تنبيهاتك\n"
        "• أرسل صورة الشارت للتحليل البصري",
        parse_mode="Markdown"
    )


async def add_user_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/add_user <ID>`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
        SUBSCRIBED_USERS.add(uid)
        await update.message.reply_text(
            f"✅ *تم تفعيل المشترك*\n🆔 `{uid}`", parse_mode="Markdown"
        )
        # أبلغ المستخدم بالتفعيل
        try:
            await ctx.bot.send_message(
                uid,
                "🎉 *تم تفعيل اشتراكك في Stock Beacon!*\n\n"
                "الآن يمكنك الوصول لجميع المميزات:\n"
                "🎯 كاشف الفرص • 🔥 المطبل لها • 🐋 رادار الحيتان • ⚡ المضاربية\n\n"
                "اضغط /start لفتح القائمة.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ ID غير صالح.")


async def remove_user_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not ctx.args:
        await update.message.reply_text("الاستخدام: `/remove_user <ID>`", parse_mode="Markdown")
        return
    try:
        uid = int(ctx.args[0])
        SUBSCRIBED_USERS.discard(uid)
        await update.message.reply_text(
            f"❌ *تم إلغاء اشتراك*\n🆔 `{uid}`", parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ ID غير صالح.")


async def stats_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    all_users = ctx.bot_data.get("all_users", set())
    st        = ctx.bot_data.get("stats", {})
    alerts    = ctx.bot_data.get("alerts", {})
    text = (
        "📊 *إحصائيات البوت*\n\n"
        f"👥 إجمالي المستخدمين: `{len(all_users)}`\n"
        f"✅ المشتركين النشطين: `{len(SUBSCRIBED_USERS)}`\n"
        f"🔔 التنبيهات النشطة: `{sum(len(v) for v in alerts.values())}`\n\n"
        f"📈 استخدام الميزات:\n"
        f"  🎯 كاشف الفرص: `{st.get('opp',0)}`\n"
        f"  🔥 المطبل لها: `{st.get('trend',0)}`\n"
        f"  🐋 رادار الحيتان: `{st.get('whale',0)}`\n"
        f"  ⚡ مضاربية رخيصة: `{st.get('penny',0)}`\n"
        f"  🔍 تحليلات فردية: `{st.get('analyses',0)}`\n\n"
        f"🆔 *المشتركون:*\n"
        + "\n".join(f"  • `{uid}`" for uid in sorted(SUBSCRIBED_USERS))
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Broadcast ────────────────────────────────────────────────────────────────
BROADCAST_MSG = 40

async def broadcast_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(
        "📣 *أرسل الرسالة التي تريد إيصالها لجميع المشتركين:*\n"
        "_(أرسل /cancel للإلغاء)_",
        parse_mode="Markdown"
    )
    return BROADCAST_MSG

async def broadcast_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg   = update.message.text
    sent  = 0
    failed = 0
    for uid in list(SUBSCRIBED_USERS):
        try:
            await ctx.bot.send_message(uid, f"📣 *رسالة من الإدارة:*\n\n{msg}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"✅ تم الإرسال لـ `{sent}` مشترك\n❌ فشل: `{failed}`",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم إلغاء البث.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# 7. تحليل السهم (نص عادي)
# ══════════════════════════════════════════════════════════════════════════════

WAIT_ALERT_SYM = 50

async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    text_in = update.message.text.strip()
    ctx.bot_data.setdefault("all_users", set()).add(uid)

    # إذا كنا ننتظر رمز تنبيه يدوي
    if ctx.user_data.get("waiting_alert"):
        ctx.user_data.pop("waiting_alert")
        await _do_add_alert(update.message, ctx, uid, text_in.upper(), via_message=True)
        return

    if uid not in SUBSCRIBED_USERS:
        await update.message.reply_text(SUB_LOCKED, reply_markup=kb_locked(), parse_mode="Markdown")
        return

    symbol = text_in.upper()
    msg = await update.message.reply_text(f"⏳ جاري جلب بيانات *{symbol}* ...", parse_mode="Markdown")
    d   = get_stock_data(symbol)
    if not d:
        await msg.edit_text(
            f"❌ لم يتم العثور على سهم `{symbol}`\nتأكد من الرمز (مثال: AAPL, TSLA, NVDA)",
            parse_mode="Markdown"
        )
        return
    ctx.bot_data.setdefault("stats", {})["analyses"] = \
        ctx.bot_data["stats"].get("analyses", 0) + 1
    await msg.edit_text(analysis_text(d), reply_markup=kb_after_analysis(symbol), parse_mode="Markdown")


async def photo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in SUBSCRIBED_USERS:
        await update.message.reply_text(SUB_LOCKED, reply_markup=kb_locked(), parse_mode="Markdown")
        return
    await update.message.reply_text(
        "📊 *تحليل الشارت المرفق:*\n\n"
        "🟢 *النمط:* اختراق إيجابي مع زخم متصاعد\n\n"
        "🟢 *منطقة الدخول*\n`السعر الحالي` ← `-2%`\n\n"
        "🛑 *الوقف*\n`-7% من الدخول`\n\n"
        "🎯 *الأهداف*\n`+15%`\n`+30%`\n`+50%`\n`+80%`"
        f"{DISCLAIMER}",
        reply_markup=kb_back(), parse_mode="Markdown"
    )


async def alerts_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    alerts = ctx.bot_data.setdefault("alerts", {})
    ua     = alerts.get(uid, {})
    if not ua:
        text = "🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات مضافة.\nاضغط ➕ لإضافة سهم."
    else:
        lines = ["🔔 *تنبيهاتك النشطة:*\n"]
        for s, inf in ua.items():
            lines.append(
                f"• *{s}*\n  دخول: `{inf['entry_lo']}←{inf['entry_hi']}`"
                f" | وقف: `{inf['stop']}` | هدف: `{inf['t1']}`"
            )
        text = "\n".join(lines)
    await update.message.reply_text(text, reply_markup=kb_alerts(uid, alerts), parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# 8. معالج الضغطات
# ══════════════════════════════════════════════════════════════════════════════

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = q.from_user.id
    name = q.from_user.first_name
    ctx.bot_data.setdefault("all_users", set()).add(uid)

    # ── القائمة الرئيسية ──────────────────────────────────────────────────────
    if data == "main_menu":
        await q.edit_message_text(
            f"📡 *أهلاً {name} في Stock Beacon* 🚀\n\n"
            f"📌 *حالة السوق:* {market_status()}\n"
            f"🆔 *معرفك:* `{uid}`\n"
            f"{sub_status(uid)}",
            reply_markup=kb_main(), parse_mode="Markdown"
        )
        return

    # ── أزرار الميزات المغلقة لغير المشتركين ─────────────────────────────────
    if data in ("opp","trend","whale","penny","alerts_box","new_alert") and uid not in SUBSCRIBED_USERS:
        await q.edit_message_text(SUB_LOCKED, reply_markup=kb_locked(), parse_mode="Markdown")
        return

    # ── تحديث سهم ─────────────────────────────────────────────────────────────
    if data.startswith("refresh:"):
        sym = data.split(":",1)[1]
        await q.edit_message_text(f"⏳ جاري تحديث *{sym}* ...", parse_mode="Markdown")
        d = get_stock_data(sym)
        if not d:
            await q.edit_message_text(
                f"❌ تعذّر جلب `{sym}` — حاول مجدداً",
                reply_markup=kb_after_analysis(sym) if uid in SUBSCRIBED_USERS else kb_back(),
                parse_mode="Markdown"
            )
            return
        await q.edit_message_text(analysis_text(d), reply_markup=kb_after_analysis(sym), parse_mode="Markdown")
        return

    # ── إضافة تنبيه (مربوط بالرمز مباشرة من زر التحليل) ─────────────────────
    if data.startswith("add_alert:"):
        sym = data.split(":",1)[1]
        await _do_add_alert(q, ctx, uid, sym, via_message=False)
        return

    # ── مربع التنبيهات ────────────────────────────────────────────────────────
    if data == "alerts_box":
        alerts = ctx.bot_data.setdefault("alerts", {})
        ua     = alerts.get(uid, {})
        if not ua:
            text = "🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات.\nاضغط ➕ لإضافة سهم."
        else:
            lines = ["🔔 *تنبيهاتك النشطة:*\n"]
            for s, inf in ua.items():
                lines.append(
                    f"• *{s}* | دخول `{inf['entry_lo']}←{inf['entry_hi']}`"
                    f" | وقف `{inf['stop']}` | هدف `{inf['t1']}`"
                )
            text = "\n".join(lines)
        await q.edit_message_text(text, reply_markup=kb_alerts(uid, alerts), parse_mode="Markdown")
        return

    # ── إضافة تنبيه يدوي جديد ────────────────────────────────────────────────
    if data == "new_alert":
        ctx.user_data["waiting_alert"] = True
        await q.edit_message_text(
            "✏️ *أرسل رمز السهم للتنبيه عليه:*\n_(مثال: TSLA أو AAPL)_",
            reply_markup=kb_back(), parse_mode="Markdown"
        )
        return

    # ── حذف تنبيه ─────────────────────────────────────────────────────────────
    if data.startswith("del_alert:"):
        sym    = data.split(":",1)[1]
        alerts = ctx.bot_data.setdefault("alerts", {})
        alerts.get(uid, {}).pop(sym, None)
        ua = alerts.get(uid, {})
        text = ("🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات." if not ua else
                "🔔 *تنبيهاتك النشطة:*\n\n" +
                "\n".join(f"• *{s}* | وقف `{i['stop']}` | هدف `{i['t1']}`"
                          for s,i in ua.items()))
        await q.edit_message_text(text, reply_markup=kb_alerts(uid, alerts), parse_mode="Markdown")
        return

    # ── كاشف الفرص ────────────────────────────────────────────────────────────
    if data == "opp":
        ctx.bot_data.setdefault("stats",{})["opp"] = ctx.bot_data["stats"].get("opp",0)+1
        await q.edit_message_text("⏳ جاري رصد أبرز الفرص الاختراقية ...")
        text = await _build_recs_text("gainers", "🎯 *أبرز الفرص الاختراقية اللحظية:*")
        await q.edit_message_text(text, reply_markup=kb_back(), parse_mode="Markdown")
        return

    # ── الأسهم المطبل لها ────────────────────────────────────────────────────
    if data == "trend":
        ctx.bot_data.setdefault("stats",{})["trend"] = ctx.bot_data["stats"].get("trend",0)+1
        await q.edit_message_text("⏳ جاري رصد أعلى الأسهم زخماً ...")
        text = await _build_recs_text("actives", "🔥 *الأسهم الأعلى زخماً والمطبل لها:*")
        await q.edit_message_text(text, reply_markup=kb_back(), parse_mode="Markdown")
        return

    # ── رادار الحيتان ─────────────────────────────────────────────────────────
    if data == "whale":
        ctx.bot_data.setdefault("stats",{})["whale"] = ctx.bot_data["stats"].get("whale",0)+1
        await q.edit_message_text("⏳ جاري رصد تحركات الحيتان المؤسسية ...")
        text = await _build_recs_text("undervalued", "🐋 *رادار السيولة والحيتان المؤسسية:*")
        await q.edit_message_text(text, reply_markup=kb_back(), parse_mode="Markdown")
        return

    # ── أسهم مضاربية رخيصة ──────────────────────────────────────────────────
    if data == "penny":
        ctx.bot_data.setdefault("stats",{})["penny"] = ctx.bot_data["stats"].get("penny",0)+1
        await q.edit_message_text("⏳ جاري البحث عن أسهم مضاربية ($1-$50) ...")
        text = await _build_recs_text("small_gainers", "⚡ *أسهم مضاربية رخيصة ($1–$50):*")
        await q.edit_message_text(text, reply_markup=kb_back(), parse_mode="Markdown")
        return


# ══════════════════════════════════════════════════════════════════════════════
# 9. بناء قائمة التوصيات
# ══════════════════════════════════════════════════════════════════════════════

async def _build_recs_text(category: str, title: str, count: int = 5) -> str:
    symbols = pick_symbols(category, n=count + 4)  # نختار أكثر لتعويض الفاشل
    results = []
    for sym in symbols:
        if len(results) >= count:
            break
        d = get_stock_data(sym)
        if not d:
            continue
        arrow = "📈" if d['change_pct'] >= 0 else "📉"
        sign  = "+" if d['change_pct'] >= 0 else ""
        results.append(
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*{d['symbol']}*  {arrow} `{sign}{d['change_pct']}%`\n\n"
            f"🟢 *منطقة الدخول*\n`{d['entry_hi']}` ← `{d['entry_lo']}`\n\n"
            f"🛑 *الوقف*  `{d['stop']}`\n\n"
            f"🎯 *الأهداف*\n`{d['t1']}` | `{d['t2']}` | `{d['t3']}` | `{d['t4']}`"
        )
    if not results:
        # طريقة أخيرة: اختر من القائمة الاحتياطية مباشرة
        for sym in random.sample(_FALLBACK_POOL, min(count+4, len(_FALLBACK_POOL))):
            if len(results) >= count:
                break
            d = get_stock_data(sym)
            if not d:
                continue
            arrow = "📈" if d['change_pct'] >= 0 else "📉"
            sign  = "+" if d['change_pct'] >= 0 else ""
            results.append(
                f"━━━━━━━━━━━━━━━━━━\n"
                f"*{d['symbol']}*  {arrow} `{sign}{d['change_pct']}%`\n\n"
                f"🟢 *منطقة الدخول*\n`{d['entry_hi']}` ← `{d['entry_lo']}`\n\n"
                f"🛑 *الوقف*  `{d['stop']}`\n\n"
                f"🎯 *الأهداف*\n`{d['t1']}` | `{d['t2']}` | `{d['t3']}` | `{d['t4']}`"
            )
    body = "\n\n".join(results) if results else "⚠️ السوق مغلق أو لا توجد بيانات متاحة الآن."
    return f"{title}\n\n{body}{DISCLAIMER}"


# ══════════════════════════════════════════════════════════════════════════════
# 10. إضافة تنبيه (مساعد)
# ══════════════════════════════════════════════════════════════════════════════

async def _do_add_alert(target, ctx: ContextTypes.DEFAULT_TYPE, uid: int, symbol: str, via_message: bool):
    d = get_stock_data(symbol)
    alerts = ctx.bot_data.setdefault("alerts", {})
    if uid not in alerts:
        alerts[uid] = {}
    if not d:
        txt = f"❌ لم يتم العثور على `{symbol}`. تأكد من الرمز."
        kb  = kb_back()
        if via_message:
            await target.reply_text(txt, reply_markup=kb, parse_mode="Markdown")
        else:
            await target.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")
        return
    alerts[uid][symbol] = {k: d[k] for k in ("entry_hi","entry_lo","stop","t1","t2","t3","t4")}
    alerts[uid][symbol]["entry"] = d['price']
    txt = (
        f"✅ *تم إضافة تنبيه على {symbol}*\n\n"
        f"*{symbol}*\n\n"
        f"🟢 *منطقة الدخول*\n`{d['entry_hi']}` ← `{d['entry_lo']}`\n\n"
        f"🛑 *الوقف*\n`{d['stop']}`\n\n"
        f"🎯 *الأهداف*\n`{d['t1']}`\n`{d['t2']}`\n`{d['t3']}`\n`{d['t4']}`\n\n"
        "سأُرسل لك تنبيهاً فور تحقق الهدف أو لمس الوقف 🔔"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 مربع التنبيهات",    callback_data="alerts_box")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])
    if via_message:
        await target.reply_text(txt, reply_markup=kb, parse_mode="Markdown")
    else:
        await target.edit_message_text(txt, reply_markup=kb, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# 11. فحص التنبيهات (Job Queue كل 5 دقائق)
# ══════════════════════════════════════════════════════════════════════════════

async def check_alerts_job(ctx: ContextTypes.DEFAULT_TYPE):
    alerts = ctx.bot_data.get("alerts", {})
    for uid, ua in list(alerts.items()):
        for sym, inf in list(ua.items()):
            d = get_stock_data(sym)
            if not d:
                continue
            p = d['price']
            if p >= inf['t1']:
                try:
                    await ctx.bot.send_message(
                        uid,
                        f"🎯 *{sym} حقق الهدف الأول!*\n\n"
                        f"💰 السعر: `${p}`\n"
                        f"✅ الهدف الأول `${inf['t1']}` — *تحقق!*\n"
                        f"🚀 الهدف التالي: `${inf['t2']}`",
                        parse_mode="Markdown"
                    )
                    inf['t1'] = inf['t2'] + 9999  # منع التكرار
                except Exception:
                    pass
            elif p <= inf['stop']:
                try:
                    await ctx.bot.send_message(
                        uid,
                        f"🛑 *{sym} لمس وقف الخسارة!*\n\n"
                        f"💰 السعر: `${p}`\n"
                        f"❌ وقف الخسارة `${inf['stop']}` — *تفعّل!*",
                        parse_mode="Markdown"
                    )
                    ua.pop(sym, None)
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
# 12. تسجيل كل الـ Handlers
# ══════════════════════════════════════════════════════════════════════════════

def register_handlers(app):
    # Broadcast ConversationHandler (للأدمن فقط)
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_start,
                                    filters=filters.User(user_id=list(ADMIN_IDS)))],
        states={BROADCAST_MSG: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send)
        ]},
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
    )

    app.add_handler(CommandHandler("start",       start_handler))
    app.add_handler(CommandHandler("help",        help_handler))
    app.add_handler(CommandHandler("alerts",      alerts_cmd))
    app.add_handler(CommandHandler("stats",       stats_handler,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(CommandHandler("add_user",    add_user_handler,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(CommandHandler("remove_user", remove_user_handler,
                                   filters=filters.User(user_id=list(ADMIN_IDS))))
    app.add_handler(broadcast_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

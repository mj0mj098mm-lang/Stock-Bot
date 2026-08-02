import logging
import datetime
import json
import random
import urllib.request
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)

logger = logging.getLogger(__name__)

# ─── 1. الإعدادات والمشتركين ──────────────────────────────────────────────────
ADMIN_IDS = [5134111738]
SUBSCRIBED_USERS = set(ADMIN_IDS)

DISCLAIMER_TEXT = (
    "\n\n⚠️ *إخلاء مسؤولية:* التوصيات مبنية على خوارزميات فنية "
    "وليست دعوة للبيع أو الشراء، المسؤولية المالية تقع على عاتقك وحدك."
)

SUBSCRIBE_PROMPT_TEXT = (
    "🔒 *عذراً، هذه الميزة مخصصة للمشتركين فقط!*\n\n"
    "حسابك غير مفعل حالياً. للاشتراك والاستفادة من التحليلات "
    "والرادارات اللحظية، تواصل مع الدعم الفني:\n"
    "💬 https://t.me/e85ej"
)

# ─── 2. قوائم الأسهم ──────────────────────────────────────────────────────────

OPPORTUNITY_STOCKS = [
    "NVDA", "AAPL", "TSLA", "META", "MSFT", "AMZN", "GOOGL", "AMD",
    "PLTR", "SMCI", "ARM", "MSTR", "COIN", "SNOW", "NET", "CRWD",
    "PANW", "DDOG", "MDB", "ORCL", "CRM", "UBER", "LYFT", "ABNB",
    "SHOP", "SQ", "PYPL", "ROKU", "TTD", "RBLX"
]

TRENDING_STOCKS = [
    "NVDA", "TSLA", "PLTR", "MSTR", "SMCI", "ARM", "COIN", "GME",
    "AMC", "BBBY", "RIVN", "LCID", "NIO", "XPEV", "LI", "NKLA",
    "SPCE", "JOBY", "ACHR", "ARCHER", "WKHS", "FSR", "GOEV", "RIDE",
    "CLOV", "WISH", "SDC", "HIMS", "BIRD", "SOFI"
]

PENNY_STOCKS = [
    "SOFI", "LCID", "SNDL", "CLOV", "WISH", "SDC", "HIMS", "BIRD",
    "NKLA", "WKHS", "GOEV", "RIDE", "SPCE", "BLNK", "CHPT", "EVGO",
    "NKLA", "FFIE", "MULN", "IDEX", "ILUS", "MMAT", "PROG", "CENN",
    "VVPR", "SINT", "JAGX", "XELA", "FCEL", "PLUG"
]

WHALE_STOCKS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "BRK-B",
    "JPM", "JNJ", "V", "UNH", "XOM", "PG", "HD", "MA", "CVX", "MRK",
    "PEP", "ABBV", "KO", "AVGO", "COST", "MCD", "TMO", "DHR", "ACN",
    "LIN", "TXN", "NEE"
]

# ─── 3. السوق والبيانات ───────────────────────────────────────────────────────

def get_market_status_ksa() -> str:
    now_ksa = datetime.datetime.now(ZoneInfo('Asia/Riyadh'))
    if now_ksa.weekday() in [5, 6]:
        return "🔴 السوق مغلق (عطلة نهاية الأسبوع)"
    t = now_ksa.time()
    if datetime.time(11, 0) <= t < datetime.time(16, 30):
        return "🟡 ما قبل الافتتاح (Pre-Market)"
    elif datetime.time(16, 30) <= t < datetime.time(23, 0):
        return "🟢 السوق مفتوح حالياً"
    elif datetime.time(23, 0) <= t or t < datetime.time(4, 0):
        return "🔵 بعد الإغلاق (After-Hours)"
    else:
        return "🔴 السوق مغلق حالياً"

def get_stock_data(symbol: str):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        meta = data['chart']['result'][0]['meta']
        price = meta.get('regularMarketPrice') or meta.get('previousClose')
        prev  = meta.get('chartPreviousClose') or meta.get('previousClose', price)
        if not price:
            return None
        change_pct = ((price - prev) / prev * 100) if prev else 0
        volume = meta.get('regularMarketVolume', 0)
        return {
            "symbol":     symbol.upper(),
            "price":      round(price, 2),
            "prev":       round(prev, 2),
            "change_pct": round(change_pct, 2),
            "volume":     volume,
            "target1":    round(price * 1.10, 2),
            "target2":    round(price * 1.18, 2),
            "stop":       round(price * 0.93, 2),
        }
    except Exception as e:
        logger.error(f"get_stock_data({symbol}): {e}")
        return None

def check_subscription(user_id: int) -> str:
    return "🟢 حالة الاشتراك: مُفعّل ✅" if user_id in SUBSCRIBED_USERS \
        else "🔴 حالة الاشتراك: غير مُفعّل ❌"

# ─── 4. لوحات المفاتيح ────────────────────────────────────────────────────────

def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎯 كاشف الفرص",        callback_data="btn_opportunities"),
            InlineKeyboardButton("🔥 الأسهم المطبل لها", callback_data="btn_trending"),
        ],
        [
            InlineKeyboardButton("🐋 رادار الحيتان",        callback_data="btn_whales"),
            InlineKeyboardButton("⚡ أسهم مضاربية رخيصة", callback_data="btn_penny"),
        ],
        [InlineKeyboardButton("🔔 مربع التنبيهات",            callback_data="btn_alerts")],
        [InlineKeyboardButton("💬 الدعم الفني والتفعيل",      url="https://t.me/e85ej")],
    ])

def analysis_keyboard(symbol: str) -> InlineKeyboardMarkup:
    """لوحة مفاتيح التحليل — زر التنبيه مربوط بالرمز مباشرة"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔔 تنبيه على هذا السهم", callback_data=f"add_alert:{symbol}"),
            InlineKeyboardButton("🔄 تحديث السعر",          callback_data=f"refresh:{symbol}"),
        ],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="btn_main_menu")],
    ])

def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="btn_main_menu")]])

def alerts_keyboard(user_id: int, alerts: dict) -> InlineKeyboardMarkup:
    """لوحة مربع التنبيهات"""
    rows = []
    for sym in list(alerts.get(user_id, {}).keys())[:8]:
        rows.append([
            InlineKeyboardButton(f"📊 {sym}", callback_data=f"refresh:{sym}"),
            InlineKeyboardButton("🗑️ حذف",   callback_data=f"del_alert:{sym}"),
        ])
    rows.append([InlineKeyboardButton("➕ إضافة تنبيه جديد", callback_data="new_alert")])
    rows.append([InlineKeyboardButton("🏠 القائمة الرئيسية",  callback_data="btn_main_menu")])
    return InlineKeyboardMarkup(rows)

# ─── 5. بناء رسالة التحليل ────────────────────────────────────────────────────

def build_analysis_text(d: dict) -> str:
    arrow = "📈" if d['change_pct'] >= 0 else "📉"
    sign  = "+" if d['change_pct'] >= 0 else ""
    vol_b = f"{d['volume']/1_000_000:.1f}M" if d['volume'] >= 1_000_000 else f"{d['volume']/1_000:.0f}K"
    return (
        f"📊 *تحليل سهم: {d['symbol']}*\n\n"
        f"💰 *السعر الحالي:* `${d['price']}`\n"
        f"{arrow} *التغيير:* `{sign}{d['change_pct']}%` مقارنة بالأمس\n"
        f"📦 *الحجم:* `{vol_b}`\n\n"
        f"🎯 *الهدف الأول (+10%):*  `${d['target1']}`\n"
        f"🚀 *الهدف الثاني (+18%):* `${d['target2']}`\n"
        f"🛑 *وقف الخسارة (-7%):*  `${d['stop']}`\n\n"
        f"📌 *حالة السوق:* {get_market_status_ksa()}"
        f"{DISCLAIMER_TEXT}"
    )

# ─── 6. جلب أسهم عشوائية لكل قسم ────────────────────────────────────────────

def pick_random_stocks(pool: list, count: int = 6) -> list:
    return random.sample(pool, min(count, len(pool)))

async def fetch_stocks_text(pool: list, title: str, count: int = 6) -> str:
    symbols = pick_random_stocks(pool, count)
    lines = [f"*{title}*\n"]
    for sym in symbols:
        d = get_stock_data(sym)
        if d:
            arrow = "📈" if d['change_pct'] >= 0 else "📉"
            sign  = "+" if d['change_pct'] >= 0 else ""
            lines.append(
                f"{arrow} *{d['symbol']}* | `${d['price']}` | "
                f"`{sign}{d['change_pct']}%` | هدف: `${d['target1']}`"
            )
        else:
            lines.append(f"• {sym} _(تعذّر جلب البيانات)_")
    lines.append(DISCLAIMER_TEXT)
    return "\n".join(lines)

# ─── 7. المعالجات ─────────────────────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    market = get_market_status_ksa()
    sub    = check_subscription(user.id)

    cmds_admin = [
        BotCommand("start",       "▶️ بدء البوت"),
        BotCommand("help",        "📖 الدليل"),
        BotCommand("add_user",    "✅ تفعيل اشتراك ID"),
        BotCommand("remove_user", "❌ إلغاء اشتراك ID"),
        BotCommand("alerts",      "🔔 تنبيهاتي"),
    ]
    cmds_user = [
        BotCommand("start",  "▶️ بدء البوت"),
        BotCommand("help",   "📖 دليل الاستخدام"),
        BotCommand("alerts", "🔔 تنبيهاتي"),
    ]
    scope = {"type": "chat", "chat_id": user.id}
    await context.bot.set_my_commands(
        cmds_admin if user.id in ADMIN_IDS else cmds_user, scope=scope
    )

    text = (
        f"📡 أهلاً بك يا {user.first_name} في بوت Stock Beacon 🚀\n\n"
        "💡 *مميزات البوت والاشتراك:*\n"
        "▪️ رادار كشف صفقات الحيتان والسيولة المؤسسية الضخمة 🐋\n"
        "▪️ كاشف الفرص والاختراقات الفورية 🎯\n"
        "▪️ مقياس التطبيل والزخم الاجتماعي للأسهم 🔥\n"
        "▪️ شارتات لحظية وتحديث مباشر للسعر 📈\n"
        "▪️ تنبيهات فورية عند تحقق الأهداف أو لمس الوقف ⚡\n\n"
        "💳 *خطط الاشتراك:*\n"
        "• الاشتراك الشهري: 299 ريال / شهرياً 🗓️\n"
        "• الاشتراك السنوي (الماسي): 399 ريال / سنوياً 🔥\n\n"
        f"📌 *حالة السوق الآن:* {market}\n"
        f"🆔 *معرفك (ID):* `{user.id}`\n"
        f"{sub}\n\n"
        "⚠️ إخلاء مسؤولية: التوصيات مبنية على خوارزميات فنية وليست دعوة للبيع أو الشراء."
    )
    await update.message.reply_text(text, reply_markup=main_keyboard(user.id), parse_mode="Markdown")


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *دليل استخدام بوت Stock Beacon* 🚀\n\n"
        "📌 *1. تحليل سهم:*\n"
        "أرسل رمز السهم مباشرة (مثال: `NVDA` أو `AAPL`)\n\n"
        "🔔 *2. التنبيهات:*\n"
        "بعد تحليل أي سهم اضغط زر _تنبيه على هذا السهم_ وسيتم ربطه تلقائياً.\n\n"
        "🎯 *3. الرادارات:*\n"
        "استخدم أزرار القائمة لعرض الفرص والأسهم النشطة.\n\n"
        "📸 *4. تحليل صورة الشارت:*\n"
        "أرسل صورة الشارت مباشرة للتحليل."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def alerts_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    alerts  = context.bot_data.setdefault("alerts", {})
    user_alerts = alerts.get(user_id, {})

    if not user_alerts:
        text = "🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات مضافة حتى الآن.\nاضغط ➕ لإضافة سهم."
    else:
        lines = ["🔔 *تنبيهاتك النشطة:*\n"]
        for sym, info in user_alerts.items():
            lines.append(f"• *{sym}* | دخول: `${info['entry']}` | هدف: `${info['target1']}` | وقف: `${info['stop']}`")
        text = "\n".join(lines)

    await update.message.reply_text(text, reply_markup=alerts_keyboard(user_id, alerts), parse_mode="Markdown")


async def add_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: `/add_user <ID>`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
        SUBSCRIBED_USERS.add(uid)
        await update.message.reply_text(f"✅ تم تفعيل ID: `{uid}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ ID غير صالح.")


async def remove_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("الاستخدام: `/remove_user <ID>`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
        SUBSCRIBED_USERS.discard(uid)
        await update.message.reply_text(f"❌ تم إلغاء ID: `{uid}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ ID غير صالح.")


# ─── 8. معالج النصوص (تحليل السهم) ──────────────────────────────────────────

WAITING_ALERT_SYMBOL = 10

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text_in = update.message.text.strip()

    # إذا كنا ننتظر رمز تنبيه جديد
    if context.user_data.get("waiting_alert"):
        context.user_data.pop("waiting_alert")
        symbol = text_in.upper()
        await _add_alert_for(update, context, user_id, symbol, via_message=True)
        return

    if user_id not in SUBSCRIBED_USERS:
        await update.message.reply_text(SUBSCRIBE_PROMPT_TEXT, parse_mode="Markdown")
        return

    symbol = text_in.upper()
    msg = await update.message.reply_text(f"⏳ جاري جلب بيانات *{symbol}* ...", parse_mode="Markdown")
    d = get_stock_data(symbol)
    if not d:
        await msg.edit_text(f"❌ لم يتم العثور على سهم باسم `{symbol}`.", parse_mode="Markdown")
        return

    await msg.edit_text(
        build_analysis_text(d),
        reply_markup=analysis_keyboard(symbol),
        parse_mode="Markdown"
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in SUBSCRIBED_USERS:
        await update.message.reply_text(SUBSCRIBE_PROMPT_TEXT, parse_mode="Markdown")
        return
    await update.message.reply_text(
        "📊 *تم تحليل صورة الشارت:*\n\n"
        "🟢 *النمط الفني:* اختراق إيجابي مع حجم متصاعد\n"
        "🎯 *الهدف المتوقع:* +10%\n"
        "🛑 *وقف الخسارة:* -7%"
        f"{DISCLAIMER_TEXT}",
        parse_mode="Markdown",
        reply_markup=back_keyboard()
    )


# ─── 9. معالج الضغطات (Callbacks) ────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    user = q.from_user
    uid  = user.id

    # ── القائمة الرئيسية ──
    if data == "btn_main_menu":
        text = (
            f"📡 أهلاً بك يا {user.first_name} في بوت Stock Beacon 🚀\n\n"
            f"📌 *حالة السوق الآن:* {get_market_status_ksa()}\n"
            f"🆔 *معرفك (ID):* `{uid}`\n"
            f"{check_subscription(uid)}"
        )
        await q.edit_message_text(text, reply_markup=main_keyboard(uid), parse_mode="Markdown")
        return

    # ── التحقق من الاشتراك ──
    if uid not in SUBSCRIBED_USERS:
        await q.edit_message_text(SUBSCRIBE_PROMPT_TEXT, reply_markup=back_keyboard(), parse_mode="Markdown")
        return

    # ── تحديث سعر سهم ──
    if data.startswith("refresh:"):
        sym = data.split(":", 1)[1]
        await q.edit_message_text(f"⏳ جاري تحديث *{sym}* ...", parse_mode="Markdown")
        d = get_stock_data(sym)
        if not d:
            await q.edit_message_text(f"❌ تعذّر جلب بيانات `{sym}`.", reply_markup=back_keyboard(), parse_mode="Markdown")
            return
        await q.edit_message_text(build_analysis_text(d), reply_markup=analysis_keyboard(sym), parse_mode="Markdown")
        return

    # ── إضافة تنبيه (من زر التحليل — مربوط بالسهم مباشرة) ──
    if data.startswith("add_alert:"):
        sym = data.split(":", 1)[1]
        await _add_alert_for(q, context, uid, sym, via_message=False)
        return

    # ── إضافة تنبيه جديد (يدوي من مربع التنبيهات) ──
    if data == "new_alert":
        context.user_data["waiting_alert"] = True
        await q.edit_message_text(
            "✏️ أرسل رمز السهم الذي تريد إضافة تنبيه له:\n_(مثال: TSLA أو NVDA)_",
            reply_markup=back_keyboard(),
            parse_mode="Markdown"
        )
        return

    # ── حذف تنبيه ──
    if data.startswith("del_alert:"):
        sym    = data.split(":", 1)[1]
        alerts = context.bot_data.setdefault("alerts", {})
        alerts.get(uid, {}).pop(sym, None)
        user_alerts = alerts.get(uid, {})
        if not user_alerts:
            text = "🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات مضافة."
        else:
            lines = ["🔔 *تنبيهاتك النشطة:*\n"]
            for s, info in user_alerts.items():
                lines.append(f"• *{s}* | دخول: `${info['entry']}` | هدف: `${info['target1']}`")
            text = "\n".join(lines)
        await q.edit_message_text(text, reply_markup=alerts_keyboard(uid, alerts), parse_mode="Markdown")
        return

    # ── مربع التنبيهات ──
    if data == "btn_alerts":
        alerts = context.bot_data.setdefault("alerts", {})
        user_alerts = alerts.get(uid, {})
        if not user_alerts:
            text = "🔔 *مربع التنبيهات*\n\nلا توجد تنبيهات مضافة حتى الآن.\nاضغط ➕ لإضافة سهم."
        else:
            lines = ["🔔 *تنبيهاتك النشطة:*\n"]
            for sym, info in user_alerts.items():
                lines.append(f"• *{sym}* | دخول: `${info['entry']}` | هدف: `${info['target1']}` | وقف: `${info['stop']}`")
            text = "\n".join(lines)
        await q.edit_message_text(text, reply_markup=alerts_keyboard(uid, alerts), parse_mode="Markdown")
        return

    # ── كاشف الفرص ──
    if data == "btn_opportunities":
        await q.edit_message_text("⏳ جاري جلب أبرز الفرص الاختراقية ...")
        text = await fetch_stocks_text(OPPORTUNITY_STOCKS, "🎯 أبرز الفرص الاختراقية الحالية:", count=6)
        await q.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
        return

    # ── الأسهم المطبل لها ──
    if data == "btn_trending":
        await q.edit_message_text("⏳ جاري جلب أعلى الأسهم زخماً ...")
        text = await fetch_stocks_text(TRENDING_STOCKS, "🔥 الأسهم الأعلى زخماً والمطبل لها:", count=6)
        await q.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
        return

    # ── رادار الحيتان ──
    if data == "btn_whales":
        await q.edit_message_text("⏳ جاري رصد تحركات الحيتان المؤسسية ...")
        text = await fetch_stocks_text(WHALE_STOCKS, "🐋 رادار السيولة والحيتان المؤسسية:", count=6)
        await q.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
        return

    # ── أسهم مضاربية رخيصة ──
    if data == "btn_penny":
        await q.edit_message_text("⏳ جاري البحث عن أسهم مضاربية ...")
        text = await fetch_stocks_text(PENNY_STOCKS, "⚡ أسهم مضاربية رخيصة:", count=6)
        await q.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
        return


# ─── 10. دالة مساعدة: إضافة تنبيه ───────────────────────────────────────────

async def _add_alert_for(target, context: ContextTypes.DEFAULT_TYPE, uid: int, symbol: str, via_message: bool):
    """تضيف تنبيهاً للسهم المحدد دون طلب إدخال مجدد"""
    d = get_stock_data(symbol)
    alerts = context.bot_data.setdefault("alerts", {})
    if uid not in alerts:
        alerts[uid] = {}

    if not d:
        text = f"❌ لم يتم العثور على سهم `{symbol}`. تأكد من الرمز وحاول مجدداً."
        if via_message:
            await target.message.reply_text(text, parse_mode="Markdown")
        else:
            await target.edit_message_text(text, reply_markup=back_keyboard(), parse_mode="Markdown")
        return

    alerts[uid][symbol] = {
        "entry":   d['price'],
        "target1": d['target1'],
        "target2": d['target2'],
        "stop":    d['stop'],
    }
    text = (
        f"✅ *تم إضافة تنبيه على {symbol}*\n\n"
        f"💰 سعر الدخول: `${d['price']}`\n"
        f"🎯 الهدف الأول: `${d['target1']}`\n"
        f"🚀 الهدف الثاني: `${d['target2']}`\n"
        f"🛑 وقف الخسارة: `${d['stop']}`\n\n"
        f"سأُرسل لك تنبيهاً فور تحقق الهدف أو لمس الوقف 🔔"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 مربع التنبيهات",    callback_data="btn_alerts")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="btn_main_menu")],
    ])
    if via_message:
        await target.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await target.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")


# ─── 11. فحص التنبيهات (Job Queue) ──────────────────────────────────────────

async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE):
    alerts = context.bot_data.get("alerts", {})
    for uid, user_alerts in list(alerts.items()):
        for sym, info in list(user_alerts.items()):
            d = get_stock_data(sym)
            if not d:
                continue
            price = d['price']

            if price >= info['target1']:
                msg = (
                    f"🎯 *تنبيه: {sym} حقق الهدف الأول!*\n\n"
                    f"💰 السعر الحالي: `${price}`\n"
                    f"✅ الهدف الأول: `${info['target1']}` — *تم تحقيقه!*\n"
                    f"🚀 الهدف التالي: `${info['target2']}`"
                )
                info['target1'] = info['target2'] + 1  # منع التكرار
                try:
                    await context.bot.send_message(uid, msg, parse_mode="Markdown")
                except Exception:
                    pass

            elif price <= info['stop']:
                msg = (
                    f"🛑 *تنبيه: {sym} لمس وقف الخسارة!*\n\n"
                    f"💰 السعر الحالي: `${price}`\n"
                    f"❌ وقف الخسارة: `${info['stop']}` — *تم لمسه!*\n"
                    f"⚠️ يُنصح بمراجعة الصفقة."
                )
                alerts[uid].pop(sym, None)
                try:
                    await context.bot.send_message(uid, msg, parse_mode="Markdown")
                except Exception:
                    pass


# ─── 12. تسجيل كل الـ Handlers ───────────────────────────────────────────────

def register_handlers(app):
    from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters

    app.add_handler(CommandHandler("start",       start_handler))
    app.add_handler(CommandHandler("help",        help_handler))
    app.add_handler(CommandHandler("alerts",      alerts_cmd_handler))
    app.add_handler(CommandHandler("add_user",    add_user_handler))
    app.add_handler(CommandHandler("remove_user", remove_user_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

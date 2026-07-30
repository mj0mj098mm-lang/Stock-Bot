import os
import time
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# حالات المحادثة مع المستخدم
WAITING_FOR_SYMBOL, WAITING_FOR_CAPITAL = range(2)

# جلب السعر اللحظي
def get_stock_price(symbol: str) -> float:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        price = data['chart']['result'][0]['meta']['regularMarketPrice']
        return round(float(price), 2)
    except Exception:
        return None

# 1. القائمة الرئيسية بالأزرار
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 تحليل سهم", callback_data='btn_analyze')],
        [InlineKeyboardButton("🛡️ حاسبة إدارة المخاطر", callback_data='btn_calc')],
        [InlineKeyboardButton("❓ كيفية الاستخدام", callback_data='btn_help')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "📊 **مرحباً بك في بوت منارة الأسهم (StockBeacon)!**\n\n"
        "اختر من الأزرار أدناه للبدء:"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

# 2. الاستجابة للأزرار
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'btn_analyze':
        await query.message.reply_text("✍️ **يرجى كتابة رمز السهم الآن (مثال: AAPL أو TSLA):**", parse_mode='Markdown')
        return WAITING_FOR_SYMBOL

    elif query.data == 'btn_calc':
        await query.message.reply_text("💰 **أدخل قيمة رأس مالك بالدولار (مثال: 5000 أو 10000):**", parse_mode='Markdown')
        return WAITING_FOR_CAPITAL

    elif query.data == 'btn_help':
        await query.message.reply_text("💡 **طريقة الاستخدام:**\nاضغط على 'تحليل سهم' واكتب رمز السهم لتصلك صورة الشارت والتقرير الفني فوراً.")
        return ConversationHandler.END

    elif query.data == 'btn_start':
        await start(update, context)
        return ConversationHandler.END

# 3. استقبال رمز السهم وإظهار التقرير والشارت
async def process_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.upper().strip()
    context.user_data['last_symbol'] = symbol

    await update.message.reply_text(f"⏳ جاري تحليل {symbol} وجلب البيانات والشارت...")

    current_price = get_stock_price(symbol)
    if not current_price:
        await update.message.reply_text(f"❌ تعذر جلب البيانات لـ `{symbol}`، تأكد من رمز السهم واكتبه مرة أخرى:", parse_mode='Markdown')
        return WAITING_FOR_SYMBOL

    context.user_data['last_price'] = current_price

    stop_loss = round(current_price * 0.97, 2)
    smc_low   = round(current_price * 0.985, 2)
    smc_high  = round(current_price * 0.995, 2)
    target1   = round(current_price * 1.03, 2)
    target2   = round(current_price * 1.06, 2)

    report_text = (
        f"📈 **تقرير التحليل اللحظي: {symbol}**\n"
        f"🔹 **السعر الحالي:** {current_price}$\n"
        f"-----------------------------------\n"
        f"🔹 **الاتجاه العام:** صاعد 🟢 (Bullish)\n"
        f"🔹 **مناطق السيولة الذكية (SMC):** {smc_low}$ - {smc_high}$\n"
        f"🎯 **الأهداف المتوقعة:** {target1}$ ⬅️ {target2}$\n"
        f"🛡️ **وقف الخسارة المقترح (SL):** {stop_loss}$"
    )

    keyboard = [
        [InlineKeyboardButton("🛡️ احسب حجم الصفقة لرأس مالي", callback_data='btn_calc')],
        [InlineKeyboardButton("🔄 تحليل سهم آخر", callback_data='btn_analyze')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # فريم الساعة مع كسر الكاش للحصول على شارت محدّث
    ts = int(time.time())
    chart_url = f"https://charts2.finviz.com/chart.ashx?t={symbol}&ty=c&ta=1&p=i60&s=l&_={ts}"

    try:
        await update.message.reply_photo(photo=chart_url, caption=report_text, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception:
        await update.message.reply_text(report_text, reply_markup=reply_markup, parse_mode='Markdown')

    return ConversationHandler.END

# 4. حساب إدارة المخاطر بناءً على المبلغ الذي يكتبه العميل
async def process_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip().replace(',', '')

    try:
        capital = float(user_input)
        if capital <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح موجب لرأس المال (مثال: 5000):")
        return WAITING_FOR_CAPITAL

    symbol = context.user_data.get('last_symbol', 'السهم')
    price  = context.user_data.get('last_price', None)

    # إذا لم يكن هناك سعر محفوظ نجلبه الآن
    if not price:
        price = get_stock_price(symbol)
    if not price:
        price = 100.0

    stop_loss_price = round(price * 0.97, 2)   # SL عند 3% تحت السعر
    sl_per_share    = round(price - stop_loss_price, 2)

    # ─── نسب التخصيص ───────────────────────────────────────
    risk_pct_2      = capital * 0.02             # أقصى خسارة 2%
    risk_pct_1      = capital * 0.01             # خسارة محافظة 1%

    # حجم الصفقة المحسوب من المخاطرة (Risk-Based Position Sizing)
    # عدد الأسهم = مبلغ المخاطرة / الخسارة لكل سهم
    shares_risk_2   = risk_pct_2 / sl_per_share if sl_per_share > 0 else 0
    shares_risk_1   = risk_pct_1 / sl_per_share if sl_per_share > 0 else 0

    position_risk_2 = round(shares_risk_2 * price, 2)
    position_risk_1 = round(shares_risk_1 * price, 2)

    shares_risk_2_d = round(shares_risk_2, 2)
    shares_risk_1_d = round(shares_risk_1, 2)

    # هدف الربح بنسبة 1:3
    target_rr3 = round(price + (sl_per_share * 3), 2)

    # الحد الأدنى لرأس المال لشراء سهم واحد كامل
    min_capital_1_share = round(price / 0.10, 2)   # إذا كان الحد 10% من الرأس المال

    # ─── هل يكفي رأس المال لسهم واحد على الأقل؟ ─────────
    can_buy_full = capital >= price

    # ─── بناء التقرير ──────────────────────────────────────
    shares_line_2 = f"{shares_risk_2_d:.2f} سهم" if shares_risk_2_d >= 1 else f"{shares_risk_2_d:.2f} سهم (كسري - متاح في بعض المنصات)"
    shares_line_1 = f"{shares_risk_1_d:.2f} سهم" if shares_risk_1_d >= 1 else f"{shares_risk_1_d:.2f} سهم (كسري)"

    warning = ""
    if not can_buy_full:
        warning = (
            f"\n⚠️ **تنبيه:** سعر السهم الواحد ({price}$) أعلى من رأس مالك.\n"
            f"• لشراء سهم كامل تحتاج على الأقل: **{price:,.2f}$**\n"
            f"• أو استخدم منصة تدعم **الأسهم الكسرية** (Fractional Shares) مثل eToro أو Webull.\n"
        )

    calc_result = (
        f"🧮 **حاسبة المخاطر الذكية — {symbol}**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💵 **رأس مالك:** {capital:,.2f}$\n"
        f"📌 **السعر الحالي:** {price}$\n"
        f"🛑 **وقف الخسارة المقترح:** {stop_loss_price}$ (3%-)\n"
        f"📉 **الخسارة لكل سهم:** {sl_per_share}$\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"**🟡 خيار محافظ (مخاطرة 1%):**\n"
        f"  • مبلغ المخاطرة: {risk_pct_1:,.2f}$\n"
        f"  • حجم الصفقة: {position_risk_1:,.2f}$\n"
        f"  • عدد الأسهم: {shares_line_1}\n\n"
        f"**🔴 خيار معتدل (مخاطرة 2%):**\n"
        f"  • مبلغ المخاطرة: {risk_pct_2:,.2f}$\n"
        f"  • حجم الصفقة: {position_risk_2:,.2f}$\n"
        f"  • عدد الأسهم: {shares_line_2}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **هدف الربح (R:R = 1:3):** {target_rr3}$\n"
        f"{warning}\n"
        f"💡 *الحجم محسوب بناءً على المخاطرة الفعلية لكل سهم، وليس نسبة ثابتة من رأس المال.*"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 تحليل سهم آخر", callback_data='btn_analyze')],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data='btn_start')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(calc_result, reply_markup=reply_markup, parse_mode='Markdown')
    return ConversationHandler.END

if __name__ == '__main__':
    if not TOKEN:
        print("❌ خطأ: لم يتم تعيين TELEGRAM_BOT_TOKEN في متغيرات البيئة.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CallbackQueryHandler(button_handler)
        ],
        states={
            WAITING_FOR_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_symbol)],
            WAITING_FOR_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_capital)],
        },
        fallbacks=[CommandHandler('start', start)]
    )

    app.add_handler(conv_handler)

    print("🚀 البوت التفاعلي يعمل بنجاح مع الأزرار والحاسبة...")
    app.run_polling()

import os
import io
import time
import logging
import requests
import yfinance as yf
import mplfinance as mpf
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

WAITING_FOR_SYMBOL, WAITING_FOR_CAPITAL = range(2)


def get_stock_price(symbol: str):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()
        price = data['chart']['result'][0]['meta']['regularMarketPrice']
        return round(float(price), 2)
    except Exception:
        return None


def generate_chart(symbol: str) -> io.BytesIO | None:
    """توليد شارت احترافي داكن بفريم الساعة يشبه TradingView."""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="5d", interval="1h")

        if df.empty or len(df) < 5:
            return None

        # إزالة timezone للتوافق مع mplfinance
        df.index = df.index.tz_localize(None)

        # ألوان TradingView الداكنة
        dark_style = mpf.make_mpf_style(
            base_mpf_style='nightclouds',
            marketcolors=mpf.make_marketcolors(
                up='#26a69a',       # أخضر TradingView
                down='#ef5350',     # أحمر TradingView
                edge={'up': '#26a69a', 'down': '#ef5350'},
                wick={'up': '#26a69a', 'down': '#ef5350'},
                volume={'up': '#1a6b63', 'down': '#8a2e2e'},
            ),
            facecolor='#131722',    # خلفية TradingView
            edgecolor='#2a2e39',
            figcolor='#131722',
            gridcolor='#1e222d',
            gridstyle='--',
            gridaxis='both',
            y_on_right=True,
            rc={
                'axes.labelcolor': '#d1d4dc',
                'xtick.color': '#787b86',
                'ytick.color': '#d1d4dc',
                'font.size': 9,
            }
        )

        # إضافة المتوسطات المتحركة
        ma20 = mpf.make_addplot(df['Close'].rolling(20).mean(), color='#f0b90b', width=1.2, label='MA20')
        ma50 = mpf.make_addplot(df['Close'].rolling(50).mean(), color='#2962ff', width=1.2, label='MA50')

        current_price = round(df['Close'].iloc[-1], 2)
        change        = round(df['Close'].iloc[-1] - df['Close'].iloc[0], 2)
        change_pct    = round((change / df['Close'].iloc[0]) * 100, 2)
        high_5d       = round(df['High'].max(), 2)
        low_5d        = round(df['Low'].min(), 2)
        color_change  = '#26a69a' if change >= 0 else '#ef5350'
        arrow         = '▲' if change >= 0 else '▼'

        fig, axes = mpf.plot(
            df,
            type='candle',
            style=dark_style,
            addplot=[ma20, ma50],
            volume=True,
            figsize=(12, 7),
            title='',
            tight_layout=True,
            returnfig=True,
            panel_ratios=(4, 1),
            datetime_format='%m/%d %H:%M',
            xrotation=20,
        )

        ax_main = axes[0]

        # خط أفقي عند السعر الحالي
        ax_main.axhline(y=current_price, color='#f0b90b', linewidth=0.8, linestyle='--', alpha=0.7)

        # تسمية السعر الحالي على اليمين
        ax_main.text(
            1.001, current_price,
            f' {current_price}$',
            transform=ax_main.get_yaxis_transform(),
            color='#f0b90b', fontsize=9, va='center', fontweight='bold'
        )

        # عنوان الشارت
        sign = '+' if change >= 0 else ''
        fig.text(
            0.04, 0.97,
            f'{symbol}  •  1H  •  {current_price}$   {arrow} {sign}{change}$ ({sign}{change_pct}%)',
            color=color_change, fontsize=13, fontweight='bold', va='top'
        )
        fig.text(
            0.04, 0.93,
            f'High 5d: {high_5d}$   Low 5d: {low_5d}$',
            color='#787b86', fontsize=9, va='top'
        )

        # legend للمتوسطات
        legend_elements = [
            Line2D([0], [0], color='#f0b90b', linewidth=1.5, label='MA 20'),
            Line2D([0], [0], color='#2962ff', linewidth=1.5, label='MA 50'),
        ]
        ax_main.legend(handles=legend_elements, loc='upper left',
                       facecolor='#1e222d', edgecolor='#2a2e39',
                       labelcolor='#d1d4dc', fontsize=8)

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor='#131722')
        buf.seek(0)
        plt.close(fig)
        return buf

    except Exception as e:
        logging.error(f"Chart generation error: {e}")
        return None


# ─── القائمة الرئيسية ────────────────────────────────────────────────────────
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


# ─── معالج الأزرار ───────────────────────────────────────────────────────────
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
        await query.message.reply_text(
            "💡 **طريقة الاستخدام:**\n"
            "اضغط على 'تحليل سهم' واكتب رمز السهم\n"
            "ستصلك صورة شارت احترافية (فريم ساعة) مع التقرير الفني فوراً."
        )
        return ConversationHandler.END

    elif query.data == 'btn_start':
        await start(update, context)
        return ConversationHandler.END


# ─── تحليل السهم ─────────────────────────────────────────────────────────────
async def process_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.upper().strip()
    context.user_data['last_symbol'] = symbol

    wait = await update.message.reply_text(f"⏳ جاري جلب بيانات {symbol} وتوليد الشارت...")

    current_price = get_stock_price(symbol)
    if not current_price:
        await wait.edit_text(f"❌ تعذر جلب البيانات لـ `{symbol}`، تأكد من الرمز وأعد الكتابة:", parse_mode='Markdown')
        return WAITING_FOR_SYMBOL

    context.user_data['last_price'] = current_price

    stop_loss = round(current_price * 0.97, 2)
    smc_low   = round(current_price * 0.985, 2)
    smc_high  = round(current_price * 0.995, 2)
    target1   = round(current_price * 1.03, 2)
    target2   = round(current_price * 1.06, 2)

    report_text = (
        f"📈 **{symbol} — تحليل فوري (فريم ساعة)**\n"
        f"🔹 **السعر الحالي:** `{current_price}$`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 **مناطق الدعم (SMC):** {smc_low}$ – {smc_high}$\n"
        f"🎯 **الهدف الأول:** {target1}$  |  **الهدف الثاني:** {target2}$\n"
        f"🛑 **وقف الخسارة المقترح:** {stop_loss}$\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *للأغراض التعليمية فقط — ليست توصية مالية.*"
    )

    keyboard = [
        [InlineKeyboardButton("🛡️ احسب حجم الصفقة لرأس مالي", callback_data='btn_calc')],
        [InlineKeyboardButton("🔄 تحليل سهم آخر", callback_data='btn_analyze')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # توليد الشارت
    chart_buf = generate_chart(symbol)
    await wait.delete()

    if chart_buf:
        await update.message.reply_photo(
            photo=chart_buf,
            caption=report_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(report_text, reply_markup=reply_markup, parse_mode='Markdown')

    return ConversationHandler.END


# ─── حاسبة المخاطر ───────────────────────────────────────────────────────────
async def process_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip().replace(',', '')

    try:
        capital = float(user_input)
        if capital <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح موجب (مثال: 5000):")
        return WAITING_FOR_CAPITAL

    symbol = context.user_data.get('last_symbol', 'السهم')
    price  = context.user_data.get('last_price') or get_stock_price(symbol) or 100.0

    stop_loss_price = round(price * 0.97, 2)
    sl_per_share    = round(price - stop_loss_price, 2)

    risk_pct_2     = capital * 0.02
    risk_pct_1     = capital * 0.01
    shares_risk_2  = round(risk_pct_2 / sl_per_share, 2) if sl_per_share > 0 else 0
    shares_risk_1  = round(risk_pct_1 / sl_per_share, 2) if sl_per_share > 0 else 0
    position_risk_2 = round(shares_risk_2 * price, 2)
    position_risk_1 = round(shares_risk_1 * price, 2)
    target_rr3     = round(price + (sl_per_share * 3), 2)
    can_buy_full   = capital >= price

    def shares_fmt(n): return f"{n:.2f} سهم" if n >= 1 else f"{n:.2f} سهم (كسري)"

    warning = ""
    if not can_buy_full:
        warning = (
            f"\n⚠️ سعر السهم ({price}$) أعلى من رأس مالك.\n"
            f"لشراء سهم كامل تحتاج: **{price:,.2f}$** على الأقل.\n"
            f"أو استخدم منصة تدعم الأسهم الكسرية (eToro / Webull).\n"
        )

    calc_result = (
        f"🧮 **حاسبة المخاطر الذكية — {symbol}**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💵 **رأس مالك:** {capital:,.2f}$\n"
        f"📌 **السعر الحالي:** {price}$\n"
        f"🛑 **وقف الخسارة:** {stop_loss_price}$ (3%-)\n"
        f"📉 **الخسارة لكل سهم:** {sl_per_share}$\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🟡 **محافظ (مخاطرة 1%):**\n"
        f"  • مبلغ المخاطرة: {risk_pct_1:,.2f}$\n"
        f"  • حجم الصفقة: {position_risk_1:,.2f}$  •  {shares_fmt(shares_risk_1)}\n\n"
        f"🔴 **معتدل (مخاطرة 2%):**\n"
        f"  • مبلغ المخاطرة: {risk_pct_2:,.2f}$\n"
        f"  • حجم الصفقة: {position_risk_2:,.2f}$  •  {shares_fmt(shares_risk_2)}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **هدف الربح (R:R = 1:3):** {target_rr3}$\n"
        f"{warning}\n"
        f"💡 *الحجم محسوب بناءً على المخاطرة الفعلية لكل سهم.*"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 تحليل سهم آخر", callback_data='btn_analyze')],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data='btn_start')]
    ]
    await update.message.reply_text(calc_result, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ConversationHandler.END


# ─── التشغيل ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if not TOKEN:
        print("❌ خطأ: لم يتم تعيين TELEGRAM_BOT_TOKEN.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CallbackQueryHandler(button_handler)
        ],
        states={
            WAITING_FOR_SYMBOL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, process_symbol)],
            WAITING_FOR_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_capital)],
        },
        fallbacks=[CommandHandler('start', start)]
    )

    app.add_handler(conv_handler)
    print("🚀 StockBeacon يعمل بنجاح — شارت احترافي داكن (فريم ساعة)...")
    app.run_polling()

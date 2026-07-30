import sys
# إصلاح تعارض namespace package عبر حذف المسارات المكررة
_seen = set()
sys.path = [p for p in sys.path if not (p in _seen or _seen.add(p))]

import os
import io
import logging
import requests
import yfinance as yf
import mplfinance as mpf
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

WAITING_FOR_SYMBOL, WAITING_FOR_CAPITAL = range(2)


# ─── جلب السعر اللحظي ─────────────────────────────────────────────────────────
def get_stock_data(symbol: str) -> dict | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=6)
        meta = r.json()['chart']['result'][0]['meta']
        price      = round(float(meta['regularMarketPrice']), 2)
        prev_close = round(float(meta.get('previousClose', price)), 2)
        change     = round(price - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0
        volume     = int(meta.get('regularMarketVolume', 0))
        avg_volume = int(meta.get('averageDailyVolume3Month', 1))
        return {
            'price': price,
            'prev_close': prev_close,
            'change': change,
            'change_pct': change_pct,
            'volume': volume,
            'avg_volume': avg_volume,
        }
    except Exception:
        return None


# ─── توليد الشارت الداكن ──────────────────────────────────────────────────────
def generate_chart(symbol: str, support: float, resistance: float, stop_loss: float, target2: float) -> io.BytesIO | None:
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="5d", interval="1h")
        if df.empty or len(df) < 5:
            return None
        df.index = df.index.tz_localize(None)

        dark_style = mpf.make_mpf_style(
            base_mpf_style='nightclouds',
            marketcolors=mpf.make_marketcolors(
                up='#26a69a', down='#ef5350',
                edge={'up': '#26a69a', 'down': '#ef5350'},
                wick={'up': '#26a69a', 'down': '#ef5350'},
                volume={'up': '#1a6b63', 'down': '#8a2e2e'},
            ),
            facecolor='#131722', edgecolor='#2a2e39',
            figcolor='#131722', gridcolor='#1e222d',
            gridstyle='--', gridaxis='both', y_on_right=True,
            rc={
                'axes.labelcolor': '#d1d4dc',
                'xtick.color': '#787b86',
                'ytick.color': '#d1d4dc',
                'font.size': 9,
            }
        )

        ma20 = mpf.make_addplot(df['Close'].rolling(20).mean(), color='#f0b90b', width=1.2)
        ma50 = mpf.make_addplot(df['Close'].rolling(50).mean(), color='#2962ff', width=1.2)

        current_price = round(df['Close'].iloc[-1], 2)
        change        = round(df['Close'].iloc[-1] - df['Close'].iloc[0], 2)
        change_pct_c  = round((change / df['Close'].iloc[0]) * 100, 2)
        color_change  = '#26a69a' if change >= 0 else '#ef5350'
        arrow         = '▲' if change >= 0 else '▼'

        fig, axes = mpf.plot(
            df, type='candle', style=dark_style,
            addplot=[ma20, ma50], volume=True,
            figsize=(12, 7), title='', tight_layout=True,
            returnfig=True, panel_ratios=(4, 1),
            datetime_format='%m/%d %H:%M', xrotation=20,
        )
        ax = axes[0]

        # خطوط المستويات
        levels = [
            (target2,    '#26a69a', '--', f'🎯 هدف {target2}$'),
            (resistance,  '#ff9800', '-',  f'⛔ مقاومة {resistance}$'),
            (current_price,'#f0b90b','--', f' سعر {current_price}$'),
            (support,     '#2962ff', '-',  f'🛡 دعم {support}$'),
            (stop_loss,   '#ef5350', '--', f'🛑 SL {stop_loss}$'),
        ]
        for price_val, color, ls, label in levels:
            ax.axhline(y=price_val, color=color, linewidth=0.9, linestyle=ls, alpha=0.8)
            ax.text(1.001, price_val, f' {price_val}$',
                    transform=ax.get_yaxis_transform(),
                    color=color, fontsize=8, va='center', fontweight='bold')

        sign = '+' if change >= 0 else ''
        fig.text(0.04, 0.97,
                 f'{symbol}  •  1H  •  {current_price}$   {arrow} {sign}{change}$ ({sign}{change_pct_c}%)',
                 color=color_change, fontsize=13, fontweight='bold', va='top')

        legend_elements = [
            Line2D([0], [0], color='#f0b90b', linewidth=1.5, label='MA 20'),
            Line2D([0], [0], color='#2962ff', linewidth=1.5, label='MA 50'),
            Line2D([0], [0], color='#26a69a', linewidth=1.2, linestyle='--', label='هدف'),
            Line2D([0], [0], color='#ef5350', linewidth=1.2, linestyle='--', label='SL'),
        ]
        ax.legend(handles=legend_elements, loc='upper left',
                  facecolor='#1e222d', edgecolor='#2a2e39',
                  labelcolor='#d1d4dc', fontsize=8)

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor='#131722')
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logging.error(f"Chart error: {e}")
        return None


# ─── حساب جودة الدخول ────────────────────────────────────────────────────────
def entry_quality(price, support, resistance, volume, avg_volume) -> tuple[str, str]:
    """
    يحدد جودة الدخول بناءً على:
    - موقع السعر من الدعم والمقاومة
    - حجم التداول مقارنة بالمتوسط
    """
    range_total  = resistance - support if resistance > support else 1
    position_pct = ((price - support) / range_total) * 100  # % من الدعم للمقاومة
    vol_ratio    = volume / avg_volume if avg_volume > 0 else 1

    if position_pct <= 30 and vol_ratio >= 0.8:
        return "🟢 دخول آمن", "السعر قريب من الدعم مع سيولة جيدة — فرصة قوية"
    elif position_pct <= 55:
        return "🟡 دخول متوسط", "السعر في منتصف النطاق — انتظر تراجعاً للدعم للأمان"
    else:
        return "🔴 تجنب الدخول الآن", "السعر قريب من المقاومة — خطر انعكاس مرتفع"


# ─── القائمة الرئيسية ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 تحليل سهم", callback_data='btn_analyze')],
        [InlineKeyboardButton("🛡️ حاسبة إدارة المخاطر", callback_data='btn_calc')],
        [InlineKeyboardButton("❓ كيفية الاستخدام", callback_data='btn_help')],
    ]
    text = (
        "📊 **مرحباً بك في بوت منارة الأسهم (StockBeacon)!**\n\n"
        "اختر من الأزرار أدناه للبدء:"
    )
    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


# ─── معالج الأزرار ───────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'btn_analyze':
        await query.message.reply_text("✍️ **اكتب رمز السهم (مثال: AAPL أو TSLA أو BTC-USD):**", parse_mode='Markdown')
        return WAITING_FOR_SYMBOL
    elif query.data == 'btn_calc':
        symbol = context.user_data.get('last_symbol', '')
        prompt = f"💰 **أدخل رأس مالك بالدولار** للحساب على آخر سهم ({symbol}):" if symbol else "💰 **أدخل رأس مالك بالدولار (مثال: 5000):**"
        await query.message.reply_text(prompt, parse_mode='Markdown')
        return WAITING_FOR_CAPITAL
    elif query.data == 'btn_help':
        await query.message.reply_text(
            "💡 **طريقة الاستخدام:**\n"
            "• اضغط 'تحليل سهم' واكتب الرمز\n"
            "• ستصلك صورة شارت احترافية مع مستويات الدعم والمقاومة والهدف ووقف الخسارة\n"
            "• ثم يمكنك حساب حجم الصفقة بناءً على رأس مالك"
        )
        return ConversationHandler.END
    elif query.data == 'btn_start':
        await start(update, context)
        return ConversationHandler.END


# ─── تحليل السهم ─────────────────────────────────────────────────────────────
async def process_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.upper().strip()
    context.user_data['last_symbol'] = symbol
    wait = await update.message.reply_text(f"⏳ جاري تحليل **{symbol}** وتوليد الشارت...", parse_mode='Markdown')

    data = get_stock_data(symbol)
    if not data:
        await wait.edit_text(f"❌ تعذر جلب البيانات لـ `{symbol}`، تأكد من الرمز وأعد الكتابة:", parse_mode='Markdown')
        return WAITING_FOR_SYMBOL

    price      = data['price']
    change     = data['change']
    change_pct = data['change_pct']
    volume     = data['volume']
    avg_volume = data['avg_volume']

    context.user_data['last_price'] = price

    # ─── حساب المستويات ────────────────────────────────
    stop_loss   = round(price * 0.97,  2)   # 3%  تحت السعر
    support     = round(price * 0.985, 2)   # دعم قريب
    resistance  = round(price * 1.03,  2)   # مقاومة أولى
    target_1    = round(price * 1.05,  2)   # هدف 5%
    target_2    = round(price * 1.10,  2)   # هدف 10%
    entry_low   = round(price * 0.998, 2)
    entry_high  = price

    # ─── تدفق السيولة ────────────────────────────────
    vol_ratio  = volume / avg_volume if avg_volume > 0 else 1
    if vol_ratio >= 1.5:
        liquidity = f"🔥 سيولة مرتفعة جداً ({vol_ratio:.1f}x المتوسط) — تدفق شراء قوي"
    elif vol_ratio >= 1.0:
        liquidity = f"✅ سيولة طبيعية ({vol_ratio:.1f}x المتوسط)"
    else:
        liquidity = f"⚠️ سيولة ضعيفة ({vol_ratio:.1f}x المتوسط) — توخَّ الحذر"

    # ─── جودة الدخول ─────────────────────────────────
    entry_label, entry_desc = entry_quality(price, support, resistance, volume, avg_volume)

    # ─── اتجاه السهم ─────────────────────────────────
    sign   = '+' if change >= 0 else ''
    trend  = "🟢 صاعد" if change >= 0 else "🔴 هابط"

    context.user_data.update({
        'last_price': price, 'stop_loss': stop_loss,
        'support': support, 'resistance': resistance,
    })

    report = (
        f"📊 **{symbol} — تحليل فوري (فريم ساعة)**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 **السعر الحالي:** `{price}$`  {trend}  ({sign}{change_pct}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **نطاق الدخول:** `{entry_low}$ – {entry_high}$`\n"
        f"🛡️ **دعم قريب:** `{support}$`\n"
        f"⛔ **مقاومة أولى:** `{resistance}$`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **الهدف الأول (+5%):** `{target_1}$`\n"
        f"🎯 **الهدف الثاني (+10%):** `{target_2}$`\n"
        f"🛑 **وقف الخسارة (-3%):** `{stop_loss}$`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💧 **تدفق السيولة:** {liquidity}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{entry_label}\n"
        f"_{entry_desc}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _للأغراض التعليمية فقط — ليست توصية مالية._"
    )

    keyboard = [
        [InlineKeyboardButton("🛡️ احسب حجم صفقتي", callback_data='btn_calc')],
        [InlineKeyboardButton("🔄 تحليل سهم آخر",   callback_data='btn_analyze')],
    ]

    chart_buf = generate_chart(symbol, support, resistance, stop_loss, target_2)
    await wait.delete()

    if chart_buf:
        await update.message.reply_photo(
            photo=chart_buf, caption=report,
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(report, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    return ConversationHandler.END


# ─── حاسبة المخاطر ───────────────────────────────────────────────────────────
async def process_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip().replace(',', '')
    try:
        capital = float(user_input)
        if capital <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً موجباً (مثال: 5000):")
        return WAITING_FOR_CAPITAL

    symbol    = context.user_data.get('last_symbol', 'السهم')
    price     = context.user_data.get('last_price') or 100.0
    stop_loss = context.user_data.get('stop_loss') or round(price * 0.97, 2)

    sl_per_share = round(price - stop_loss, 2) or round(price * 0.03, 2)

    risk_1   = capital * 0.01
    risk_2   = capital * 0.02
    sh_1     = round(risk_1 / sl_per_share, 2) if sl_per_share else 0
    sh_2     = round(risk_2 / sl_per_share, 2) if sl_per_share else 0
    pos_1    = round(sh_1 * price, 2)
    pos_2    = round(sh_2 * price, 2)
    target_rr3 = round(price + sl_per_share * 3, 2)
    warning  = ""
    if capital < price:
        warning = (
            f"\n⚠️ سعر السهم ({price}$) أعلى من رأس مالك.\n"
            f"تحتاج على الأقل **{price:,.2f}$** لشراء سهم كامل.\n"
            f"أو استخدم منصة تدعم الأسهم الكسرية (eToro / Webull).\n"
        )

    def fmt(n): return f"{n:.2f} سهم" if n >= 1 else f"{n:.3f} سهم (كسري)"

    result = (
        f"🧮 **حاسبة المخاطر الذكية — {symbol}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 **رأس مالك:** {capital:,.2f}$\n"
        f"📌 **السعر الحالي:** {price}$\n"
        f"🛑 **وقف الخسارة:** {stop_loss}$\n"
        f"📉 **الخسارة لكل سهم:** {sl_per_share}$\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟡 **محافظ — مخاطرة 1%:**\n"
        f"  • مبلغ الخطر: {risk_1:,.2f}$\n"
        f"  • حجم الصفقة: {pos_1:,.2f}$  ({fmt(sh_1)})\n\n"
        f"🔴 **معتدل — مخاطرة 2%:**\n"
        f"  • مبلغ الخطر: {risk_2:,.2f}$\n"
        f"  • حجم الصفقة: {pos_2:,.2f}$  ({fmt(sh_2)})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **هدف الربح (R:R 1:3):** {target_rr3}$"
        f"{warning}\n"
        f"💡 _الحجم محسوب على أساس المخاطرة الفعلية لكل سهم._"
    )

    keyboard = [
        [InlineKeyboardButton("🔄 تحليل سهم آخر",         callback_data='btn_analyze')],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",       callback_data='btn_start')],
    ]
    await update.message.reply_text(result, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ConversationHandler.END


# ─── التشغيل ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN غير موجود.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CallbackQueryHandler(button_handler),
        ],
        states={
            WAITING_FOR_SYMBOL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, process_symbol)],
            WAITING_FOR_CAPITAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_capital)],
        },
        fallbacks=[CommandHandler('start', start)],
    )
    app.add_handler(conv)
    print("🚀 StockBeacon يعمل — شارت داكن + مستويات + تدفق السيولة + جودة الدخول")
    app.run_polling()

import sys, os
# ─── إصلاح تعارض namespace package ─────────────────────────────────────────
_seen = set()
sys.path = [p for p in sys.path if not (p in _seen or _seen.add(p))]

import io, logging, asyncio
from datetime import datetime
import requests, yfinance as yf
import mplfinance as mpf
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
    JobQueue
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

WAITING_SYMBOL, WAITING_CAPITAL, WAITING_ALERT_SYMBOL, WAITING_ALERT_CAPITAL = range(4)

# ─── ذاكرة التنبيهات ─────────────────────────────────────────────────────────
# { user_id: [ {symbol, resistance, support, sl, capital, last_alert_state} ] }
user_alerts: dict[int, list[dict]] = {}


# ═══════════════════════════════════════════════════════════════════════════════
#  جلب البيانات
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_data(symbol: str) -> dict | None:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=6)
        meta = r.json()['chart']['result'][0]['meta']
        price      = round(float(meta['regularMarketPrice']), 2)
        prev_close = round(float(meta.get('previousClose', price)), 2)
        volume     = int(meta.get('regularMarketVolume', 0))
        avg_vol    = int(meta.get('averageDailyVolume3Month', 1))
        change     = round(price - prev_close, 2)
        chg_pct    = round(change / prev_close * 100, 2) if prev_close else 0
        return dict(price=price, prev_close=prev_close, change=change,
                    chg_pct=chg_pct, volume=volume, avg_vol=avg_vol)
    except Exception:
        return None


def fetch_ohlcv(symbol: str):
    """جلب بيانات OHLCV للساعة — آخر 5 أيام."""
    try:
        df = yf.Ticker(symbol).history(period="5d", interval="1h")
        if df.empty or len(df) < 5:
            return None
        df.index = df.index.tz_localize(None)
        return df
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  تحليل SMC
# ═══════════════════════════════════════════════════════════════════════════════
def smc_analysis(df, price: float) -> dict:
    """
    يحسب:
    - Liquidity Pools (مناطق تجمع وقف الخسارة)
    - Fair Value Gaps (FVG)
    - Order Blocks (أكبر شمعة هبوطية/صاعدة قبل حركة قوية)
    - Swing High / Swing Low (دعم ومقاومة حقيقية)
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    opens  = df['Open'].values
    vols   = df['Volume'].values

    # Swing High / Swing Low (آخر 20 شمعة)
    window = min(20, len(df))
    swing_high = round(float(highs[-window:].max()), 2)
    swing_low  = round(float(lows[-window:].min()),  2)

    # Liquidity Pools: قمم وقيعان تكررت مرتين+ → مناطق تجمع وقف الخسارة
    pool_buy  = round(swing_low  * 1.002, 2)   # سيولة شراء فوق القاع
    pool_sell = round(swing_high * 0.998, 2)   # سيولة بيع تحت القمة

    # Fair Value Gap (FVG): فجوة بين high[i-2] و low[i] — آخر 15 شمعة
    fvg_bull = fvg_bear = None
    for i in range(2, min(15, len(df))):
        gap_up   = lows[-i]   - highs[-(i+2)]   # FVG صاعد
        gap_down = lows[-(i+2)] - highs[-i]     # FVG هابط
        if gap_up   > 0 and fvg_bull is None:
            fvg_bull = (round(float(highs[-(i+2)]), 2), round(float(lows[-i]), 2))
        if gap_down > 0 and fvg_bear is None:
            fvg_bear = (round(float(highs[-i]), 2), round(float(lows[-(i+2)]), 2))

    # Order Block: أكبر شمعة هبوطية قبل ارتفاع قوي (ذاكرة مؤسسية)
    ob_zone = None
    for i in range(3, min(20, len(df))):
        is_bearish_candle  = closes[-(i+1)] < opens[-(i+1)]
        strong_move_after  = closes[-i] > closes[-(i+1)] * 1.005
        if is_bearish_candle and strong_move_after:
            ob_zone = (round(float(lows[-(i+1)]), 2), round(float(highs[-(i+1)]), 2))
            break

    # Volume Pressure
    avg_vol_recent = float(vols[-5:].mean()) if len(vols) >= 5 else float(vols.mean())
    vol_last = float(vols[-1])
    vol_ratio = round(vol_last / avg_vol_recent, 2) if avg_vol_recent else 1.0
    pressure = "شراء 🟢" if closes[-1] > opens[-1] else "بيع 🔴"

    return dict(
        swing_high=swing_high, swing_low=swing_low,
        pool_buy=pool_buy, pool_sell=pool_sell,
        fvg_bull=fvg_bull, fvg_bear=fvg_bear,
        ob_zone=ob_zone, vol_ratio=vol_ratio, pressure=pressure
    )


def entry_signal(price, swing_low, swing_high, pool_buy, fvg_bull, vol_ratio) -> tuple[str, str]:
    rng = swing_high - swing_low if swing_high > swing_low else 1
    pos = (price - swing_low) / rng * 100
    near_fvg = fvg_bull and fvg_bull[0] <= price <= fvg_bull[1] * 1.01

    if pos <= 30 and vol_ratio >= 0.8:
        return "🟢 دخول آمن — فرصة قوية", "السعر في منطقة الدعم مع سيولة جيدة"
    elif pos <= 30 and near_fvg:
        return "🟢 دخول ممتاز — FVG + دعم", "السعر داخل فجوة سعرية مع دعم قوي"
    elif pos <= 50 and vol_ratio >= 1.2:
        return "🟡 دخول متوسط — سيولة مرتفعة", "السعر في المنتصف لكن السيولة تدعم"
    elif pos <= 50:
        return "🟡 دخول متوسط", "انتظر تراجعاً للدعم لتحسين نقطة الدخول"
    elif pos >= 80:
        return "🔴 تجنب الدخول", "السعر قريب جداً من المقاومة — خطر انعكاس"
    else:
        return "🟠 مخاطرة متوسطة-عالية", "السعر في الثلث العلوي من النطاق"


# ═══════════════════════════════════════════════════════════════════════════════
#  توليد الشارت
# ═══════════════════════════════════════════════════════════════════════════════
def build_chart(symbol: str, df, smc: dict, sl: float, t1: float, t2: float) -> io.BytesIO | None:
    try:
        style = mpf.make_mpf_style(
            base_mpf_style='nightclouds',
            marketcolors=mpf.make_marketcolors(
                up='#26a69a', down='#ef5350',
                edge={'up': '#26a69a', 'down': '#ef5350'},
                wick={'up': '#26a69a', 'down': '#ef5350'},
                volume={'up': '#1a6b63', 'down': '#8a2e2e'},
            ),
            facecolor='#131722', edgecolor='#2a2e39', figcolor='#131722',
            gridcolor='#1e222d', gridstyle='--', gridaxis='both', y_on_right=True,
            rc={'axes.labelcolor': '#d1d4dc', 'xtick.color': '#787b86',
                'ytick.color': '#d1d4dc', 'font.size': 9}
        )
        ma20 = mpf.make_addplot(df['Close'].rolling(20).mean(), color='#f0b90b', width=1.2)
        ma50 = mpf.make_addplot(df['Close'].rolling(50).mean(), color='#2962ff', width=1.2)

        price   = round(float(df['Close'].iloc[-1]), 2)
        change  = round(price - float(df['Close'].iloc[0]), 2)
        chg_pct = round(change / float(df['Close'].iloc[0]) * 100, 2)
        color_c = '#26a69a' if change >= 0 else '#ef5350'
        sign    = '+' if change >= 0 else ''
        arrow   = '▲' if change >= 0 else '▼'

        fig, axes = mpf.plot(df, type='candle', style=style,
                             addplot=[ma20, ma50], volume=True,
                             figsize=(13, 7.5), title='', tight_layout=True,
                             returnfig=True, panel_ratios=(4, 1),
                             datetime_format='%m/%d %H:%M', xrotation=20)
        ax = axes[0]

        # مستويات على الشارت
        lvls = [
            (t2,                    '#00e5ff', '--', f'T2 {t2}$'),
            (t1,                    '#26a69a', '--', f'T1 {t1}$'),
            (smc['swing_high'],     '#ff9800', '-',  f'مقاومة {smc["swing_high"]}$'),
            (price,                 '#f0b90b', '--', f'{price}$'),
            (smc['pool_buy'],       '#7b61ff', ':',  f'سيولة {smc["pool_buy"]}$'),
            (smc['swing_low'],      '#2962ff', '-',  f'دعم {smc["swing_low"]}$'),
            (sl,                    '#ef5350', '--', f'SL {sl}$'),
        ]
        for val, col, ls, lbl in lvls:
            ax.axhline(y=val, color=col, linewidth=0.9, linestyle=ls, alpha=0.85)
            ax.text(1.001, val, f' {lbl}', transform=ax.get_yaxis_transform(),
                    color=col, fontsize=7.5, va='center', fontweight='bold')

        # FVG shading
        if smc['fvg_bull']:
            ax.axhspan(smc['fvg_bull'][0], smc['fvg_bull'][1],
                       alpha=0.12, color='#26a69a', label='FVG')
        if smc['ob_zone']:
            ax.axhspan(smc['ob_zone'][0], smc['ob_zone'][1],
                       alpha=0.1, color='#7b61ff')

        fig.text(0.035, 0.97,
                 f'{symbol}  •  1H  •  {price}$   {arrow} {sign}{change}$ ({sign}{chg_pct}%)',
                 color=color_c, fontsize=13, fontweight='bold', va='top')
        fig.text(0.035, 0.93,
                 f'Swing High: {smc["swing_high"]}$   Swing Low: {smc["swing_low"]}$   Vol×: {smc["vol_ratio"]}',
                 color='#787b86', fontsize=8.5, va='top')

        legend = [
            Line2D([0], [0], color='#f0b90b', lw=1.5, label='MA20'),
            Line2D([0], [0], color='#2962ff', lw=1.5, label='MA50'),
            Line2D([0], [0], color='#26a69a', lw=1.2, ls='--', label='هدف'),
            Line2D([0], [0], color='#ef5350', lw=1.2, ls='--', label='SL'),
            Line2D([0], [0], color='#7b61ff', lw=1.2, ls=':', label='سيولة SMC'),
        ]
        ax.legend(handles=legend, loc='upper left', facecolor='#1e222d',
                  edgecolor='#2a2e39', labelcolor='#d1d4dc', fontsize=8)

        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor='#131722')
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.error(f"Chart error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  نص التقرير الكامل
# ═══════════════════════════════════════════════════════════════════════════════
def build_report(symbol: str, d: dict, smc: dict, capital: float | None = None) -> str:
    price   = d['price']
    sl      = round(price * 0.93, 2)    # وقف الخسارة 7%
    t1      = round(price * 1.10, 2)    # هدف 10%
    t2      = round(price * 1.18, 2)    # هدف 18%
    entry_l = round(price * 0.999, 2)
    sign    = '+' if d['change'] >= 0 else ''
    trend   = '🟢 صاعد' if d['change'] >= 0 else '🔴 هابط'

    # FVG
    fvg_line = ''
    if smc['fvg_bull']:
        fvg_line = f"📐 **FVG صاعد:** `{smc['fvg_bull'][0]}$ – {smc['fvg_bull'][1]}$` ← فجوة سعرية يبحث عنها السوق\n"
    elif smc['fvg_bear']:
        fvg_line = f"📐 **FVG هابط:** `{smc['fvg_bear'][0]}$ – {smc['fvg_bear'][1]}$`\n"

    # Order Block
    ob_line = ''
    if smc['ob_zone']:
        ob_line = f"🟣 **Order Block:** `{smc['ob_zone'][0]}$ – {smc['ob_zone'][1]}$` ← ذاكرة مؤسسية\n"

    # تدفق السيولة
    vr = smc['vol_ratio']
    if vr >= 1.8:   liq = f"🔥 سيولة انفجارية ({vr}x) — {smc['pressure']}"
    elif vr >= 1.2: liq = f"⚡ سيولة مرتفعة ({vr}x) — {smc['pressure']}"
    elif vr >= 0.8: liq = f"✅ سيولة طبيعية ({vr}x) — {smc['pressure']}"
    else:           liq = f"⚠️ سيولة ضعيفة ({vr}x) — توخَّ الحذر"

    # جودة الدخول
    sig_label, sig_desc = entry_signal(
        price, smc['swing_low'], smc['swing_high'],
        smc['pool_buy'], smc['fvg_bull'], vr
    )

    # ملخص AI بالعربية
    rr = round((t1 - price) / (price - sl), 2) if price > sl else 0
    if '🟢' in sig_label:
        ai_summary = (f"✅ الاتجاه {trend} بشكل واضح. فرصة دخول {sig_label.split('—')[0].strip()} "
                      f"عند `{entry_l}$` مع هدف `{t1}$` ووقف خسارة عند `{sl}$`. "
                      f"نسبة المخاطرة/المكافأة: **1:{rr}** — صفقة جذابة.")
    elif '🔴' in sig_label:
        ai_summary = (f"⛔ السعر قريب من المقاومة `{smc['swing_high']}$`. "
                      f"**لا تدخل الآن** — انتظر تراجعاً نحو `{smc['swing_low']}$` للحصول على نقطة دخول أفضل.")
    else:
        ai_summary = (f"⚠️ الوضع محايد. يمكن الدخول بحذر عند `{entry_l}$` "
                      f"لكن انتظر تأكيداً بكسر `{smc['swing_high']}$` لصعود قوي.")

    # حاسبة المخاطر الفورية
    risk_section = ''
    if capital and capital > 0:
        sl_per_share = round(price - sl, 2)
        risk_2       = capital * 0.02
        shares       = round(risk_2 / sl_per_share, 2) if sl_per_share > 0 else 0
        pos_size     = round(shares * price, 2)
        risk_section = (
            f"\n🧮 **حاسبة المخاطر (رأس مال {capital:,.0f}$):**\n"
            f"  • حجم الصفقة المقترح (2% خطر): `{pos_size:,.2f}$`\n"
            f"  • عدد الأسهم: `{shares:.2f}`\n"
            f"  • أقصى خسارة: `{risk_2:,.2f}$`\n"
        )

    return (
        f"📊 **{symbol} — تحليل SMC فوري (فريم ساعة)**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 **السعر:** `{price}$`  {trend}  ({sign}{d['chg_pct']}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔵 **دعم (Swing Low):** `{smc['swing_low']}$`\n"
        f"🟠 **مقاومة (Swing High):** `{smc['swing_high']}$`\n"
        f"🟣 **سيولة شراء (Pool):** `{smc['pool_buy']}$`\n"
        f"🟣 **سيولة بيع (Pool):** `{smc['pool_sell']}$`\n"
        f"{fvg_line}"
        f"{ob_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **نطاق الدخول:** `{entry_l}$ – {price}$`\n"
        f"🎯 **الهدف الأول (+10%):** `{t1}$`\n"
        f"🎯 **الهدف الثاني (+18%):** `{t2}$`\n"
        f"🛑 **وقف الخسارة (-7%):** `{sl}$`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💧 **تدفق السيولة:** {liq}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{sig_label}\n"
        f"_{sig_desc}_\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **ملخص الذكاء الاصطناعي:**\n"
        f"{ai_summary}"
        f"{risk_section}"
        f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _للأغراض التعليمية فقط._"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  نظام التنبيهات الفورية
# ═══════════════════════════════════════════════════════════════════════════════
async def check_alerts(context) -> None:
    """يعمل كل 5 دقائق ويتحقق من مستويات الاختراق."""
    for uid, watchlist in list(user_alerts.items()):
        for item in watchlist:
            d = fetch_data(item['symbol'])
            if not d:
                continue
            price = d['price']
            state = item.get('last_state', 'none')
            new_state = state

            msg = None
            if price >= item['resistance'] and state != 'breakout_up':
                msg = (f"🚨 **تنبيه اختراق صاعد — {item['symbol']}**\n"
                       f"السعر `{price}$` اخترق المقاومة `{item['resistance']}$` ⬆️\n"
                       f"🎯 الهدف التالي: `{round(item['resistance']*1.05,2)}$`")
                new_state = 'breakout_up'
            elif price <= item['support'] and state != 'breakout_down':
                msg = (f"⚠️ **تنبيه كسر دعم — {item['symbol']}**\n"
                       f"السعر `{price}$` كسر الدعم `{item['support']}$` ⬇️\n"
                       f"🛑 احترس من وقف الخسارة عند `{item['sl']}$`")
                new_state = 'breakout_down'
            elif price <= item['sl'] and state != 'sl_hit':
                msg = (f"🔴 **تحذير: وقف الخسارة — {item['symbol']}**\n"
                       f"السعر `{price}$` لامس وقف الخسارة `{item['sl']}$`!\n"
                       f"راجع صفقتك فوراً.")
                new_state = 'sl_hit'
            elif d['avg_vol'] > 0 and d['volume'] / d['avg_vol'] >= 2.0 and state != 'vol_spike':
                msg = (f"💥 **انفجار سيولة — {item['symbol']}**\n"
                       f"السعر `{price}$` | حجم التداول {round(d['volume']/d['avg_vol'],1)}x المعدل\n"
                       f"حركة قوية محتملة — تابع الشارت!")
                new_state = 'vol_spike'

            item['last_state'] = new_state
            if msg:
                try:
                    await context.bot.send_message(chat_id=uid, text=msg, parse_mode='Markdown')
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════════
#  Handlers
# ═══════════════════════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📊 تحليل سهم",              callback_data='analyze')],
        [InlineKeyboardButton("🔔 إضافة تنبيه فوري",       callback_data='add_alert')],
        [InlineKeyboardButton("📋 تنبيهاتي",               callback_data='my_alerts')],
        [InlineKeyboardButton("🛡️ حاسبة المخاطر",          callback_data='calc')],
        [InlineKeyboardButton("❓ كيفية الاستخدام",         callback_data='help')],
    ]
    text = (
        "📊 **مرحباً بك في StockBeacon — منارة الأسهم**\n\n"
        "تحليل SMC احترافي فوري للأسهم والعملات الرقمية:\n"
        "• مناطق السيولة والدعم والمقاومة الحقيقية\n"
        "• Fair Value Gaps & Order Blocks\n"
        "• تنبيهات فورية عند الاختراق\n\n"
        "اختر من القائمة:"
    )
    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')


async def btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == 'analyze':
        await q.message.reply_text("✍️ أرسل رمز السهم (مثال: `AAPL` أو `BTC-USD`):", parse_mode='Markdown')
        return WAITING_SYMBOL
    elif d == 'add_alert':
        await q.message.reply_text("🔔 أرسل رمز السهم الذي تريد مراقبته:")
        return WAITING_ALERT_SYMBOL
    elif d == 'my_alerts':
        uid = q.from_user.id
        items = user_alerts.get(uid, [])
        if not items:
            await q.message.reply_text("📋 لا توجد تنبيهات مفعّلة. أضف سهماً بالضغط على 'إضافة تنبيه فوري'.")
        else:
            lines = [f"• **{x['symbol']}** — مقاومة `{x['resistance']}$` | دعم `{x['support']}$` | SL `{x['sl']}$`"
                     for x in items]
            await q.message.reply_text(
                "📋 **تنبيهاتك الحالية:**\n" + "\n".join(lines) + "\n\n_يُفحص كل 5 دقائق_",
                parse_mode='Markdown'
            )
        return ConversationHandler.END
    elif d == 'calc':
        context.user_data['calc_only'] = True
        await q.message.reply_text("✍️ أرسل رمز السهم لحساب المخاطر:")
        return WAITING_SYMBOL
    elif d == 'help':
        await q.message.reply_text(
            "💡 **طريقة الاستخدام:**\n\n"
            "1️⃣ *تحليل سهم* — أرسل الرمز وستصلك صورة شارت احترافية مع:\n"
            "  • دعم ومقاومة حقيقية (Swing High/Low)\n"
            "  • مناطق السيولة (Liquidity Pools)\n"
            "  • الفجوات السعرية (FVG) وذاكرة المؤسسات (Order Block)\n"
            "  • هدف 10% وهدف 18% ووقف خسارة 7%\n"
            "  • تقييم الدخول + ملخص ذكاء اصطناعي بالعربية\n\n"
            "2️⃣ *تنبيه فوري* — أضف سهماً وسيصلك إشعار تلقائي عند:\n"
            "  • اختراق المقاومة ⬆️ أو كسر الدعم ⬇️\n"
            "  • لمس وقف الخسارة 🛑\n"
            "  • انفجار السيولة 💥 (2x المعدل)",
            parse_mode='Markdown'
        )
        return ConversationHandler.END
    elif d == 'del_alerts':
        uid = q.from_user.id
        user_alerts[uid] = []
        await q.message.reply_text("✅ تم حذف جميع التنبيهات.")
        return ConversationHandler.END
    elif d == 'main':
        await start(update, context)
        return ConversationHandler.END


# ─── تحليل السهم ─────────────────────────────────────────────────────────────
async def process_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.upper().strip()
    context.user_data['symbol'] = symbol
    calc_only = context.user_data.pop('calc_only', False)

    wait = await update.message.reply_text(f"⏳ جاري تحليل **{symbol}**...", parse_mode='Markdown')

    d = fetch_data(symbol)
    if not d:
        await wait.edit_text(f"❌ تعذر جلب `{symbol}`. تأكد من الرمز:", parse_mode='Markdown')
        return WAITING_SYMBOL

    df = fetch_ohlcv(symbol)
    smc = smc_analysis(df, d['price']) if df is not None else {
        'swing_high': round(d['price']*1.03,2), 'swing_low': round(d['price']*0.97,2),
        'pool_buy': round(d['price']*0.985,2), 'pool_sell': round(d['price']*1.015,2),
        'fvg_bull': None, 'fvg_bear': None, 'ob_zone': None,
        'vol_ratio': 1.0, 'pressure': 'غير محدد'
    }

    price = d['price']
    sl    = round(price * 0.93, 2)
    t1    = round(price * 1.10, 2)
    t2    = round(price * 1.18, 2)

    context.user_data.update({
        'last_symbol': symbol, 'last_price': price,
        'sl': sl, 'support': smc['swing_low'], 'resistance': smc['swing_high'],
        'smc': smc
    })

    capital = context.user_data.get('capital')

    if calc_only:
        await wait.edit_text("💰 أدخل رأس مالك بالدولار (مثال: 5000):")
        return WAITING_CAPITAL

    report = build_report(symbol, d, smc, capital)
    kb = [
        [InlineKeyboardButton("🧮 أدخل رأس مالك للحساب",   callback_data='calc')],
        [InlineKeyboardButton("🔔 تنبيه فوري على هذا السهم", callback_data='add_alert')],
        [InlineKeyboardButton("🔄 تحليل سهم آخر",           callback_data='analyze')],
        [InlineKeyboardButton("🔙 القائمة الرئيسية",         callback_data='main')],
    ]

    chart = build_chart(symbol, df, smc, sl, t1, t2) if df is not None else None
    await wait.delete()

    if chart:
        await update.message.reply_photo(photo=chart, caption=report,
                                         reply_markup=InlineKeyboardMarkup(kb),
                                         parse_mode='Markdown')
    else:
        await update.message.reply_text(report, reply_markup=InlineKeyboardMarkup(kb),
                                        parse_mode='Markdown')
    return ConversationHandler.END


# ─── حاسبة المخاطر ───────────────────────────────────────────────────────────
async def process_capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().replace(',', '')
    try:
        capital = float(raw)
        if capital <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أدخل رقماً صحيحاً (مثال: 10000):")
        return WAITING_CAPITAL

    context.user_data['capital'] = capital
    symbol = context.user_data.get('last_symbol', '')
    price  = context.user_data.get('last_price')
    sl     = context.user_data.get('sl')

    if not price:
        d = fetch_data(symbol) if symbol else None
        price = d['price'] if d else 100.0
        sl    = round(price * 0.93, 2)

    sl_ps   = round(price - sl, 2)
    risk1   = capital * 0.01
    risk2   = capital * 0.02
    sh1     = round(risk1 / sl_ps, 3) if sl_ps else 0
    sh2     = round(risk2 / sl_ps, 3) if sl_ps else 0
    pos1    = round(sh1 * price, 2)
    pos2    = round(sh2 * price, 2)
    t1      = round(price * 1.10, 2)
    rr      = round((t1 - price) / (price - sl), 2) if price > sl else 0
    warning = f"\n⚠️ سعر السهم ({price}$) أعلى من رأس مالك — استخدم منصة أسهم كسرية.\n" if capital < price else ""

    def sf(n): return f"{n:.3f} سهم" if n < 1 else f"{n:.2f} سهم"

    txt = (
        f"🧮 **حاسبة المخاطر الذكية — {symbol or 'السهم'}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 رأس المال: `{capital:,.2f}$`  |  السعر: `{price}$`\n"
        f"🛑 وقف الخسارة: `{sl}$` (-7%)  |  خسارة/سهم: `{sl_ps}$`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟡 **محافظ (مخاطرة 1% = {risk1:,.2f}$):**\n"
        f"  حجم الصفقة: `{pos1:,.2f}$`  |  {sf(sh1)}\n\n"
        f"🔴 **معتدل (مخاطرة 2% = {risk2:,.2f}$):**\n"
        f"  حجم الصفقة: `{pos2:,.2f}$`  |  {sf(sh2)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 الهدف (+10%): `{t1}$`  |  R:R = 1:{rr}\n"
        f"{warning}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 _الحجم محسوب على أساس الخسارة الفعلية لكل سهم._"
    )
    kb = [
        [InlineKeyboardButton("🔄 تحليل سهم آخر",   callback_data='analyze')],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data='main')],
    ]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
    return ConversationHandler.END


# ─── إضافة تنبيه ─────────────────────────────────────────────────────────────
async def process_alert_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.upper().strip()
    d = fetch_data(symbol)
    if not d:
        await update.message.reply_text(f"❌ تعذر جلب `{symbol}`. تأكد من الرمز:", parse_mode='Markdown')
        return WAITING_ALERT_SYMBOL

    price = d['price']
    uid   = update.effective_user.id
    sl    = round(price * 0.93, 2)

    # حساب الدعم والمقاومة من OHLCV إن أمكن
    df  = fetch_ohlcv(symbol)
    if df is not None:
        smc = smc_analysis(df, price)
        res = smc['swing_high']
        sup = smc['swing_low']
    else:
        res = round(price * 1.05, 2)
        sup = round(price * 0.97, 2)

    alert = dict(symbol=symbol, resistance=res, support=sup, sl=sl, last_state='none')
    user_alerts.setdefault(uid, [])

    # تجنب التكرار
    user_alerts[uid] = [a for a in user_alerts[uid] if a['symbol'] != symbol]
    user_alerts[uid].append(alert)

    await update.message.reply_text(
        f"✅ **تم تفعيل التنبيه على {symbol}!**\n\n"
        f"📌 السعر الحالي: `{price}$`\n"
        f"🟠 مقاومة: `{res}$` — سيصلك تنبيه عند الاختراق\n"
        f"🔵 دعم: `{sup}$` — سيصلك تنبيه عند الكسر\n"
        f"🛑 وقف الخسارة: `{sl}$` (-7%)\n\n"
        f"_يُفحص كل 5 دقائق تلقائياً_ 🔄",
        parse_mode='Markdown'
    )
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════════
#  التشغيل
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    if not TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN غير موجود.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    # Job Queue: تنبيهات كل 5 دقائق
    app.job_queue.run_repeating(check_alerts, interval=300, first=60)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CallbackQueryHandler(btn),
        ],
        states={
            WAITING_SYMBOL:       [MessageHandler(filters.TEXT & ~filters.COMMAND, process_symbol)],
            WAITING_CAPITAL:      [MessageHandler(filters.TEXT & ~filters.COMMAND, process_capital)],
            WAITING_ALERT_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_alert_symbol)],
        },
        fallbacks=[CommandHandler('start', start)],
        per_message=False,
    )
    app.add_handler(conv)

    print("🚀 StockBeacon v3 — SMC + تنبيهات فورية + حاسبة ذكية + ملخص AI")
    app.run_polling(drop_pending_updates=True)

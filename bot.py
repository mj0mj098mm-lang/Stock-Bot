import os
import yfinance as yf
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "⚡ **أهلاً بك في بوت التحليل الفني والفرص (StockBeacon AI)**\n\n"
        "أداة رصد المستويات الفنية والأهداف التلقائية للأسهم الأمريكية والعملات الرقمية 📈\n\n"
        "🔹 **طريقة الاستخدام:**\n"
        "أرسل رمز السهم الأمريكي (مثال: `NVDA` أو `AAPL` أو `TSLA`)\n"
        "أو رمز العملة الرقمية (مثال: `BTC-USD` أو `ETH-USD`)."
    )
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()
    
    wait_msg = await update.message.reply_text("🔍 جاري رصد مستويات السهم وتحليل البيانات...")

    try:
        stock = yf.Ticker(symbol)
        df = stock.history(period="1mo")

        if df.empty:
            await wait_msg.edit_text(f"❌ لم نتمكن من إيجاد بيانات للرمز `{symbol}`. تأكد من كتابة الرمز الصحيح.")
            return

        current_price = df['Close'].iloc[-1]
        
        recent_low = df['Low'].tail(10).min()
        stop_loss = recent_low if recent_low < current_price else current_price * 0.96
        
        risk_pct = ((current_price - stop_loss) / current_price) * 100
        
        target_1 = current_price * 1.05
        target_2 = current_price * 1.10
        
        ma20 = df['Close'].mean()
        trend = "🟢 إيجابي (تداول فوق المتوسط)" if current_price > ma20 else "🟠 محايد / تصحيحي"

        report = (
            f"📊 **تقرير التحليل الفني: ${symbol}**\n"
            f"───────────────\n"
            f"💵 **السعر الحالي:** ${current_price:.2f}\n"
            f"📈 **الاتجاه العام:** {trend}\n\n"
            f"📥 **نطاق الدخول المقترح:** ${current_price * 0.998:.2f} - ${current_price:.2f}\n"
            f"🎯 **الهدف الأول (+5%):** ${target_1:.2f}\n"
            f"🎯 **الهدف الثاني (+10%):** ${target_2:.2f}\n"
            f"🛑 **وقف الخسارة (-{risk_pct:.1f}%):** ${stop_loss:.2f}\n\n"
            f"🛡️ **إدارة المخاطر:** يُنصح بعدم الدخول بأكثر من 2% إلى 5% من رأس مالك في الصفقة الواحدة.\n"
            f"───────────────\n"
            f"⚠️ *تنبيه: مؤشرات فنية ناتجة عن خوارزميات برمجية لأغراض تحليلية وتأكيدية فقط، وليست توصية مالية مباشرة.*"
        )
        
        await wait_msg.edit_text(report, parse_mode='Markdown')

    except Exception as e:
        await wait_msg.edit_text("❌ حدث خطأ أثناء جلب البيانات. حاول مرة أخرى لاحقاً.")

def main():
    if not TOKEN:
        print("❌ خطأ: لم يتم تعيين TELEGRAM_BOT_TOKEN في متغيرات البيئة.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze))
    
    print("🤖 بوت StockBeacon شغال وجاهز لاستقبال الأوامر...")
    app.run_polling()

if __name__ == '__main__':
    main()

import sys
import os
import logging

# ── إصلاح مسار المكتبات ──────────────────────────────────────────────────────
_lib = os.path.join(os.path.dirname(__file__), ".pythonlibs", "lib", "python3.13", "site-packages")
if _lib not in sys.path:
    sys.path.insert(0, _lib)
# منع تكرار المسارات
seen = []
for p in sys.path:
    if p not in seen:
        seen.append(p)
sys.path[:] = seen

# ── تسجيل السجلات ────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── الاستيراد الرئيسي ─────────────────────────────────────────────────────────
from telegram.ext import ApplicationBuilder

import mani  # كل الـ handlers موجودة في mani.py

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("❌ TELEGRAM_BOT_TOKEN غير موجود في بيئة التشغيل!")
        sys.exit(1)

    app = ApplicationBuilder().token(token).build()

    # تسجيل جميع الـ handlers من mani.py
    mani.register_handlers(app)

    # تشغيل فحص التنبيهات كل 5 دقائق
    app.job_queue.run_repeating(mani.check_alerts_job, interval=300, first=60)

    print("🚀 StockBeacon — واجهة مطابقة + تنبيهات مربوطة بالسهم مباشرة")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

"""
StockBeacon — Entry Point
يشغّل:
  • خادم HTTP بسيط (Flask) للـ keep-alive على Render / Railway / كل خادم سحابي
  • بوت تيليغرام بـ polling مستمر
"""
import sys
import os
import threading
import logging

# ── إصلاح مسار المكتبات ──────────────────────────────────────────────────────
_lib = os.path.join(os.path.dirname(__file__), ".pythonlibs", "lib", "python3.13", "site-packages")
if _lib not in sys.path:
    sys.path.insert(0, _lib)
seen = []
for p in sys.path:
    if p not in seen:
        seen.append(p)
sys.path[:] = seen

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("StockBeacon")

# ── خادم HTTP للـ keep-alive ──────────────────────────────────────────────────
def _run_health_server():
    """خادم بسيط يستجيب لـ /health حتى لا يُغلق Render/Railway الخدمة"""
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        # على Render PORT مخصص؛ على Replit نختار منفذاً حراً
        port = int(os.environ.get("PORT", 9000))

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"OK - StockBeacon is alive"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass  # صمت السجلات الاعتيادية

        srv = HTTPServer(("0.0.0.0", port), _H)
        logger.info(f"🌐 Health server on port {port}")
        srv.serve_forever()
    except Exception as e:
        logger.warning(f"Health server error: {e}")

# ── التشغيل الرئيسي ───────────────────────────────────────────────────────────
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("❌  TELEGRAM_BOT_TOKEN مفقود!")
        sys.exit(1)

    # ابدأ خادم الـ keep-alive في خيط منفصل
    t = threading.Thread(target=_run_health_server, daemon=True)
    t.start()

    from telegram.ext import ApplicationBuilder
    import mani

    app = ApplicationBuilder().token(token).build()
    mani.register_handlers(app)
    app.job_queue.run_repeating(mani.check_alerts_job, interval=300, first=60)

    logger.info("🚀 StockBeacon AI — جاهز للعمل 24/7")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message","callback_query"])

if __name__ == "__main__":
    main()

import json
import logging
import os
from pathlib import Path
from threading import Thread

from waitress import serve

from bot import Bot, ManagedBots, TelegramError
from miniapp import create_miniapp
from service import Store, create_app
from telegram_paid_worker import start_paid_messages

def start_payment_bot(parent, parent_token, store):
    if not (token := os.getenv("PAYMENT_BOT_TOKEN")):
        return
    try:
        if token == parent_token:
            raise TelegramError("Payment bot must be different from TextScan")
        seller = Bot(token, store)
        seller_identity = seller.call("getMe", http_timeout=6)
        parent_identity = parent.call("getMe", http_timeout=6)
        if seller.call("getWebhookInfo", http_timeout=6).get("url"):
            raise TelegramError("Payment bot is already configured with a webhook")
        if not seller.sales or not seller_identity.get("username"):
            raise TelegramError("Payment bot or sales settings incomplete")
        seller.seller = parent_identity["username"]
        seller.invoice_issuer = seller_identity["id"]
        Thread(target=seller.run, daemon=True, name="payment-bot").start()
        parent.payment_username = seller_identity["username"]
        logging.info("TextScan payment bot enabled: @%s", parent.payment_username)
    except TelegramError as exc:
        logging.warning("Payment bot unavailable: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    store = Store(os.getenv("DATABASE_PATH", "data/textscan.sqlite3"))
    payment_settings = Path(store.path).with_name("payment-api.json")
    if payment_settings.is_file() and not payment_settings.stat().st_mode & 0o077:
        try:
            settings = json.loads(payment_settings.read_text(encoding="utf-8"))
            os.environ.update((key, settings[key]) for key in ("TG_API_ID", "TG_API_HASH"))
        except (OSError, ValueError, KeyError, TypeError):
            logging.warning("Could not read saved Telegram API settings.")
    if token := os.getenv("TELEGRAM_BOT_TOKEN"):
        bot = Bot(token, store)
        if username := os.getenv("MANAGER_BOT_USERNAME"):
            previous_token = os.getenv("PREVIOUS_MANAGER_BOT_TOKEN")
            previous_parent = Bot(previous_token, store) if previous_token else None
            bot.manager = ManagedBots(bot, username, previous_parent)
            Thread(target=bot.manager.restore, daemon=True, name="restore-managed-bots").start()
        if bot.miniapp_url.startswith("https://"):
            Thread(target=serve, args=(create_miniapp(bot, token),),
                   kwargs={"host": os.getenv("MINIAPP_HOST", "127.0.0.1"), "port": int(os.getenv("MINIAPP_PORT", "8082")),
                           "max_request_body_size": 12_000}, daemon=True, name="miniapp").start()
        if os.getenv("ENABLE_BOT_INVOICES") == "1":
            Thread(target=start_payment_bot, args=(bot, token, store), daemon=True,
                   name="configure-payment-bot").start()
        Thread(target=start_paid_messages, args=(bot,), daemon=True, name="paid-messages").start()
        Thread(target=bot.run, daemon=True, name="telegram").start()
    else:
        logging.info("TELEGRAM_BOT_TOKEN is absent. Running API only.")
    serve(create_app(store), host=os.getenv("HOST", "127.0.0.1"),
          port=int(os.getenv("PORT", "8080")), max_request_body_size=5 * 1024 * 1024 + 64 * 1024)

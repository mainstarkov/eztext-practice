"""Admin-only provisioning of the dedicated Telegram payment session."""

import asyncio
import io
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread

from telegram_paid_worker import matches_payment_receiver, start_paid_messages

LOG = logging.getLogger(__name__)
QR_LOCK = Lock()


def begin_payment_login(bot, user):
    if str(user) != os.getenv("PAID_MESSAGE_ADMIN_ID"):
        return
    keys = ("TG_API_ID", "TG_API_HASH", "PAID_MESSAGE_SESSION", "PAID_MESSAGE_RECEIVER_PHONE")
    if not all(os.getenv(key) for key in keys):
        bot.say(user, "QR-вход пока не настроен. Нужны данные собственного Telegram API-приложения.")
        return
    target = Path(os.environ["PAID_MESSAGE_SESSION"])
    if target.exists():
        bot.say(user, "Сессия уже сохранена. Для повторного входа сначала отключите действующую сессию.")
        return
    if not QR_LOCK.acquire(blocking=False):
        bot.say(user, "QR-вход уже запущен. Дождитесь завершения текущей попытки.")
        return
    Thread(target=payment_login, args=(bot, user, target), daemon=True, name="payment-login").start()


def payment_login(bot, user, target):
    saved = False
    try:
        saved = asyncio.run(make_payment_session(bot, user, target))
    except Exception as exc:
        LOG.warning("QR payment setup failed (%s).", type(exc).__name__)
        try:
            bot.say(user, "Не удалось завершить QR-вход. Попробуйте /payment_login ещё раз.")
        except Exception:
            LOG.warning("Could not deliver QR login status.")
    finally:
        QR_LOCK.release()
    if saved:
        start_paid_messages(bot)


async def make_payment_session(bot, user, target):
    from qrcode import make as make_qr
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    with TemporaryDirectory(dir=target.parent) as directory:
        temporary = Path(directory) / "payment.session"
        temporary.touch(mode=0o600)
        client = TelegramClient(str(temporary), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
        await client.connect()
        try:
            login = await client.qr_login()
            image = io.BytesIO()
            make_qr(login.url).save(image, format="PNG")
            # Start listening before sending the image so a fast scan is not missed.
            waiter = asyncio.create_task(login.wait())
            await asyncio.sleep(0)
            try:
                await asyncio.to_thread(
                    bot.send_file, user, "photo", "textscan-login.png", image.getvalue(), "image/png",
                    caption="На втором аккаунте откройте «Настройки → Устройства → Подключить устройство» "
                            "и отсканируйте QR. Не пересылайте этот код.",
                    protect_content="true",
                )
                session_user = await waiter
            except SessionPasswordNeededError:
                bot.say(user, "Telegram запросил 2FA. Эту попытку остановил. Пароль в бот не отправляйте.")
                return
            except asyncio.TimeoutError:
                bot.say(user, "QR-код истёк. Отправьте /payment_login для нового кода.")
                return
            finally:
                if not waiter.done():
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            if not matches_payment_receiver(session_user):
                await client.log_out()
                bot.say(user, "QR отсканирован другим аккаунтом. Сессия отклонена.")
                return
        finally:
            await client.disconnect()
        os.link(temporary, target)
    bot.say(user, "Сессия второго аккаунта сохранена. Проверяем режим платных сообщений.")
    return True

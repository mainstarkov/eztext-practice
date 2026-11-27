"""Optional Telegram user-session listener for genuine incoming Star Messages."""

import asyncio
import logging
import os
from pathlib import Path

from paid_messages import PaidMessages

LOG = logging.getLogger(__name__)


def matches_payment_receiver(user):
    expected = os.getenv("PAID_MESSAGE_RECEIVER_PHONE", "").lstrip("+")
    return bool(expected and user and not user.bot and (user.phone or "").lstrip("+") == expected)


async def watch_paid_messages(bot, session, api_id, api_hash):
    from telethon import TelegramClient, events, types

    client = TelegramClient(str(session), api_id, api_hash, receive_updates=True, catch_up=True)
    await client.connect()
    try:
        me = await client.get_me()
        if not me or me.bot or not me.username:
            LOG.warning("Paid-message receiver must be a signed-in user with a Telegram username.")
            return
        if not matches_payment_receiver(me):
            LOG.warning("Paid-message session belongs to a different Telegram account.")
            return
        ledger = PaidMessages(bot.store)

        @client.on(events.Raw(types=(types.UpdateNewMessage,)))
        async def incoming(update):
            message = update.message
            action = getattr(message, "action", None)
            if (not isinstance(message, types.MessageService) or message.out
                    or not isinstance(message.peer_id, types.PeerUser)
                    or not isinstance(action, types.MessageActionStarGift)
                    or action.refunded or action.auction_acquired or action.upgrade_separate
                    or not isinstance(action.gift, types.StarGift)):
                return
            sender = action.from_id or message.from_id or message.peer_id
            if not isinstance(sender, types.PeerUser) or sender.user_id != message.peer_id.user_id:
                return
            credits = await asyncio.to_thread(
                ledger.credit_gift, me.id, sender.user_id, message.id, int(message.date.timestamp()),
                action.gift.stars,
            )
            if credits:
                link = f"https://t.me/{bot.manager.username}?start=gift"
                await client.send_message(sender.user_id, f"Подарок получен. Начислено запросов: {credits}.\n"
                                          f"Открыть TextScan: {link}", link_preview=False)

        bot.paid_messages = (ledger, me.username)
        LOG.info("TextScan gift receiver is ready.")
        try:
            await client.run_until_disconnected()
        finally:
            bot.paid_messages = None
    finally:
        await client.disconnect()


def start_paid_messages(bot):
    session_path = os.getenv("PAID_MESSAGE_SESSION")
    if not session_path:
        return
    try:
        session = Path(session_path)
        if not session.is_file() or session.stat().st_mode & 0o077 or not bot.manager:
            raise ValueError("Invalid paid-message session configuration")
        asyncio.run(watch_paid_messages(
            bot, session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
        ))
    except Exception as exc:
        LOG.warning("Paid-message receiver unavailable (%s).", type(exc).__name__)

import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread, local
from urllib.parse import urlsplit

from paid_messages import GIFT_PACKS

import requests
from PIL import Image, ImageOps

from service import LANGUAGES, MAX_BYTES, PACKS, REFERRAL_BONUS, BotRequired, Problem, find_regions, interactive_model, interactive_snapshot, mark_lines, recognize

LOG = logging.getLogger(__name__)
TELEGRAM_HTTP = local()


def telegram_http():
    # Keep connections warm without sharing a Session between worker threads.
    if not hasattr(TELEGRAM_HTTP, "session"):
        TELEGRAM_HTTP.session = requests.Session()
    return TELEGRAM_HTTP.session


MAIN_BUTTONS = {
    "💳 Купить запросы": "/buy",
    "📊 Остаток": "/balance",
    "🔑 API-ключ": "/key",
    "📄 Условия": "/terms",
    "💬 Помощь с оплатой": "/paysupport",
    "🤖 Мой бот": "/mybot",
    "🎁 Пригласить друга": "/ref",
}
MAIN_ROWS = [
    ["📊 Остаток", "💳 Купить запросы"],
    ["🔑 API-ключ", "📄 Условия"],
    ["💬 Помощь с оплатой"],
]


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, token, store):
        test_mode = os.getenv("TELEGRAM_TEST") == "1"
        suffix = "test/" if test_mode else ""
        self.api = f"https://api.telegram.org/bot{token}/{suffix}"
        self.files = f"https://api.telegram.org/file/bot{token}/{suffix}"
        self.store = store
        self.snapshot_bot_id = int(token.partition(":")[0])
        self.manager = None
        self.parent_username = ""
        self.bot_id = None
        self.payment_username = ""
        self.paid_messages = None
        self.seller = None
        self.invoice_issuer = 0
        self.stop = Event()
        self.web_api = os.getenv("PUBLIC_API_URL", "").rstrip("/")
        self.miniapp_url = os.getenv("MINIAPP_URL", "").rstrip("/")
        self.support = os.getenv("SUPPORT_CONTACT", "")
        url = urlsplit(self.web_api)
        local_test = test_mode and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}
        self.sales = os.getenv("ENABLE_BOT_INVOICES") == "1" and bool(
            self.support and (local_test or (url.scheme == "https" and url.hostname))
        )
        self.packs = {**PACKS, "test": (10, 1)} if test_mode else PACKS
        self.workers = ThreadPoolExecutor(max_workers=2)
        self.slots = BoundedSemaphore(2)
        self.search_lock = Lock()
        self.snapshots = OrderedDict()
        self.recent_ocr = OrderedDict()
        self.prompts = OrderedDict()
        self.welcome_photo_id = None

    def call(self, method, http_timeout=10, **data):
        try:
            with telegram_http().post(self.api + method, json=data, timeout=http_timeout) as response:
                answer = response.json()
                if not answer.get("ok"):
                    raise TelegramError(
                        f"{method}: Telegram error {answer.get('error_code', response.status_code)}"
                    )
                return answer["result"]
        except (requests.RequestException, ValueError):
            raise TelegramError(f"{method}: connection failed") from None

    def say(self, user, text):
        self.call("sendMessage", chat_id=user, text=text)

    def show_menu(self, user):
        rows = [[{"text": label} for label in row] for row in MAIN_ROWS]
        if self.manager:
            personal = ({"text": "🤖 Мой бот"} if self.store.has_managed_bot(user)
                        else self.manager.creation_button())
            rows.insert(0, [personal, {"text": "🎁 Пригласить друга"}])
        caption = (
            "<b>EZText — текст из фото</b>\n\n"
            "Отправьте фото или файл. Бот вернёт текст и снимок с рамками. "
            "Нажмите «Открыть снимок», чтобы скопировать нужное слово.\n\n"
            "<b>10 бесплатных запросов в час</b> после создания своего бота.\n"
            "Подпись «точно» включает более тщательную обработку. Для рукописных заметок — «почерк»."
        )
        markup = {"keyboard": rows, "resize_keyboard": True}
        if self.manager:
            try:
                if self.welcome_photo_id:
                    self.call("sendPhoto", chat_id=user, photo=self.welcome_photo_id,
                              caption=caption, parse_mode="HTML", reply_markup=markup)
                    return
                cover = Path(__file__).with_name("welcome.jpg").read_bytes()
                result = self.send_file(user, "photo", "eztext-welcome.jpg", cover, "image/jpeg",
                                        caption=caption, parse_mode="HTML", reply_markup=json.dumps(markup))
                if result.get("photo"):
                    self.welcome_photo_id = result["photo"][-1]["file_id"]
                return
            except (OSError, TelegramError) as exc:
                self.welcome_photo_id = None
                LOG.warning("Could not send welcome image (%s).", type(exc).__name__)
        self.call("sendMessage", chat_id=user, text=caption, parse_mode="HTML", reply_markup=markup)

    def download(self, file):
        if file.get("file_size", 0) > MAX_BYTES:
            raise Problem("Максимальный размер файла — 5 МБ.", 413)
        for attempt in range(3):
            try:
                info = self.call("getFile", file_id=file["file_id"])
                path = info.get("file_path")
                if not path:
                    raise TelegramError("getFile: file path missing")
                body = bytearray()
                with telegram_http().get(self.files + path, stream=True, timeout=(5, 15)) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise Problem("Максимальный размер файла — 5 МБ.", 413)
                return bytes(body)
            except (requests.RequestException, TelegramError):
                if attempt == 2:
                    raise TelegramError("download: could not retrieve Telegram file") from None
                time.sleep(attempt + 1)

    def send_file(self, user, field, filename, content, mime, **data):
        method = "sendPhoto" if field == "photo" else "sendDocument"
        try:
            with telegram_http().post(
                self.api + method,
                data={"chat_id": user, **data},
                files={field: (filename, io.BytesIO(content), mime)},
                timeout=20,
            ) as response:
                answer = response.json()
                if not answer.get("ok"):
                    code = answer.get("error_code", response.status_code)
                    raise TelegramError(f"{method}: Telegram error {code}")
                return answer["result"]
        except (requests.RequestException, ValueError):
            raise TelegramError(f"{method}: connection failed") from None

    def cache_search(self, user, data, regions, word_boxes=None):
        token = secrets.token_urlsafe(9)
        expires = self.store.save_snapshot(token, self.snapshot_bot_id, user, data, regions, word_boxes)
        with self.search_lock:
            self.remember_snapshot(token, (expires, user, data, regions, word_boxes))
        return token

    def remember_snapshot(self, token, entry):
        self.snapshots[token] = entry
        self.snapshots.move_to_end(token)
        if len(self.snapshots) > 16:
            self.snapshots.popitem(last=False)

    def snapshot(self, user, token, include_words=False):
        with self.search_lock:
            entry = self.snapshots.get(token)
            if entry and entry[0] <= time.time():
                self.snapshots.pop(token)
                entry = None
            if entry and entry[1] != user:
                return None
        if entry is None:
            entry = self.store.load_snapshot(token, self.snapshot_bot_id, user)
            if entry is None:
                return None
            with self.search_lock:
                self.remember_snapshot(token, entry)
        return entry[2:5] if include_words else entry[2:4]

    def snapshot_model(self, user, token):
        saved = self.snapshot(user, token, include_words=True)
        if saved is None:
            return None
        with self.search_lock:
            entry = self.snapshots.get(token)
            if entry and len(entry) > 5:
                return entry[5]
        model = interactive_model(*saved)
        with self.search_lock:
            if entry and self.snapshots.get(token) is entry:
                self.snapshots[token] = (*entry, model)
        return model

    def search_button(self, token):
        viewer = {"text": "✨ Интерактивный снимок", "callback_data": f"view:{token}"}
        if self.miniapp_url.startswith("https://"):
            url = f"{self.miniapp_url}/?scan={token}"
            if self.bot_id is not None:
                url += f"&bot={self.bot_id}"
            viewer = {"text": "✨ Открыть снимок", "web_app": {"url": url}}
        return {"inline_keyboard": [
            [viewer],
            [{"text": "📋 Выбрать текст", "callback_data": f"lines:{token}:0"}],
            [{"text": "🔎 Найти и подсветить", "callback_data": f"find:{token}"}],
        ]}

    def send_interactive(self, event):
        user = event["from"]["id"]
        saved = self.snapshot(user, event["data"].removeprefix("view:"))
        if not saved:
            self.call("answerCallbackQuery", callback_query_id=event["id"],
                      text="Снимок уже недоступен. Отправьте его ещё раз.")
            return
        self.call("answerCallbackQuery", callback_query_id=event["id"])
        self.send_file(user, "document", "TextScan.html", interactive_snapshot(*saved), "text/html",
                       caption="Откройте файл в браузере. Нажмите на рамки на снимке, "
                               "чтобы собрать нужные строки или выбрать отдельные слова.")

    def text_picker(self, event):
        parts = event.get("data", "").split(":")
        action, token, *numbers = parts
        user = event["from"]["id"]
        saved = self.snapshot(user, token)
        if not saved or not all(value.isascii() and value.isdecimal() for value in numbers):
            self.call("answerCallbackQuery", callback_query_id=event["id"],
                      text="Снимок уже недоступен. Отправьте его ещё раз.")
            return
        regions = saved[1]
        rows = []
        if action == "lines" and len(numbers) == 1:
            page = int(numbers[0])
            start = page * 6
            if start >= len(regions):
                self.call("answerCallbackQuery", callback_query_id=event["id"])
                return
            title = "Выберите строку по номеру рамки"
            for index, (line, _) in enumerate(regions[start:start + 6], start):
                label = " ".join(line.split())
                label = label[:45] + ("…" if len(label) > 45 else "")
                rows.append([{"text": f"{index + 1} · {label}",
                              "callback_data": f"words:{token}:{index}:0"}])
            rows.extend(self.picker_navigation(f"lines:{token}", page, start + 6 < len(regions)))
        elif action == "words" and len(numbers) == 2 and int(numbers[0]) < len(regions):
            index, page = map(int, numbers)
            line = regions[index][0]
            title = f"Строка {index + 1}\n\n{line[:1500]}\n\nНажмите на слово, чтобы скопировать его."
            if len(line.encode("utf-16-le")) <= 512:
                rows.append([{"text": "📋 Скопировать строку", "copy_text": {"text": line}}])
            words = re.findall(r"[/@]?[\w]+(?:['’.-][\w]+)*|[^\w\s]", line)
            start = page * 8
            word_buttons = [{"text": word[:32], "copy_text": {"text": word}}
                            for word in words[start:start + 8]
                            if len(word.encode("utf-16-le")) <= 512]
            rows.extend(word_buttons[pos:pos + 3] for pos in range(0, len(word_buttons), 3))
            rows.extend(self.picker_navigation(f"words:{token}:{index}", page, start + 8 < len(words)))
            rows.append([{"text": "‹ К строкам", "callback_data": f"lines:{token}:{index // 6}"}])
        else:
            self.call("answerCallbackQuery", callback_query_id=event["id"])
            return
        self.call("answerCallbackQuery", callback_query_id=event["id"])
        message = event["message"]
        markup = {"inline_keyboard": rows}
        if "photo" in message or "document" in message:
            self.call("sendMessage", chat_id=user, text=title, reply_markup=markup,
                      reply_parameters={"message_id": message["message_id"], "allow_sending_without_reply": True})
        else:
            self.call("editMessageText", chat_id=user, message_id=message["message_id"],
                      text=title, reply_markup=markup)

    @staticmethod
    def picker_navigation(prefix, page, has_next):
        buttons = []
        if page > 0:
            buttons.append({"text": "‹ Назад", "callback_data": f"{prefix}:{page - 1}"})
        if has_next:
            buttons.append({"text": "Дальше ›", "callback_data": f"{prefix}:{page + 1}"})
        return [buttons] if buttons else []

    def search_prompt(self, event):
        token = event.get("data", "").removeprefix("find:")
        user = event["from"]["id"]
        if not event.get("data", "").startswith("find:") or not self.snapshot(user, token):
            self.call("answerCallbackQuery", callback_query_id=event["id"],
                      text="Снимок уже недоступен. Отправьте его ещё раз.")
            return
        self.call("answerCallbackQuery", callback_query_id=event["id"])
        prompt = self.call("sendMessage", chat_id=user,
                           text="Что выделить? Ответьте буквой, словом, фразой или номером рамки, например #3. Подсвечу все совпадения, включая похожие от 70%.",
                           reply_markup={"force_reply": True, "input_field_placeholder": "Буква, слово, фраза или #3"})
        self.prompts[(user, prompt["message_id"])] = token
        if len(self.prompts) > 64:
            self.prompts.popitem(last=False)

    def highlight(self, user, token, query):
        saved = self.snapshot(user, token, include_words=True)
        if not saved:
            self.say(user, "Снимок уже недоступен. Отправьте его ещё раз.")
            return
        if not 1 <= len(query) <= 200:
            self.say(user, "Введите от 1 до 200 символов или номер рамки.")
            return
        data, regions, word_boxes = saved
        selected = find_regions(regions, query, word_boxes)
        if not selected:
            self.call("sendMessage", chat_id=user, text="Совпадений нет. Попробуйте другой запрос.",
                      reply_markup=self.search_button(token))
            return
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGBA")
            canvas = Image.new("RGBA", image.size, "white")
            canvas.alpha_composite(image)
            preview = mark_lines(canvas, [box for _, box in selected], accent="#FFC857", numbered=False)
        excerpt = " · ".join(dict.fromkeys(label for label, _ in selected))
        excerpt = excerpt[:450] + ("…" if len(excerpt) > 450 else "")
        meta = {"caption": "Подсвечено областей: " + str(len(selected)) + "\n\n" + excerpt,
                "reply_markup": json.dumps(self.search_button(token))}
        try:
            self.send_file(user, "photo", "textscan-focus.jpg", preview, "image/jpeg", **meta)
        except TelegramError:
            self.send_file(user, "document", "textscan-focus.jpg", preview, "image/jpeg", **meta)

    def recognize_photo(self, user, data, lang, handwriting, exact=False):
        tessdata = None if exact or handwriting else os.getenv("BOT_FAST_TESSDATA")
        key = (user, hashlib.sha256(data).digest(), lang, handwriting, tessdata)
        with self.search_lock:
            now = time.monotonic()
            while self.recent_ocr and next(iter(self.recent_ocr.values()))[0] <= now:
                self.recent_ocr.popitem(last=False)
            cached = self.recent_ocr.get(key)
        if cached is not None:
            self.store.debit(user, via_bot=True)
            return cached[1]

        regions = []
        word_boxes = {}
        text, marked = recognize(self.store, user, data, lang, via_bot=True,
                                 regions=regions, word_boxes=word_boxes, handwriting=handwriting, tessdata=tessdata)
        result = text, marked, regions, word_boxes
        with self.search_lock:
            self.recent_ocr[key] = (time.monotonic() + 180, result)
            self.recent_ocr.move_to_end(key)
            if len(self.recent_ocr) > 8:
                self.recent_ocr.popitem(last=False)
        return result

    def scan(self, user, file, lang, message_id=None, handwriting=False, exact=False):
        reply = {"message_id": message_id, "allow_sending_without_reply": True} if message_id else {}
        media_reply = {"reply_parameters": json.dumps(reply)} if reply else {}
        try:
            trial, credits = self.store.balance(user)
            if not (trial or credits) and not self.store.has_managed_bot(user):
                raise BotRequired()
            data = self.download(file)
            started = time.monotonic()
            status = 500
            try:
                text, marked, regions, word_boxes = self.recognize_photo(user, data, lang, handwriting, exact=exact)
                status = 200
            except Problem as exc:
                status = exc.status
                raise
            finally:
                self.store.record_request(user, status, lang, round((time.monotonic() - started) * 1000),
                                          source="telegram")
            if text and handwriting:
                self.say(user, "✍️ Черновик рукописного текста. Сверьте результат с фото.")
            if len(text) <= 3000:
                self.call("sendMessage", chat_id=user,
                          text=text or "Текст не найден. Попробуйте отправить чёткое изображение файлом.",
                          **({"reply_parameters": reply} if reply else {}))
            else:
                self.send_file(user, "document", "textscan.txt", text.encode(), "text/plain", **media_reply)
            if marked:
                if regions:
                    token = self.cache_search(user, data, regions, word_boxes)
                    media_reply["reply_markup"] = json.dumps(self.search_button(token))
                try:
                    self.send_file(user, "photo", "textscan-marked.jpg", marked, "image/jpeg",
                                   caption="TextScan · найденные строки", **media_reply)
                except TelegramError as exc:
                    LOG.warning("%s. Sending marked image as an attachment.", exc)
                    self.send_file(user, "document", "textscan-marked.jpg", marked, "image/jpeg",
                                   caption="TextScan · фото с рамками", **media_reply)
        except BotRequired as exc:
            if self.manager:
                self.manager.offer(user)
            elif self.parent_username:
                self.say(user, f"Чтобы открыть бесплатные запросы, создайте бота через /mybot "
                               f"в @{self.parent_username}. Там же доступны платные пакеты.")
            else:
                self.say(user, str(exc))
        except Problem as exc:
            self.say(user, str(exc))
        except TelegramError as exc:
            LOG.warning("%s", exc)
            try:
                self.say(user, "Telegram не передал фото или ответ полностью. Отправьте фото ещё раз.")
            except TelegramError as notify_error:
                LOG.warning("%s", notify_error)
        except Exception:
            LOG.exception("Unexpected OCR error")
            try:
                self.say(user, "Не удалось обработать фото. Попробуйте ещё раз.")
            except TelegramError as notify_error:
                LOG.warning("%s", notify_error)
        finally:
            self.slots.release()

    def send_key(self, user, prefix=""):
        try:
            key = self.store.issue_key(user)
        except Problem as exc:
            self.say(user, str(exc))
            return
        markup = {}
        if self.seller:
            markup["reply_markup"] = {"inline_keyboard": [[{
                "text": "Открыть TextScan", "url": f"https://t.me/{self.seller}",
            }]]}
        elif self.web_api.startswith("https://"):
            markup["reply_markup"] = {"inline_keyboard": [[{
                "text": "Открыть кабинет API", "url": os.getenv("PUBLIC_CONNECT_URL") or self.web_api,
            }]]}
        self.call("sendMessage", chat_id=user, link_preview_options={"is_disabled": True}, **markup,
                  text=f"{prefix}API-ключ:\n{key}\n"
                       f"API: {self.web_api or 'адрес уточните у владельца бота'}\n"
                       "Передавайте ключ в заголовке Authorization: Bearer <ключ>. "
                       "Сохраните его. Команда /key заменяет текущий ключ.")
        if prefix and self.sales:
            Thread(target=self.verify_paid_key, args=(key,), daemon=True).start()

    def verify_paid_key(self, key):
        try:
            with requests.get(self.web_api + "/v1/balance",
                              headers={"Authorization": "Bearer " + key},
                              timeout=6, allow_redirects=False) as response:
                result = response.json()
                if response.status_code == 200 and isinstance(result.get("credits"), int):
                    LOG.info("Paid API key verified through public HTTPS.")
                    return
        except (requests.RequestException, ValueError, AttributeError):
            pass
        LOG.warning("Could not verify newly issued paid API key through public HTTPS.")


    def update(self, event):
        if self.manager is not None and (managed := event.get("managed_bot")):
            self.manager.received(managed["user"]["id"], managed["bot"])
            return
        if callback := event.get("callback_query"):
            if callback.get("data") == "admin:payment_login":
                user = callback["from"]["id"]
                chat = callback.get("message", {}).get("chat", {})
                self.call("answerCallbackQuery", callback_query_id=callback["id"])
                if self.manager and chat.get("type") == "private" and chat.get("id") == user:
                    from telegram_qr_setup import begin_payment_login
                    begin_payment_login(self, user)
            elif callback.get("data", "").startswith(("detach:", "detach-confirm:")):
                if self.manager:
                    self.manager.detach_callback(callback)
            elif callback.get("data", "").startswith("pay:"):
                self.confirm_purchase(callback)
            elif callback.get("data", "").startswith("gift:"):
                user = callback["from"]["id"]
                chat = callback.get("message", {}).get("chat", {})
                self.call("answerCallbackQuery", callback_query_id=callback["id"])
                if self.manager and chat.get("type") == "private" and chat.get("id") == user:
                    self.buy(user, [callback["data"].removeprefix("gift:")])
            elif callback.get("data", "").startswith("view:"):
                self.send_interactive(callback)
            elif callback.get("data", "").startswith(("lines:", "words:")):
                self.text_picker(callback)
            else:
                self.search_prompt(callback)
            return
        if query := event.get("pre_checkout_query"):
            ok = self.sales and self.store.checkout(query["from"]["id"], query, self.invoice_issuer)
            data = {} if ok else {"error_message": "Счёт недоступен. Создайте новый через /buy."}
            self.call("answerPreCheckoutQuery", pre_checkout_query_id=query["id"], ok=ok, **data)
            return
        msg = event.get("message", {})
        if msg.get("chat", {}).get("type") != "private" or "from" not in msg:
            return
        user = msg["from"]["id"]
        original_text = msg.get("text", "")
        text = original_text.split()
        command = text[0].split("@")[0] if text else ""
        menu_command = MAIN_BUTTONS.get(original_text.strip())
        if menu_command:
            command, text = menu_command, [menu_command]
        referral = None
        if self.manager and command == "/start" and len(text) == 2:
            match = re.fullmatch(r"ref_([A-Za-z0-9_-]{22})", text[1])
            if match:
                referral = match[1]
        self.store.account(user, referral)
        if self.manager is not None and (created := msg.get("managed_bot_created")):
            self.manager.received(user, created["bot"])
            return
        if payment := msg.get("successful_payment"):
            if self.parent_username:
                return
            try:
                credits = self.store.fulfill(user, payment, self.invoice_issuer)
            except (ValueError, KeyError):
                LOG.error("Payment update failed validation, update_id=%s", event["update_id"])
                self.say(user, "Платёж требует проверки. Напишите в /paysupport.")
                return
            if credits:
                self.send_key(user, f"Оплата подтверждена. Начислено {credits} запросов.\n")
            return
        replied_to = msg.get("reply_to_message", {}).get("message_id")
        if original_text and (token := self.prompts.get((user, replied_to))):
            self.highlight(user, token, original_text.strip())
            return
        if command == "/admin":
            if self.manager and str(user) == os.getenv("PAID_MESSAGE_ADMIN_ID"):
                state = "подключена" if self.paid_messages else "не подключена"
                settings = bool(os.getenv("TG_API_ID") and os.getenv("TG_API_HASH"))
                buttons = []
                if self.miniapp_url.startswith("https://") and not settings:
                    buttons.append([{"text": "Сохранить данные Telegram API",
                                     "web_app": {"url": f"{self.miniapp_url}/payment-setup"}}])
                if settings and not self.paid_messages:
                    buttons.append([{"text": "Подключить аккаунт по QR", "callback_data": "admin:payment_login"}])
                markup = {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}
                self.call("sendMessage", chat_id=user,
                          text=f"Администрирование TextScan\nСессия платёжного аккаунта: {state}.",
                          **markup)
        elif command == "/payment_login" and self.manager:
            from telegram_qr_setup import begin_payment_login
            begin_payment_login(self, user)
        elif self.seller and command in {"/start", "/help"}:
            requested = text[1][4:] if len(text) == 2 and text[1].startswith("buy_") else ""
            self.buy(user, [requested] if requested in self.packs else [])
        elif self.seller and command not in {"/buy", "/balance", "/key", "/terms", "/support", "/paysupport"}:
            self.call("sendMessage", chat_id=user,
                      text="Здесь можно купить запросы. Для распознавания фотографий откройте TextScan.",
                      reply_markup={"inline_keyboard": [[{"text": "Открыть TextScan",
                                                         "url": f"https://t.me/{self.seller}"}]]})
        elif command in {"/mybot", "/newbot"} and self.manager:
            self.manager.offer(user)
        elif command == "/ref" and self.manager:
            code, invited, completed, bonus = self.store.referral_stats(user)
            url = f"https://t.me/{self.manager.username}?start=ref_{code}"
            self.call("sendMessage", chat_id=user, text=(
                f"Твоя ссылка: {url}\n\nПерешли её другу. После того как он впервые запустит "
                f"TextScan по ссылке и создаст своего бота, ты получишь {REFERRAL_BONUS} бонусных запросов.\n\n"
                f"Пришли по ссылке: {invited}\nСоздали бота: {completed}\nБонусных запросов осталось: {bonus}"),
                link_preview_options={"is_disabled": True},
                reply_markup={"inline_keyboard": [[{"text": "Скопировать ссылку", "copy_text": {"text": url}}]]})
        elif self.parent_username and command in {"/buy", "/paysupport", "/support", "/mybot", "/newbot", "/ref"}:
            self.say(user, f"Покупка запросов и управление ботами: @{self.parent_username}.")
        elif command == "/start" and text[1:] == ["gift"]:
            _, credits = self.store.balance(user)
            self.send_key(user, f"Платных запросов на балансе: {credits}.\n")
        elif command in {"/start", "/help"}:
            self.show_menu(user)
        elif command == "/balance":
            trial, credits = self.store.balance(user)
            free = f"Бесплатных за час: {trial} из 10" if self.store.has_managed_bot(user) else (
                "Бесплатные запросы откроются после создания своего бота.")
            bonus = self.store.bonus_balance(user)
            self.say(user, f"{free}\nБонусных в боте: {bonus}\nПлатных для API и бота: {credits}")
        elif command == "/redeem" and self.paid_messages:
            ledger, _ = self.paid_messages
            if len(text) != 2 or not (credits := ledger.redeem(user, text[1])):
                self.say(user, "Код неверный, уже использован или выдан другому покупателю.")
                return
            self.send_key(user, f"Код активирован. Начислено запросов: {credits}.\n")
        elif command == "/key":
            self.send_key(user)
        elif command in {"/support", "/paysupport"}:
            if self.support:
                self.say(user, f"Споры по оплате и возвраты: {self.support}. "
                         "Поддержка Telegram не рассматривает покупки в TextScan.")
            else:
                self.say(user, "Приём платежей пока закрыт.")
        elif command == "/terms":
            self.say(user, self.terms())
        elif command == "/buy":
            self.buy(user, text[1:])
        elif file := (msg.get("photo") or [msg.get("document")])[-1]:
            if self.slots.acquire(blocking=False):
                caption = msg.get("caption", "").strip().lower()
                handwriting = caption in {"почерк", "почерк rus", "почерк eng"}
                exact = caption in {"точно", "точно rus", "точно eng", "точно rus+eng"}
                language = caption.partition(" ")[2] if handwriting or exact else caption
                lang = language if language in LANGUAGES else "rus+eng"
                self.workers.submit(self.scan, user, file, lang, msg.get("message_id"), handwriting, exact)
            else:
                self.say(user, "Сервис занят. Попробуйте отправить изображение через несколько секунд.")
        else:
            self.say(user, "Отправьте изображение JPEG, PNG или WebP. Команды — /help.")

    def terms(self):
        return ("Один запрос распознаёт одно изображение. Пакет действует для API и бота. "
                "Запрос списывается после обработки, даже если на изображении нет текста. "
                "При ошибке распознавания запрос возвращается. Оплата — в Telegram Stars. "
                f"Вопросы по покупкам и возвратам: {self.support or '/paysupport'}.")

    def buy(self, user, args):
        if self.paid_messages:
            _, receiver = self.paid_messages
            if len(args) != 1 or args[0] not in {"15", "25"}:
                self.call("sendMessage", chat_id=user, text="Выберите подарок для оплаты.",
                          reply_markup={"inline_keyboard": [[{
                              "text": f"{credits} запросов · {stars} ⭐", "callback_data": f"gift:{stars}",
                          }] for stars, credits in GIFT_PACKS.items()]})
                return
            stars = int(args[0])
            self.call("sendMessage", chat_id=user, text=(
                f"{GIFT_PACKS[stars]} запросов за подарок стоимостью {stars} ⭐. "
                "Откройте профиль и отправьте подарок. Запросы начислятся автоматически. "
                "После оплаты в чате появится ссылка обратно в бот."),
                reply_markup={"inline_keyboard": [[{
                    "text": "Открыть профиль получателя", "url": f"https://t.me/{receiver}",
                }]]})
            return
        if self.payment_username:
            payload = "buy_" + args[0] if len(args) == 1 and args[0] in self.packs else "buy"
            self.call("sendMessage", chat_id=user,
                      text="Оплата за Telegram Stars. После покупки запросы появятся в балансе TextScan.",
                      reply_markup={"inline_keyboard": [[{
                          "text": "Перейти к оплате", "url": f"https://t.me/{self.payment_username}?start={payload}",
                      }]]})
            return
        if not self.sales:
            self.say(user, "Покупки пока закрыты.")
            return
        if len(args) != 1 or args[0] not in self.packs:
            offers = "\n".join(f"/buy {size} — {credits} запросов за {stars} ⭐"
                               for size, (credits, stars) in self.packs.items())
            self.say(user, f"{self.terms()}\n\nЕсли согласны, выберите пакет командой:\n{offers}")
            return
        pack = args[0]
        credits, stars = self.packs[pack]
        self.call("sendMessage", chat_id=user,
                  text=f"{credits} запросов за {stars} ⭐\n\n{self.terms()}",
                  reply_markup={"inline_keyboard": [[{
                      "text": "Согласен с условиями · выставить счёт",
                      "callback_data": f"pay:{pack}",
                  }]]})

    def confirm_purchase(self, callback):
        user = callback["from"]["id"]
        chat = callback.get("message", {}).get("chat", {})
        pack = callback["data"].removeprefix("pay:")
        answer = {"callback_query_id": callback["id"]}
        if self.payment_username or self.paid_messages:
            self.call("answerCallbackQuery", **answer, text="Продажа перенесена. Откройте /buy.")
            return
        if not self.sales or pack not in self.packs or chat.get("type") != "private" or chat.get("id") != user:
            self.call("answerCallbackQuery", **answer, text="Счёт недоступен. Откройте /buy.")
            return
        self.call("answerCallbackQuery", **answer)
        credits, stars = self.packs[pack]
        order_id = self.store.order(user, credits, stars, self.invoice_issuer)
        self.call(
            "sendInvoice", chat_id=user, title=f"TextScan — {credits} запросов",
            description="Пополнение баланса API и Telegram-бота. Условия — /terms.",
            payload=order_id, currency="XTR", provider_token="",
            prices=[{"label": f"{credits} запросов", "amount": stars}],
            start_parameter="buy",
        )

    def run(self):
        offset = 0
        allowed = ["message", "pre_checkout_query", "callback_query"]
        if self.manager:
            allowed.append("managed_bot")
        while not self.stop.is_set():
            try:
                updates = self.call("getUpdates", http_timeout=35, timeout=25, offset=offset,
                                    allowed_updates=allowed, limit=25)
                if self.stop.is_set():
                    break
                for update in updates:
                    try:
                        self.update(update)
                    except TelegramError as exc:
                        LOG.warning("%s", exc)
                    offset = update["update_id"] + 1
                if not updates:
                    time.sleep(0.5)
            except TelegramError as exc:
                LOG.warning("%s", exc)
                self.stop.wait(3)


class ManagedBots:
    LIMIT = 16

    def __init__(self, parent, username, previous_parent=None):
        self.parent = parent
        self.username = username
        self.previous_parent = previous_parent
        self.active = {}
        self.lock = Lock()
    def activate(self, bot_id):
        try:
            token = self.parent.call("getManagedBotToken", user_id=bot_id, http_timeout=6)
        except TelegramError:
            if self.previous_parent is None:
                raise
            token = self.previous_parent.call("getManagedBotToken", user_id=bot_id, http_timeout=6)
        if token.partition(":")[0] != str(bot_id):
            raise TelegramError("Telegram returned an unexpected managed bot")
        with self.lock:
            with self.parent.store.connect() as db:
                if not db.execute("SELECT 1 FROM managed_bots WHERE id=?", (bot_id,)).fetchone():
                    return
            old = self.active.get(bot_id)
            if old and old[1] == token:
                return
            bot = Bot(token, self.parent.store)
            bot.parent_username = self.username
            bot.miniapp_url = self.parent.miniapp_url
            bot.bot_id = bot_id
            bot.sales = False
            if old:
                old[0].stop.set()
                old[0].workers.shutdown(wait=False, cancel_futures=True)
            self.active[bot_id] = (bot, token)
            Thread(target=bot.run, daemon=True, name=f"managed-bot-{bot_id}").start()

    def lookup(self, bot_id):
        with self.lock:
            return self.active.get(bot_id)

    def restore(self):
        with self.parent.store.connect() as db:
            bots = db.execute("SELECT id, owner FROM managed_bots").fetchall()
        for bot_id, owner in bots:
            try:
                self.activate(bot_id)
                self.reward(owner, bot_id)
            except TelegramError as exc:
                LOG.warning("Could not restore managed bot: %s", exc)

    @staticmethod
    def creation_button():
        return {"text": "🤖 Создать своего бота", "request_managed_bot": {
            "request_id": secrets.randbelow(2**31),
            "suggested_name": "TextScan OCR",
            "suggested_username": f"ts{secrets.token_hex(2)}bot",
        }}

    def offer(self, user):
        with self.parent.store.connect() as db:
            row = db.execute("SELECT id, username FROM managed_bots WHERE owner=?", (user,)).fetchone()
            count = db.execute("SELECT count(*) FROM managed_bots").fetchone()[0]
        if row:
            try:
                self.activate(row[0])
            except TelegramError:
                self.parent.say(user, "Не удалось подключить бота. Попробуйте /mybot ещё раз позже.")
                return
            self.reward(user, row[0])
            self.parent.call("sendMessage", chat_id=user, text="Ваш OCR-бот подключён. Теперь доступно 10 бесплатных запросов в час.",
                             reply_markup={"inline_keyboard": [[
                                 {"text": "Открыть своего бота", "url": f"https://t.me/{row[1]}"},
                             ], [{"text": "Отвязать бота", "callback_data": f"detach:{row[0]}"}]]})
            return
        if count >= self.LIMIT:
            self.parent.say(user, "Сейчас все места заняты. Попробуйте позже.")
            return
        self.parent.call("sendMessage", chat_id=user,
                         text="Создайте своего OCR-бота с собственным именем. Он будет обрабатывать фото "
                              "на сервере TextScan. После создания откроются 10 бесплатных "
                              "распознаваний в час. Лимит общий для обоих ботов.",
                         reply_markup={
                             "keyboard": [[self.creation_button()]],
                             "resize_keyboard": True, "one_time_keyboard": True,
                         })

    def detach_callback(self, callback):
        user = callback["from"]["id"]
        chat = callback.get("message", {}).get("chat", {})
        action, _, raw_id = callback.get("data", "").partition(":")
        answer = {"callback_query_id": callback["id"]}
        if chat.get("type") != "private" or chat.get("id") != user or not re.fullmatch(r"[0-9]{1,16}", raw_id):
            self.parent.call("answerCallbackQuery", **answer)
            return
        bot_id = int(raw_id)
        with self.lock, self.parent.store.connect() as db:
            row = db.execute("SELECT username FROM managed_bots WHERE id=? AND owner=?", (bot_id, user)).fetchone()
            if row and action == "detach-confirm":
                db.execute("INSERT OR IGNORE INTO detached_bots(id) VALUES (?)", (bot_id,))
                db.execute("DELETE FROM managed_bots WHERE id=? AND owner=?", (bot_id, user))
        if not row:
            self.parent.call("answerCallbackQuery", **answer, text="Этот бот уже отвязан или принадлежит другому аккаунту.")
            return
        self.parent.call("answerCallbackQuery", **answer)
        if action == "detach":
            self.parent.call("sendMessage", chat_id=user,
                             text=f"Отвязать @{row[0]} от EZText? Распознавание в нём остановится, "
                                  "а бесплатный лимит снова откроется после создания нового бота.",
                             reply_markup={"inline_keyboard": [[{
                                 "text": "Подтвердить отвязку", "callback_data": f"detach-confirm:{bot_id}",
                             }]]})
            return
        with self.lock:
            child = self.active.pop(bot_id, None)
            if child:
                child[0].stop.set()
                child[0].workers.shutdown(wait=False, cancel_futures=True)
        self.parent.say(user, f"@{row[0]} отвязан от EZText. Для нового OCR-бота отправьте /mybot.")

    def received(self, owner, identity):
        bot_id, username = identity["id"], identity.get("username", "")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return
        with self.parent.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM detached_bots WHERE id=?", (bot_id,)).fetchone():
                return
            known = db.execute("SELECT owner, username FROM managed_bots WHERE id=?", (bot_id,)).fetchone()
            occupied = db.execute("SELECT id FROM managed_bots WHERE owner=? AND id<>?",
                                  (owner, bot_id)).fetchone()
            count = db.execute("SELECT count(*) FROM managed_bots WHERE id<>?", (bot_id,)).fetchone()[0]
            if not occupied and count < self.LIMIT:
                db.execute("""INSERT INTO managed_bots (id, owner, username) VALUES (?, ?, ?)
                              ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, username=excluded.username""",
                           (bot_id, owner, username))
                if known is None:
                    db.execute("UPDATE referrals SET bot_id=? WHERE invitee=? AND bot_id IS NULL",
                               (bot_id, owner))
        if occupied or count >= self.LIMIT:
            self.parent.say(owner, "Подключение недоступно: лимит управляемых ботов исчерпан.")
            return
        if known == (owner, username) and self.lookup(bot_id):
            self.activate(bot_id)
            self.reward(owner, bot_id)
            return
        self.offer(owner)

    def reward(self, user, bot_id):
        if (inviter := self.parent.store.reward_referral(user, bot_id)) is not None:
            try:
                self.parent.say(inviter, f"Приглашённый друг создал своего бота. "
                                 f"Начислено {REFERRAL_BONUS} бонусных запросов. Счётчик: /ref.")
            except TelegramError as exc:
                LOG.warning("Referral reward notification failed: %s", exc)

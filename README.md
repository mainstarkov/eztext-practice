# EZText

EZText распознаёт русский и английский текст на фотографиях и скриншотах. Telegram-бот возвращает текст и снимок с рамками. В мини-аппе можно нажать на слово для копирования, выделить несколько слов жестом и искать совпадения с учётом опечаток.

- Telegram: [@eztextbot](https://t.me/eztextbot)
- Кабинет: [eztext.noip.at/dashboard/](https://eztext.noip.at/dashboard/)
- Документация API: [eztext.noip.at/docs/](https://eztext.noip.at/docs/)

## Запуск

Понадобятся Python 3.10+ и Tesseract с моделями `rus` и `eng`. На macOS установите их командой `brew install tesseract tesseract-lang`.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a
. ./.env
set +a
python main.py
```

Укажите токен своего бота в `TELEGRAM_BOT_TOKEN`. HTTP API слушает порт 8080. Для мини-аппа задайте `MINIAPP_URL` и направьте HTTPS-прокси на порт 8082. В публичной конфигурации префикс `/api/` проксируется на порт 8080 с удалением префикса, а `/dashboard/` и `/docs/` передаются как есть.

Для контейнерного запуска заполните `.env` и выполните `docker compose up -d --build`. Данные SQLite хранятся в отдельном томе. Платёжного бота со счетами можно включить через `ENABLE_BOT_INVOICES=1`. Подключение личного аккаунта для приёма подарков требует отдельно настроенных Telethon, qrcode и закрытой сессии Telegram.

## Распознавание через API

Получите ключ командой `/key` в Telegram. После создания своего OCR-бота 10 бесплатных запросов в час действуют и в Telegram, и через API. Повторная выдача заменяет прежний ключ.

```sh
curl https://eztext.noip.at/api/v1/ocr \
  -H "Authorization: Bearer $EZTEXT_API_KEY" \
  -F 'image=@photo.png'
```

`GET /api/v1/balance` показывает остаток, а `GET /api/v1/history` возвращает общую историю API и Telegram. Интерактивные снимки доступны отправителю в течение 24 часов. На VPS можно настроить отдельную быструю модель для Telegram через `BOT_FAST_TESSDATA`. Подпись к фото «точно» выбирает основную OCR-модель, а «почерк» включает обработку рукописных заметок.

Не добавляйте в Git токены, файлы окружения, базы, ключи или сессии Telegram.

## Демо и адрес запуска

[Открыть GitHub Pages](https://mainstarkov.github.io/eztext-practice/).

API: http://127.0.0.1:8080. Мини-приложение: порт 8082.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a
. ./.env
set +a
python main.py
```

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl

from flask import Flask, Response, abort, request

from service import find_regions

def telegram_user(raw, token):
    if not isinstance(raw, str) or len(raw) > 8192:
        return None
    try:
        pairs = parse_qsl(raw, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            return None
        signature = fields.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
        expected = hmac.digest(secret, check.encode(), hashlib.sha256).hex()
        if not hmac.compare_digest(signature, expected):
            return None
        age = time.time() - int(fields["auth_date"])
        if not -30 <= age <= 900:
            return None
        user = json.loads(fields["user"])["id"]
        return user if type(user) is int and user > 0 else None
    except (KeyError, ValueError, TypeError):
        return None


def create_miniapp(bot, token):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 12_000
    template = Path(__file__).with_name("interactive.html").read_text(encoding="utf-8")
    template = template.replace(
        '<div class="layout">',
        '<p id="launch-status" role="status">Открываем снимок…</p><div class="layout" hidden>',
    ).replace(
        "script-src 'unsafe-inline'",
        "connect-src 'self'; script-src 'unsafe-inline' https://telegram.org",
    ).replace(
        '<script type="application/json" id="scan-data">@@SCAN_DATA@@</script>',
        '<script src="https://telegram.org/js/telegram-web-app.js"></script>',
    ).replace(
        "const {image, lines, width, height} = JSON.parse(document.querySelector('#scan-data').textContent);",
        """(async () => {
const telegram = window.Telegram?.WebApp;
const status = document.querySelector('#launch-status');
status.textContent = 'Открываем снимок…';
const params = new URL(location.href).searchParams;
const snap = params.get('scan');
if (!telegram?.initData || !snap) {
  status.textContent = 'Откройте снимок кнопкой в чате с ботом.';
  return;
}
telegram.ready();
telegram.expand();
const requestSnapshot = (extra = {}, signal) => fetch(new URL('snapshot', location.href), {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({snap, bot: params.get('bot'), initData: telegram.initData, ...extra}),
  signal,
});
async function searchSnapshot(query, signal) {
  const result = await requestSnapshot({query}, signal);
  if (!result.ok) throw new Error('Search failed');
  return (await result.json()).matches;
}
const response = await requestSnapshot();
if (!response.ok) {
  status.textContent = response.status === 403
    ? 'Откройте снимок в Telegram из чата, где вы отправили фото.'
    : response.status === 404
      ? 'Срок хранения снимка истёк или он был создан до обновления. Отправьте фото ещё раз.'
      : 'Не удалось загрузить снимок. Попробуйте открыть его ещё раз.';
  return;
}
const {image, lines, width, height} = await response.json();
status.hidden = true;
document.querySelector('.layout').hidden = false;""",
    ).replace(
        "render();\n</script>",
        """render();
})().catch(() => {
  const status = document.querySelector('#launch-status');
  status.hidden = false;
  document.querySelector('.layout').hidden = true;
  status.textContent = 'Не удалось загрузить снимок. Откройте его ещё раз.';
});
</script>""",
    )

    @app.after_request
    def private(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def viewer():
        return Response(template, content_type="text/html; charset=utf-8")

    @app.get("/payment-setup")
    def payment_setup():
        return Response(
            Path(__file__).with_name("payment_setup.html").read_text(encoding="utf-8"),
            content_type="text/html; charset=utf-8",
        )

    @app.post("/payment-setup")
    def save_payment_setup():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        user = telegram_user(data.get("initData"), token)
        if user is None or str(user) != os.getenv("PAID_MESSAGE_ADMIN_ID"):
            abort(403)
        api_id, api_hash = data.get("api_id"), data.get("api_hash")
        if (not isinstance(api_id, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", api_id)
                or api_id in {"2040", "17349"} or int(api_id) > 2147483647
                or not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash)):
            abort(400)
        session = os.getenv("PAID_MESSAGE_SESSION", "")
        if not session or Path(session).is_file():
            abort(409)
        target = Path(bot.store.path).with_name("payment-api.json")
        payload = {"TG_API_ID": api_id, "TG_API_HASH": api_hash}
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".payment-api.")
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(payload, file)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        os.environ.update(payload)
        return {"saved": True}

    @app.post("/snapshot")
    def snapshot():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("snap"), str):
            abort(400)
        target, target_token = bot, token
        if (bot_id := data.get("bot")) is not None:
            if not isinstance(bot_id, str) or not bot_id.isascii() or not bot_id.isdecimal() or len(bot_id) > 16:
                abort(400)
            managed = bot.manager.lookup(int(bot_id)) if bot.manager else None
            if not managed:
                abort(404)
            target, target_token = managed
        user = telegram_user(data.get("initData"), target_token)
        if user is None:
            abort(403)
        if "query" in data:
            query = data["query"]
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
                abort(400)
            saved = target.snapshot(user, data["snap"], include_words=True)
            if saved is None:
                abort(404)
            _, regions, word_boxes = saved
            return {"matches": [box for _, box in find_regions(regions, query.strip(), word_boxes)]}
        model = target.snapshot_model(user, data["snap"])
        if model is None:
            abort(404)
        return model

    return app

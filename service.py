import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from difflib import SequenceMatcher
from threading import BoundedSemaphore

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, UnidentifiedImageError

MAX_BYTES = 5 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000
LANGUAGES = {"rus", "eng", "rus+eng"}
WORKERS = BoundedSemaphore(2)
PACKS = {"100": (100, 1), "1000": (1000, 5)}
FREE_PER_HOUR = 10
FREE_WINDOW = 3600
REFERRAL_BONUS = 10


class Problem(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class BotRequired(Problem):
    def __init__(self):
        super().__init__("Чтобы открыть 10 бесплатных запросов в час, создайте своего бота через /mybot. "
                         "Платные пакеты доступны через /buy.", 402)


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY, trial INTEGER NOT NULL,
                    credits INTEGER NOT NULL DEFAULT 0, key_hash TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS detached_bots (id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS managed_bots (
                    id INTEGER PRIMARY KEY, owner INTEGER NOT NULL UNIQUE, username TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS referral_profiles (
                    owner INTEGER PRIMARY KEY REFERENCES accounts(id),
                    code TEXT NOT NULL UNIQUE,
                    bonus INTEGER NOT NULL DEFAULT 0 CHECK(bonus >= 0)
                );
                CREATE TABLE IF NOT EXISTS referrals (
                    invitee INTEGER PRIMARY KEY REFERENCES accounts(id),
                    inviter INTEGER NOT NULL REFERENCES referral_profiles(owner),
                    bot_id INTEGER UNIQUE,
                    rewarded INTEGER NOT NULL DEFAULT 0 CHECK(rewarded IN (0, 1)),
                    CHECK(inviter <> invitee)
                );
                CREATE INDEX IF NOT EXISTS referrals_inviter ON referrals(inviter);
                CREATE TABLE IF NOT EXISTS free_scans (
                    id TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
                    started REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS free_scans_account_time ON free_scans(account, started);
                CREATE INDEX IF NOT EXISTS free_scans_time ON free_scans(started);
                CREATE TABLE IF NOT EXISTS paid_message_orders (
                    ticket TEXT PRIMARY KEY,
                    buyer INTEGER NOT NULL REFERENCES accounts(id),
                    expires INTEGER NOT NULL,
                    recipient INTEGER,
                    message_id INTEGER,
                    code_hash TEXT UNIQUE,
                    redeemed INTEGER NOT NULL DEFAULT 0 CHECK(redeemed IN (0, 1)),
                    UNIQUE (recipient, buyer, message_id)
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    token TEXT PRIMARY KEY, bot INTEGER NOT NULL, owner INTEGER NOT NULL,
                    expires INTEGER NOT NULL, image BLOB NOT NULL, geometry TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS snapshots_expires ON snapshots(expires);
                CREATE TABLE IF NOT EXISTS api_requests (
                    id INTEGER PRIMARY KEY,
                    account INTEGER NOT NULL REFERENCES accounts(id),
                    at INTEGER NOT NULL,
                    status INTEGER NOT NULL,
                    language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS api_requests_account_id ON api_requests(account,id DESC);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, account INTEGER NOT NULL REFERENCES accounts(id),
                    credits INTEGER NOT NULL, stars INTEGER NOT NULL,
                    charge TEXT UNIQUE,
                    issuer INTEGER NOT NULL DEFAULT 0
                );
            """)

            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(orders)")}
            if "issuer" not in columns:
                db.execute("ALTER TABLE orders ADD COLUMN issuer INTEGER NOT NULL DEFAULT 0")
            payment_columns = {row[1] for row in db.execute("PRAGMA table_info(paid_message_orders)")}
            if "stars" not in payment_columns:
                db.execute("ALTER TABLE paid_message_orders ADD COLUMN stars INTEGER NOT NULL DEFAULT 1")
            history_columns = {row[1] for row in db.execute("PRAGMA table_info(api_requests)")}
            if "source" not in history_columns:
                db.execute("ALTER TABLE api_requests ADD COLUMN source TEXT NOT NULL DEFAULT 'api'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def account(self, user, referral_code=None):
        with self.connect() as db:
            created = db.execute("INSERT OR IGNORE INTO accounts(id, trial) VALUES (?, 0)", (user,)).rowcount
            if created and referral_code:
                db.execute("""INSERT INTO referrals(invitee, inviter)
                              SELECT ?, owner FROM referral_profiles WHERE code=? AND owner<>?""",
                           (user, referral_code, user))

    def referral_stats(self, user):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO referral_profiles(owner, code) VALUES (?, ?)",
                       (user, secrets.token_urlsafe(16)))
            code, bonus = db.execute("SELECT code, bonus FROM referral_profiles WHERE owner=?", (user,)).fetchone()
            invited, completed = db.execute(
                "SELECT count(*), coalesce(sum(rewarded),0) FROM referrals WHERE inviter=?", (user,)
            ).fetchone()
            return code, invited, completed, bonus

    def bonus_balance(self, user):
        with self.connect() as db:
            row = db.execute("SELECT bonus FROM referral_profiles WHERE owner=?", (user,)).fetchone()
            return row[0] if row else 0

    def reward_referral(self, user, bot_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""UPDATE referrals SET rewarded=1
                WHERE invitee=? AND bot_id=? AND rewarded=0
                AND EXISTS (SELECT 1 FROM managed_bots WHERE id=? AND owner=?)
                RETURNING inviter""", (user, bot_id, bot_id, user)).fetchone()
            if not row:
                return None
            inviter = row[0]
            db.execute("UPDATE referral_profiles SET bonus=bonus+? WHERE owner=?", (REFERRAL_BONUS, inviter))
            return inviter

    def has_managed_bot(self, user):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM managed_bots WHERE owner=?", (user,)).fetchone() is not None

    def balance(self, user):
        with self.connect() as db:
            used = db.execute(
                "SELECT count(*) FROM free_scans WHERE account=? AND started>?",
                (user, time.time() - FREE_WINDOW),
            ).fetchone()[0]
            credits = db.execute("SELECT credits FROM accounts WHERE id=?", (user,)).fetchone()[0]
            eligible = db.execute("SELECT 1 FROM managed_bots WHERE owner=?", (user,)).fetchone()
            return (FREE_PER_HOUR - used if eligible else 0), credits

    def issue_key(self, user):
        key = "ts_" + secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id, trial) VALUES (?, 0)", (user,))
            db.execute("UPDATE accounts SET key_hash=? WHERE id=?", (digest(key), user))
        return key

    def authenticate(self, key):
        with self.connect() as db:
            return db.execute("SELECT id, credits FROM accounts WHERE key_hash=?", (digest(key),)).fetchone()

    def debit(self, user, via_bot):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            eligible = db.execute("SELECT 1 FROM managed_bots WHERE owner=?", (user,)).fetchone() is not None
            if eligible:
                now = time.time()
                db.execute("DELETE FROM free_scans WHERE started<=?", (now - FREE_WINDOW,))
                used = db.execute(
                    "SELECT count(*) FROM free_scans WHERE account=?", (user,)
                ).fetchone()[0]
                if used < FREE_PER_HOUR:
                    reservation = secrets.token_hex(16)
                    db.execute("INSERT INTO free_scans VALUES (?, ?, ?)", (reservation, user, now))
                    return reservation
            if via_bot and eligible and db.execute(
                "UPDATE referral_profiles SET bonus=bonus-1 WHERE owner=? AND bonus>0", (user,)
            ).rowcount:
                return "bonus"
            if db.execute(
                "UPDATE accounts SET credits=credits-1 WHERE id=? AND credits>0", (user,)
            ).rowcount:
                return "credits"
        if via_bot and not eligible:
            raise BotRequired()
        if eligible:
            raise Problem("10 бесплатных запросов за последний час уже использованы. "
                          "Дождитесь обновления лимита или пополните баланс через /buy.", 429)
        raise Problem("Создайте OCR-бота через /mybot для бесплатных запросов или пополните баланс через /buy.", 402)

    def save_snapshot(self, token, bot, owner, data, regions, words):
        now = int(time.time())
        expires = now + 86400
        with self.connect() as db:
            db.execute("DELETE FROM snapshots WHERE expires<=?", (now,))
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?)",
                       (token, bot, owner, expires, data, json.dumps([regions, words])))
        return expires

    def load_snapshot(self, token, bot, owner):
        with self.connect() as db:
            row = db.execute("""SELECT expires,image,geometry FROM snapshots
                WHERE token=? AND bot=? AND owner=? AND expires>?""",
                             (token, bot, owner, int(time.time()))).fetchone()
        if row is None:
            return None
        expires, image, geometry = row
        regions, words = json.loads(geometry)
        return expires, owner, image, regions, {int(key): boxes for key, boxes in (words or {}).items()}

    def record_request(self, user, status, language, duration, source="api"):
        try:
            with self.connect() as db:
                db.execute("""INSERT INTO api_requests(account,at,status,language,duration_ms,source)
                              VALUES (?,?,?,?,?,?)""", (user, int(time.time()), status, language, duration, source))
                db.execute("""DELETE FROM api_requests WHERE account=? AND id <= (
                    SELECT id FROM api_requests WHERE account=? ORDER BY id DESC LIMIT 1 OFFSET 100
                )""", (user, user))
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Could not save OCR request metadata")

    def api_history(self, user):
        with self.connect() as db:
            rows = db.execute("""SELECT at,status,language,duration_ms,source FROM api_requests
                                 WHERE account=? ORDER BY id DESC LIMIT 100""", (user,)).fetchall()
        return [{"at": at, "status": status, "language": language, "duration_ms": duration, "source": source}
                for at, status, language, duration, source in rows]

    def restore(self, user, bucket):
        with self.connect() as db:
            if bucket == "credits":
                db.execute("UPDATE accounts SET credits=credits+1 WHERE id=?", (user,))
            elif bucket == "bonus":
                db.execute("UPDATE referral_profiles SET bonus=bonus+1 WHERE owner=?", (user,))
            else:
                db.execute("DELETE FROM free_scans WHERE id=? AND account=?", (bucket, user))

    def order(self, user, credits, stars, issuer=0):
        order_id = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("INSERT INTO orders(id, account, credits, stars, issuer) VALUES (?, ?, ?, ?, ?)",
                       (order_id, user, credits, stars, issuer))
        return order_id

    def checkout(self, user, payment, issuer=0):
        with self.connect() as db:
            return payment["currency"] == "XTR" and db.execute(
                "SELECT 1 FROM orders WHERE id=? AND account=? AND stars=? AND issuer=? AND charge IS NULL",
                (payment["invoice_payload"], user, payment["total_amount"], issuer),
            ).fetchone() is not None

    def fulfill(self, user, payment, issuer=0):
        if payment["currency"] != "XTR":
            raise ValueError("Unexpected payment currency")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT credits, charge FROM orders WHERE id=? AND account=? AND stars=? AND issuer=?",
                (payment["invoice_payload"], user, payment["total_amount"], issuer),
            ).fetchone()
            if row is None:
                raise ValueError("Payment does not match an order")
            credits, old_charge = row
            charge = payment["telegram_payment_charge_id"]
            if old_charge == charge:
                return 0
            if old_charge is not None:
                raise ValueError("Order already fulfilled with a different charge")
            db.execute("UPDATE orders SET charge=? WHERE id=?", (charge, payment["invoice_payload"]))
            db.execute("UPDATE accounts SET credits=credits+? WHERE id=?", (credits, user))
            return credits


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def tesseract_lines(tsv):
    boxes = {}
    words = {}
    word_boxes = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if row["level"] != "5" or not (row["text"] or "").strip():
            continue
        x, y, width, height = (int(row[field]) for field in ("left", "top", "width", "height"))
        line = tuple(row[field] for field in ("page_num", "block_num", "par_num", "line_num"))
        token = row["text"].strip()
        words.setdefault(line, []).append(token)
        word_boxes.setdefault(line, []).extend([(x, y, x + width, y + height)] * len(token.split()))
        if line in boxes:
            left, top, right, bottom = boxes[line]
            boxes[line] = min(left, x), min(top, y), max(right, x + width), max(bottom, y + height)
        else:
            boxes[line] = x, y, x + width, y + height
    return [(" ".join(words[key]), box, word_boxes[key]) for key, box in boxes.items()]


def mark_lines(image, boxes, accent="#50F5D0", numbered=True):
    if not boxes:
        return None

    preview = image.convert("RGB")
    preview.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
    scale_x, scale_y = preview.width / image.width, preview.height / image.height
    stroke = max(2, min(5, round(min(preview.size) / 240)))
    padding = stroke * 2
    font = ImageFont.load_default(size=14 + stroke)
    draw = ImageDraw.Draw(preview)
    for index, (x1, y1, x2, y2) in enumerate(boxes, 1):
        left = max(0, round(x1 * scale_x) - padding)
        top = max(0, round(y1 * scale_y) - padding)
        right = min(preview.width - 1, round(x2 * scale_x) + padding)
        bottom = min(preview.height - 1, round(y2 * scale_y) + padding)
        draw.rounded_rectangle((left, top, right, bottom), radius=6, outline="#0A2535", width=stroke + 2)
        draw.rounded_rectangle((left, top, right, bottom), radius=6, outline=accent, width=stroke)
        if not numbered:
            continue
        label = str(index)
        label_width = round(draw.textlength(label, font=font)) + 12
        label_height = 20 + stroke
        if top >= label_height and left + label_width <= preview.width:
            draw.rounded_rectangle((left, top - label_height, left + label_width, top),
                                   radius=5, fill=accent)
            draw.text((left + 6, top - label_height + 1), label, font=font, fill="#0A2535")
    min_side = (max(preview.size) + 19) // 20
    if min(preview.size) < min_side:
        preview = ImageOps.pad(preview, (max(preview.width, min_side), max(preview.height, min_side)),
                               color="#0A2535")
    content = io.BytesIO()
    preview.save(content, "JPEG", quality=88)
    return content.getvalue()


def run_ocr(normalized, lang, image=None, regions=None, word_boxes=None, input_size=None, modes=("3", "11"), tessdata=None):
    environment = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    if tessdata:
        environment["TESSDATA_PREFIX"] = tessdata
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "scan"
        for mode in modes:
            try:
                run = subprocess.run(
                    ["tesseract", "stdin", str(output), "-l", lang, "--psm", mode,
                     "-c", "tessedit_create_txt=1",
                     "-c", f"tessedit_create_tsv={int(image is not None)}"],
                    input=normalized, capture_output=True, timeout=20, check=True,
                    env=environment,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise Problem("Распознавание временно недоступно.", 503) from exc
            if b"Failed loading language" in run.stderr:
                raise Problem("Языковая модель временно недоступна.", 503)
            text = output.with_suffix(".txt").read_text(encoding="utf-8").strip()
            if text:
                break
        preview = None
        if text and image is not None:
            lines = tesseract_lines(output.with_suffix(".tsv").read_text(encoding="utf-8"))
            if input_size and input_size != image.size:
                sx = image.width / input_size[0]
                sy = image.height / input_size[1]

                def original_box(box):
                    x1, y1, x2, y2 = box
                    return x1 * sx, y1 * sy, x2 * sx, y2 * sy

                lines = [(line, original_box(box), [original_box(word) for word in words])
                         for line, box, words in lines]
            if regions is not None:
                regions.extend((fix_english_quotes(line), box) for line, box, _ in lines)
            if word_boxes is not None:
                word_boxes.update({index: words for index, (_, _, words) in enumerate(lines)})
            preview = mark_lines(image, [box for _, box, _ in lines])
        return fix_english_quotes(text), preview


CHAT_LINE = re.compile(
    r"(?P<word>/?[A-Za-z0-9_]{1,23})\s+(?P<time>(?:[01]?[0-9]|2[0-3]):[0-5][0-9])"
    r"\s*[/lI|vV✓✔]{1,3}"
)


def refine_chat_lines(source, lines, lang):
    if lang == "rus":
        return
    remaining = 4
    for line in lines:
        match = CHAT_LINE.fullmatch(line["text"])
        if not match or "prefix_box" not in line or not remaining:
            continue
        x1, y1, x2, y2 = line["prefix_box"]
        crop = source.crop((round(x1 * source.width) - 3, round(y1 * source.height) - 3,
                            round(x2 * source.width) + 3, round(y2 * source.height) + 3)).convert("L")
        stats = ImageStat.Stat(crop)
        if stats.median[0] >= 128 or stats.extrema[0][1] < 200:
            continue
        remaining -= 1
        word = match["word"]
        if len(word) >= 5 and any(char.isdigit() for char in word):
            crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
            crop = ImageOps.expand(crop.point(lambda pixel: 0 if pixel > 190 else 255),
                                   border=12, fill=255)
            payload = io.BytesIO()
            crop.save(payload, "PNG")
            try:
                reading = subprocess.run(
                    ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "7"],
                    input=payload.getvalue(), capture_output=True, timeout=2, check=True,
                    env={**os.environ, "OMP_THREAD_LIMIT": "1"},
                ).stdout.decode("utf-8", errors="replace").strip()
            except (OSError, subprocess.SubprocessError):
                reading = ""
            if (re.fullmatch(r"/?[A-Za-z0-9_]+", reading) and len(reading) == len(word)
                    and SequenceMatcher(None, reading, word).ratio() >= 0.75):
                word = reading
        line["text"] = f"{word} {match['time']}"


def fix_english_quotes(text):
    return re.sub(
        r"""(?<![\w'"])['‘’"](?P<word>[A-Za-z][A-Za-z0-9_-]*)['‘’"](?![\w'"])""",
        lambda match: ("'" + match["word"] + "'")
        if not (match[0].startswith('"') and match[0].endswith('"')) else match[0],
        text,
    )


def interactive_model(data, regions, word_boxes=None):
    with Image.open(io.BytesIO(data)) as raw:
        image = ImageOps.exif_transpose(raw)
        transparent = "A" in image.getbands() or "transparency" in image.info
        image = image.convert("RGBA" if transparent else "RGB")
    width, height = image.size
    image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
    if transparent:
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    photo = io.BytesIO()
    image.save(photo, "JPEG", quality=88, optimize=True)
    return {
        "image": "data:image/jpeg;base64," + base64.b64encode(photo.getvalue()).decode("ascii"),
        "width": width,
        "height": height,
        "lines": [{"text": text, "box": box, "word_boxes": (word_boxes or {}).get(index, [])}
                  for index, (text, box) in enumerate(regions)],
    }


def interactive_snapshot(data, regions):
    payload = json.dumps(interactive_model(data, regions), ensure_ascii=True).replace("<", "\\u003c")
    template = Path(__file__).with_name("interactive.html").read_text(encoding="utf-8")
    return template.replace("@@SCAN_DATA@@", payload).encode("utf-8")


def find_regions(regions, query, word_boxes=None):
    selector = re.fullmatch(r"#([1-9][0-9]{0,5})", query)
    if selector:
        return regions[int(selector[1]) - 1:int(selector[1])]
    needle = re.findall(r"[^\W_]+", query.casefold())
    if not needle:
        return []

    words = []
    for row, (line, line_box) in enumerate(regions):
        tokens = list(re.finditer(r"\S+", line))
        boxes = (word_boxes or {}).get(row, [])
        for index, token in enumerate(tokens):
            normalized = re.findall(r"[^\W_]+", token[0].casefold())
            if not normalized:
                continue
            box = boxes[index] if len(boxes) == len(tokens) else None
            words.append(("".join(normalized), token[0], box, line_box))

    selected = {}
    if len(needle) == 1 and len(needle[0]) == 1:
        letter = needle[0]
        for _, word, box, line_box in words:
            for offset, character in enumerate(word):
                if character.casefold() != letter:
                    continue
                if box:
                    left, top, right, bottom = box
                    step = (right - left) / len(word)
                    bounds = (left + step * offset, top, left + step * (offset + 1), bottom)
                else:
                    bounds = tuple(line_box)
                selected.setdefault(bounds, character)
    else:
        count = len(needle)
        def similar(actual, expected):
            if actual == expected:
                return True
            matcher = SequenceMatcher(None, actual, expected, autojunk=False)
            return matcher.quick_ratio() >= .7 and matcher.ratio() >= .7

        for start in range(len(words) - count + 1):
            group = words[start:start + count]
            if not all(similar(word[0], expected) for word, expected in zip(group, needle)):
                continue
            for _, word, box, line_box in group:
                selected.setdefault(tuple(box or line_box), word)
    return [(word, box) for box, word in selected.items()]


def run_vision(image, lang, include_preview, regions=None, word_boxes=None):
    executable = os.getenv("VISION_OCR_EXECUTABLE")
    if not executable:
        return None
    source = image.convert("RGB")
    if max(source.size) <= 2048 and source.width * source.height <= 3_000_000:
        size = tuple(round(side * 1.5) for side in source.size)
        source = source.resize(size, Image.Resampling.LANCZOS)
    payload = io.BytesIO()
    source.save(payload, "PNG")
    try:
        result = subprocess.run([executable, lang], input=payload.getvalue(),
                                capture_output=True, timeout=15, check=True)
        lines = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not lines:
        return None
    refine_chat_lines(source, lines, lang)
    text = "\n".join(fix_english_quotes(line["text"]) for line in lines)
    preview = None
    if include_preview:
        boxes = [(x1 * image.width, y1 * image.height, x2 * image.width, y2 * image.height)
                 for x1, y1, x2, y2 in (line["box"] for line in lines)]
        if regions is not None:
            regions.extend(zip(text.split("\n"), boxes))
        if word_boxes is not None:
            for index, (line, corrected) in enumerate(zip(lines, text.split("\n"))):
                source_words = line.get("words", [])
                count = len(corrected.split())
                if count and len(source_words) >= count:
                    word_boxes[index] = [
                        (x1 * image.width, y1 * image.height, x2 * image.width, y2 * image.height)
                        if entry else None
                        for entry in source_words[:count]
                        for x1, y1, x2, y2 in [entry or (0, 0, 0, 0)]
                    ]
        preview = mark_lines(image, boxes)
    return text, preview


def recognize(store, user, data, lang="rus+eng", via_bot=False, regions=None, word_boxes=None, handwriting=False, tessdata=None):
    if lang not in LANGUAGES:
        raise Problem("Допустимые языки: rus, eng, rus+eng.", 400)
    if len(data) > MAX_BYTES:
        raise Problem("Максимальный размер файла — 5 МБ.", 413)
    if not WORKERS.acquire(blocking=False):
        raise Problem("Сервис занят. Повторите запрос через несколько секунд.", 503)

    bucket = None
    try:
        bucket = store.debit(user, via_bot)
        try:
            with Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP"]) as image:
                if image.width * image.height > Image.MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise Problem("Нужно одно изображение размером до 16 мегапикселей.", 400)
                corrected = ImageOps.exif_transpose(image).convert("RGBA")
                background = Image.new("RGBA", corrected.size, "white")
                background.alpha_composite(corrected)

        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise Problem("Загрузите корректный файл JPEG, PNG или WebP до 16 мегапикселей.", 400) from exc
        if handwriting:
            grayscale = ImageOps.autocontrast(background.convert("L"), cutoff=1)
            if max(grayscale.size) < 1400:
                grayscale = grayscale.resize(tuple(round(side * 1.5) for side in grayscale.size),
                                             Image.Resampling.LANCZOS)
            payload = io.BytesIO()
            grayscale.save(payload, "PNG", compress_level=1)
            return run_ocr(payload.getvalue(), lang, background if via_bot else None,
                           regions, word_boxes, input_size=grayscale.size, modes=("6", "11"))
        vision = run_vision(background, lang, via_bot, regions, word_boxes)
        if vision is not None:
            return vision
        grayscale = background.convert("L")
        stats = ImageStat.Stat(grayscale)
        mostly_dark = stats.median[0] < 65 and sum(grayscale.histogram()[175:]) > grayscale.width * grayscale.height * 0.003
        if mostly_dark:
            enhanced = grayscale
            if max(grayscale.size) <= 2200 and grayscale.width * grayscale.height <= 3_000_000:
                enhanced = grayscale.resize(tuple(round(side * 1.5) for side in grayscale.size),
                                            Image.Resampling.LANCZOS)
            normalized = io.BytesIO()
            enhanced.point(lambda pixel: 0 if pixel > 130 else 255).save(normalized, "PNG", compress_level=1)
            result = run_ocr(normalized.getvalue(), lang, background if via_bot else None,
                             regions, word_boxes, input_size=enhanced.size, tessdata=tessdata)
            if result[0]:
                return result
        normalized = io.BytesIO()
        grayscale.save(normalized, "PNG", compress_level=1)
        return run_ocr(normalized.getvalue(), lang, background if via_bot else None,
                       regions, word_boxes, tessdata=tessdata)
    except BaseException:
        if bucket is not None:
            store.restore(user, bucket)
        raise
    finally:
        WORKERS.release()


def create_app(store):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES + 64 * 1024

    @app.errorhandler(Problem)
    def handle_problem(exc):
        return jsonify(error=str(exc)), exc.status

    @app.errorhandler(413)
    def handle_large(_exc):
        return jsonify(error="Максимальный размер файла — 5 МБ."), 413

    def account():
        scheme, _, key = request.headers.get("Authorization", "").partition(" ")
        result = store.authenticate(key) if scheme.lower() == "bearer" and key else None
        if result is None:
            raise Problem("Нужен действующий API-ключ.", 401)
        return result

    @app.get("/dashboard/cover.jpg")
    def dashboard_cover():
        return send_from_directory(Path(__file__).parent, "welcome.jpg", max_age=86400)

    @app.get("/dashboard/favicon.png")
    def favicon_png():
        return send_from_directory(Path(__file__).parent, "favicon-tab.png", max_age=86400)

    @app.get("/favicon.svg")
    @app.get("/favicon.ico")
    @app.get("/apple-touch-icon.png")
    def favicon():
        return send_from_directory(Path(__file__).parent, request.path[1:], max_age=86400)

    @app.get("/dashboard/")
    @app.get("/docs/")
    def dashboard():
        filename = "docs.html" if request.path == "/docs/" else "dashboard.html"
        response = send_from_directory(Path(__file__).parent, filename)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/v1/history")
    def history():
        user, _ = account()
        return {"requests": store.api_history(user)}, 200, {"Cache-Control": "no-store"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/balance")
    def balance():
        user, _ = account()
        free, credits = store.balance(user)
        return {"credits": credits, "free": free}, 200, {"Cache-Control": "no-store"}

    @app.post("/v1/ocr")
    def ocr():
        user, _ = account()
        lang = request.args.get("lang", "rus+eng")
        started = time.monotonic()
        status = 500
        try:
            if request.mimetype == "multipart/form-data":
                image = request.files.get("image")
                if image is None:
                    raise Problem("Добавьте изображение в поле image.", 400)
                data = image.read(MAX_BYTES + 1)
            else:
                data = request.get_data()
            result, _ = recognize(store, user, data, lang)
            status = 200
            return {"text": result, "language": lang}, 200, {"Cache-Control": "no-store"}
        except Problem as exc:
            status = exc.status
            raise
        except RequestEntityTooLarge:
            status = 413
            raise
        finally:
            store.record_request(user, status, lang if lang in LANGUAGES else "invalid",
                             round((time.monotonic() - started) * 1000))

    return app

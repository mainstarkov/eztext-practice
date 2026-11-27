"""Ledger for Telegram gifts and previously issued redemption codes."""

import hashlib
import re
import secrets

GIFT_PACKS = {15: 30, 25: 50}
REQUESTS_PER_ORDER = 100


class PaidMessages:
    def __init__(self, store):
        self.store = store

    def credit_gift(self, recipient, sender, message_id, sent_at, stars):
        credits = GIFT_PACKS.get(stars)
        if not credits or recipient <= 0 or sender <= 0 or sender == recipient or message_id <= 0:
            return 0
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO accounts(id, trial) VALUES (?, 0)", (sender,))
            recorded = db.execute("""INSERT INTO paid_message_orders
                (ticket, buyer, expires, recipient, message_id, redeemed, stars)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(recipient, buyer, message_id) DO NOTHING""",
                ("gift-" + secrets.token_hex(16), sender, sent_at, recipient, message_id, stars)).rowcount
            if not recorded:
                return 0
            db.execute("UPDATE accounts SET credits=credits+? WHERE id=?", (credits, sender))
        return credits

    def redeem(self, buyer, code):
        code = code.strip().upper()
        if not re.fullmatch(r"[0-9A-F]{24}", code):
            return False
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            claimed = db.execute("""UPDATE paid_message_orders SET redeemed=1
                WHERE buyer=? AND code_hash=? AND redeemed=0 RETURNING stars""", (buyer, digest)).fetchone()
            if not claimed:
                return False
            credits = GIFT_PACKS.get(claimed[0], REQUESTS_PER_ORDER)
            db.execute("UPDATE accounts SET credits=credits+? WHERE id=?", (credits, buyer))
        return credits

import os
import time
import re
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, List, Tuple

import httpx
from bs4 import BeautifulSoup
import html as html_lib

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# CONFIG
# =========================
MAILTM_BASE = "https://api.mail.tm"
DB_PATH = "data.db"

PORT = int(os.environ.get("PORT", "10000"))
POLL_EVERY_SECONDS = int(os.environ.get("POLL_EVERY_SECONDS", "12"))

CONTACT_USERNAME = "@platoonleaderr"

# =========================
# BUTTONS
# =========================
BTN_NEW = "📧 Generate new mail"
BTN_CURRENT = "📌 Current mail"
BTN_DELETE = "🗑️ Remove current mail"

BTN_LIST = "📜 My saved mails"
BTN_REUSE = "♻️ Reuse a mail"
BTN_RENAME = "✏️ Rename a mail"
BTN_DELETE_SAVED = "🧨 Delete saved mail"

BTN_HELP = "❓ Help / Contact"
BTN_BACK = "⬅️ Back to menu"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(BTN_NEW), KeyboardButton(BTN_CURRENT)],
        [KeyboardButton(BTN_LIST), KeyboardButton(BTN_REUSE)],
        [KeyboardButton(BTN_RENAME), KeyboardButton(BTN_DELETE_SAVED)],
        [KeyboardButton(BTN_DELETE), KeyboardButton(BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MODE_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(BTN_BACK)]],
    resize_keyboard=True,
    is_persistent=True,
)

# =========================
# DATABASE
# =========================
def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # mailboxes with per-user sequence user_seq + label
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_seq INTEGER NOT NULL,
            address TEXT NOT NULL,
            password TEXT NOT NULL,
            token TEXT NOT NULL,
            label TEXT DEFAULT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(chat_id, user_seq),
            UNIQUE(chat_id, address)
        )
        """
    )

    # migration: add label if older db
    try:
        cur.execute("ALTER TABLE mailboxes ADD COLUMN label TEXT")
    except Exception:
        pass

    # active mailbox per user
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS active_mailbox (
            chat_id INTEGER PRIMARY KEY,
            mailbox_id INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # seen messages
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_messages (
            chat_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY(chat_id, message_id)
        )
        """
    )

    # Backfill user_seq for older rows where user_seq is NULL (safe no-op if already ok)
    # If your DB was created by old versions, user_seq might exist but some rows may be NULL.
    cur.execute("SELECT DISTINCT chat_id FROM mailboxes")
    chat_ids = [r[0] for r in cur.fetchall()]
    for cid in chat_ids:
        cur.execute(
            "SELECT id, user_seq FROM mailboxes WHERE chat_id=? ORDER BY created_at ASC, id ASC",
            (cid,),
        )
        rows = cur.fetchall()
        seq = 0
        for row_id, user_seq in rows:
            if user_seq is None:
                seq += 1
                cur.execute("UPDATE mailboxes SET user_seq=? WHERE id=?", (seq, row_id))
            else:
                try:
                    seq = max(seq, int(user_seq))
                except Exception:
                    pass

    con.commit()
    con.close()


def db_save_mailbox(chat_id: int, address: str, password: str, token: str) -> int:
    """Assign per-user sequence (user_seq): 1,2,3... for each user"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT COALESCE(MAX(user_seq), 0) + 1 FROM mailboxes WHERE chat_id=?", (chat_id,))
    next_seq = cur.fetchone()[0]

    cur.execute(
        """
        INSERT OR IGNORE INTO mailboxes(chat_id, user_seq, address, password, token, label, created_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
        """,
        (chat_id, next_seq, address, password, token, int(time.time())),
    )
    con.commit()

    cur.execute("SELECT id FROM mailboxes WHERE chat_id=? AND address=?", (chat_id, address))
    mailbox_id = cur.fetchone()[0]
    con.close()
    return mailbox_id


def db_list_mailboxes(chat_id: int) -> List[Tuple[int, int, str, Optional[str], int]]:
    """[(db_id, user_seq, address, label, created_at), ...]"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, user_seq, address, label, created_at FROM mailboxes WHERE chat_id=? ORDER BY user_seq DESC",
        (chat_id,),
    )
    rows = cur.fetchall()
    con.close()
    return rows


def db_get_mailbox_by_seq(chat_id: int, user_seq: int) -> Optional[Tuple[int, str, Optional[str]]]:
    """(db_id, address, label) or None"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, address, label FROM mailboxes WHERE chat_id=? AND user_seq=?",
        (chat_id, user_seq),
    )
    row = cur.fetchone()
    con.close()
    return row


def db_set_active_mailbox(chat_id: int, mailbox_id: int) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO active_mailbox(chat_id, mailbox_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
          mailbox_id=excluded.mailbox_id,
          updated_at=excluded.updated_at
        """,
        (chat_id, mailbox_id, int(time.time())),
    )
    con.commit()
    con.close()


def db_get_active_mailbox(chat_id: int) -> Optional[Tuple[int, str, str]]:
    """(mailbox_db_id, address, token)"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        SELECT m.id, m.address, m.token
        FROM active_mailbox a
        JOIN mailboxes m ON m.id = a.mailbox_id
        WHERE a.chat_id = ?
        """,
        (chat_id,),
    )
    row = cur.fetchone()
    con.close()
    return row


def db_delete_active_mailbox_only(chat_id: int) -> None:
    """Remove only active selection; saved list remains"""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM active_mailbox WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()


def db_delete_saved_by_seq(chat_id: int, user_seq: int) -> bool:
    """Delete saved mailbox by per-user ID (user_seq). Returns True if deleted."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # Find db_id
    cur.execute("SELECT id FROM mailboxes WHERE chat_id=? AND user_seq=?", (chat_id, user_seq))
    row = cur.fetchone()
    if not row:
        con.close()
        return False
    db_id = row[0]

    # If it is active, remove active pointer too
    cur.execute("DELETE FROM active_mailbox WHERE chat_id=? AND mailbox_id=?", (chat_id, db_id))
    cur.execute("DELETE FROM mailboxes WHERE chat_id=? AND id=?", (chat_id, db_id))

    con.commit()
    con.close()
    return True


def db_set_label_by_seq(chat_id: int, user_seq: int, label: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("UPDATE mailboxes SET label=? WHERE chat_id=? AND user_seq=?", (label, chat_id, user_seq))
    changed = cur.rowcount > 0
    con.commit()
    con.close()
    return changed


def db_get_token(chat_id: int, mailbox_id: int) -> Optional[str]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT token FROM mailboxes WHERE chat_id=? AND id=?", (chat_id, mailbox_id))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def db_is_seen(chat_id: int, message_id: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM seen_messages WHERE chat_id=? AND message_id=? LIMIT 1",
        (chat_id, message_id),
    )
    row = cur.fetchone()
    con.close()
    return row is not None


def db_mark_seen(chat_id: int, message_id: str) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO seen_messages(chat_id, message_id, seen_at) VALUES (?, ?, ?)",
        (chat_id, message_id, int(time.time())),
    )
    con.commit()
    con.close()


# =========================
# MAIL.TM
# =========================
async def mailtm_get_random_domain(client: httpx.AsyncClient) -> str:
    r = await client.get(f"{MAILTM_BASE}/domains?page=1")
    r.raise_for_status()
    items = r.json().get("hydra:member", [])
    if not items:
        raise RuntimeError("No domains available right now.")
    for d in items:
        if d.get("isActive"):
            return d["domain"]
    return items[0]["domain"]


async def mailtm_create_account_and_token(client: httpx.AsyncClient) -> Tuple[str, str, str]:
    domain = await mailtm_get_random_domain(client)
    address = f"{secrets.token_hex(6)}@{domain}"
    password = secrets.token_urlsafe(12)

    r1 = await client.post(f"{MAILTM_BASE}/accounts", json={"address": address, "password": password})
    if r1.status_code >= 400:
        address = f"{secrets.token_hex(7)}@{domain}"
        r1 = await client.post(f"{MAILTM_BASE}/accounts", json={"address": address, "password": password})
    r1.raise_for_status()

    r2 = await client.post(f"{MAILTM_BASE}/token", json={"address": address, "password": password})
    r2.raise_for_status()
    token = r2.json()["token"]
    return address, password, token


async def mailtm_list_messages(client: httpx.AsyncClient, token: str):
    r = await client.get(
        f"{MAILTM_BASE}/messages?page=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json().get("hydra:member", [])


async def mailtm_read_message(client: httpx.AsyncClient, token: str, msg_id: str):
    r = await client.get(
        f"{MAILTM_BASE}/messages/{msg_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()


# =========================
# HTML -> TEXT + OTP detector
# =========================
OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

def html_to_text(html_content) -> str:
    if isinstance(html_content, list):
        html_content = "\n".join([x for x in html_content if isinstance(x, str)])
    if not isinstance(html_content, str):
        return ""

    html_content = html_lib.unescape(html_content)
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def extract_otp(text: str) -> Optional[str]:
    # take first match (most common)
    m = OTP_RE.search(text or "")
    return m.group(1) if m else None


def format_full_message(msg: dict) -> str:
    frm = (msg.get("from") or {}).get("address", "unknown")
    subj = msg.get("subject") or "(no subject)"
    created = msg.get("createdAt") or ""

    text = (msg.get("text") or "").strip()
    if not text:
        text = html_to_text(msg.get("html"))
    if not text:
        text = "(empty body)"

    otp = extract_otp(text)

    # Telegram length limit
    if len(text) > 3200:
        text = text[:3200] + "\n…(truncated)"

    otp_line = f"🔐 <b>OTP:</b> <code>{otp}</code>\n\n" if otp else ""

    return (
        f"📩 <b>New Email</b>\n"
        f"<b>From:</b> {frm}\n"
        f"<b>Subject:</b> {subj}\n"
        f"<b>Date:</b> {created}\n\n"
        f"{otp_line}"
        f"{text}"
    )


async def create_new_mail_for_chat(chat_id: int) -> str:
    async with httpx.AsyncClient(timeout=25) as client:
        address, password, token = await mailtm_create_account_and_token(client)
    mailbox_id = db_save_mailbox(chat_id, address, password, token)
    db_set_active_mailbox(chat_id, mailbox_id)
    return address


# =========================
# TELEGRAM UI HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    # BACK
    if txt == BTN_BACK:
        context.user_data.pop("reuse_mode", None)
        context.user_data.pop("rename_mode", None)
        context.user_data.pop("delete_saved_mode", None)
        await update.message.reply_text("Menu ✅", reply_markup=MAIN_MENU)
        return

    # /start => direct new mail
    if txt.lower() == "/start":
        await update.message.reply_text("Creating…", reply_markup=MAIN_MENU)
        address = await create_new_mail_for_chat(chat_id)
        await update.message.reply_text(
            f"📧 <b>Your mail:</b>\n<code>{address}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if txt == BTN_HELP:
        await update.message.reply_text(f"Contact: {CONTACT_USERNAME}", reply_markup=MAIN_MENU)
        return

    if txt == BTN_CURRENT:
        active = db_get_active_mailbox(chat_id)
        if not active:
            await update.message.reply_text("No active mail. Tap “Generate new mail”.", reply_markup=MAIN_MENU)
            return
        _db_id, address, _token = active
        await update.message.reply_text(
            f"📌 <b>Current mail:</b>\n<code>{address}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if txt == BTN_NEW:
        await update.message.reply_text("Creating…", reply_markup=MAIN_MENU)
        address = await create_new_mail_for_chat(chat_id)
        await update.message.reply_text(
            f"📧 <b>Your new mail:</b>\n<code>{address}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_MENU,
        )
        return

    if txt == BTN_DELETE:
        active = db_get_active_mailbox(chat_id)
        if not active:
            await update.message.reply_text("No active mail.", reply_markup=MAIN_MENU)
            return
        db_delete_active_mailbox_only(chat_id)
        await update.message.reply_text("✅ Current mail removed (saved list is still there).", reply_markup=MAIN_MENU)
        return

    if txt == BTN_LIST:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("No saved mails yet.", reply_markup=MAIN_MENU)
            return

        active = db_get_active_mailbox(chat_id)
        active_db_id = active[0] if active else None

        lines = ["📜 <b>Your saved mails</b>\n"]
        for db_id, user_seq, addr, label, _created_at in rows[:30]:
            mark = "✅" if db_id == active_db_id else "▫️"
            label_txt = f" — <b>{html_lib.escape(label)}</b>" if label else ""
            lines.append(f"{mark} <code>{addr}</code>{label_txt}\n<b>ID:</b> <code>{user_seq}</code>\n")

        lines.append("Reuse: tap ♻️ Reuse → send ID\nRename: tap ✏️ Rename → send: ID Name\nDelete saved: tap 🧨 Delete saved → send ID")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
        return

    if txt == BTN_REUSE:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("No saved mails to reuse.", reply_markup=MAIN_MENU)
            return
        context.user_data["reuse_mode"] = True
        await update.message.reply_text(
            "♻️ Send the <b>ID</b> you want to reuse.\nExample: <code>1</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=MODE_MENU,
        )
        return

    if txt == BTN_RENAME:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("No saved mails to rename.", reply_markup=MAIN_MENU)
            return
        context.user_data["rename_mode"] = True
        await update.message.reply_text(
            "✏️ Send like this:\n<code>ID Name</code>\nExample: <code>2 Facebook</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=MODE_MENU,
        )
        return

    if txt == BTN_DELETE_SAVED:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("No saved mails to delete.", reply_markup=MAIN_MENU)
            return
        context.user_data["delete_saved_mode"] = True
        await update.message.reply_text(
            "🧨 Send the <b>ID</b> you want to delete from saved list.\nExample: <code>3</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=MODE_MENU,
        )
        return

    # -------- Modes processing --------
    if context.user_data.get("reuse_mode"):
        if txt.isdigit():
            seq = int(txt)
            found = db_get_mailbox_by_seq(chat_id, seq)
            if not found:
                await update.message.reply_text("Invalid ID. Try again.", reply_markup=MODE_MENU)
                return
            mailbox_db_id, address, _label = found
            db_set_active_mailbox(chat_id, mailbox_db_id)
            context.user_data.pop("reuse_mode", None)
            await update.message.reply_text(
                f"✅ Reusing:\n<code>{address}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=MAIN_MENU,
            )
            return
        await update.message.reply_text("Send numeric ID or tap Back.", reply_markup=MODE_MENU)
        return

    if context.user_data.get("delete_saved_mode"):
        if txt.isdigit():
            seq = int(txt)
            ok = db_delete_saved_by_seq(chat_id, seq)
            if not ok:
                await update.message.reply_text("Invalid ID. Try again.", reply_markup=MODE_MENU)
                return
            context.user_data.pop("delete_saved_mode", None)
            await update.message.reply_text("✅ Deleted from saved list.", reply_markup=MAIN_MENU)
            return
        await update.message.reply_text("Send numeric ID or tap Back.", reply_markup=MODE_MENU)
        return

    if context.user_data.get("rename_mode"):
        parts = txt.split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            seq = int(parts[0])
            name = parts[1].strip()
            if len(name) > 25:
                name = name[:25]
            ok = db_set_label_by_seq(chat_id, seq, name)
            if not ok:
                await update.message.reply_text("Invalid ID. Try again.", reply_markup=MODE_MENU)
                return
            context.user_data.pop("rename_mode", None)
            await update.message.reply_text("✅ Renamed successfully.", reply_markup=MAIN_MENU)
            return
        await update.message.reply_text("Format: ID Name (example: 2 Facebook) or tap Back.", reply_markup=MODE_MENU)
        return

    # fallback
    await update.message.reply_text("Use menu buttons 👇", reply_markup=MAIN_MENU)


# =========================
# AUTO-FORWARD (JobQueue)
# =========================
async def poll_all_chats(context: ContextTypes.DEFAULT_TYPE) -> None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT chat_id, mailbox_id FROM active_mailbox")
    actives = cur.fetchall()
    con.close()

    if not actives:
        return

    async with httpx.AsyncClient(timeout=25) as client:
        for chat_id, mailbox_id in actives:
            token = db_get_token(chat_id, mailbox_id)
            if not token:
                continue

            try:
                msgs = await mailtm_list_messages(client, token)
            except Exception:
                continue

            # send unseen messages oldest-first
            new_ids = []
            for m in msgs:
                mid = m.get("id")
                if mid and not db_is_seen(chat_id, mid):
                    new_ids.append(mid)

            for mid in reversed(new_ids):
                try:
                    full = await mailtm_read_message(client, token, mid)
                    text = format_full_message(full)
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
                    db_mark_seen(chat_id, mid)
                except Exception:
                    continue


# =========================
# RENDER PORT SERVER
# =========================
class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

def run_port_server():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


# =========================
# MAIN
# =========================
def main():
    init_db()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

    # Start Render port server
    threading.Thread(target=run_port_server, daemon=True).start()

    app = Application.builder().token(bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, handle_text))

    # Auto-forward emails
    app.job_queue.run_repeating(
        poll_all_chats,
        interval=POLL_EVERY_SECONDS,
        first=5
    )

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

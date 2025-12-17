import os
import time
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, List, Tuple

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters

# =========================
# CONFIG
# =========================
MAILTM_BASE = "https://api.mail.tm"
DB_PATH = "data.db"

PORT = int(os.environ.get("PORT", "10000"))  # Render Web Service needs an open port
POLL_EVERY_SECONDS = int(os.environ.get("POLL_EVERY_SECONDS", "12"))

CONTACT_USERNAME = "@platoonleaderr"

# =========================
# BUTTONS (NO COMMANDS)
# =========================
BTN_NEW = "📧 Generate new mail"
BTN_DELETE = "🗑️ Delete current mail"
BTN_LIST = "📜 My saved mails"
BTN_REUSE = "♻️ Reuse a mail"
BTN_HELP = "❓ Help / Contact"
BTN_BACK = "⬅️ Back to menu"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(BTN_NEW), KeyboardButton(BTN_DELETE)],
        [KeyboardButton(BTN_LIST), KeyboardButton(BTN_REUSE)],
        [KeyboardButton(BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

REUSE_MENU = ReplyKeyboardMarkup(
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

    # mailboxes table (per user sequence via user_seq)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_seq INTEGER NOT NULL,
            address TEXT NOT NULL,
            password TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(chat_id, user_seq),
            UNIQUE(chat_id, address)
        )
        """
    )

    # migrate older db (if your old table didn't have user_seq)
    try:
        cur.execute("ALTER TABLE mailboxes ADD COLUMN user_seq INTEGER")
        # After adding column, existing rows will have NULL; we'll backfill below.
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

    # Backfill user_seq for any rows where user_seq is NULL (for old DBs)
    # Assign per-user sequence ordered by created_at, id.
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
                cur.execute(
                    "UPDATE mailboxes SET user_seq=? WHERE id=?",
                    (seq, row_id),
                )
            else:
                # keep existing, but ensure seq moves forward
                try:
                    seq = max(seq, int(user_seq))
                except Exception:
                    pass

    con.commit()
    con.close()


def db_save_mailbox(chat_id: int, address: str, password: str, token: str) -> int:
    """
    Saves mailbox and assigns a per-user sequence number (user_seq)
    so each user sees IDs: 1,2,3... (not global)
    Returns internal DB primary key id (mailbox_id)
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT COALESCE(MAX(user_seq), 0) + 1 FROM mailboxes WHERE chat_id=?", (chat_id,))
    next_seq = cur.fetchone()[0]

    cur.execute(
        """
        INSERT OR IGNORE INTO mailboxes(chat_id, user_seq, address, password, token, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chat_id, next_seq, address, password, token, int(time.time())),
    )
    con.commit()

    cur.execute("SELECT id FROM mailboxes WHERE chat_id=? AND address=?", (chat_id, address))
    mailbox_id = cur.fetchone()[0]
    con.close()
    return mailbox_id


def db_list_mailboxes(chat_id: int) -> List[Tuple[int, int, str, int]]:
    """
    Returns:
      [(db_id, user_seq, address, created_at), ...]
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, user_seq, address, created_at FROM mailboxes WHERE chat_id=? ORDER BY user_seq DESC",
        (chat_id,),
    )
    rows = cur.fetchall()
    con.close()
    return rows


def db_get_mailbox_by_seq(chat_id: int, user_seq: int) -> Optional[Tuple[int, str]]:
    """
    Lookup mailbox by per-user ID (user_seq)
    Returns (db_id, address) or None
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, address FROM mailboxes WHERE chat_id=? AND user_seq=?",
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
    """
    returns (mailbox_db_id, address, token)
    """
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
    """
    Removes active selection only; saved mails remain for reuse
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM active_mailbox WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()


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


def format_full_message(msg: dict) -> str:
    frm = (msg.get("from") or {}).get("address", "unknown")
    subj = msg.get("subject") or "(no subject)"
    created = msg.get("createdAt") or ""

    text = (msg.get("text") or "").strip()
    if not text and msg.get("html"):
        text = "(HTML-only email. Text version not available.)"

    if len(text) > 3500:
        text = text[:3500] + "\n…(truncated)"

    return (
        f"📩 <b>New Email</b>\n"
        f"<b>From:</b> {frm}\n"
        f"<b>Subject:</b> {subj}\n"
        f"<b>Date:</b> {created}\n\n"
        f"{text or '(empty body)'}"
    )


async def create_new_mail_for_chat(chat_id: int) -> str:
    async with httpx.AsyncClient(timeout=25) as client:
        address, password, token = await mailtm_create_account_and_token(client)
    mailbox_id = db_save_mailbox(chat_id, address, password, token)
    db_set_active_mailbox(chat_id, mailbox_id)
    return address


# =========================
# TELEGRAM HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

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
        await update.message.reply_text("✅ Current mail removed.", reply_markup=MAIN_MENU)
        return

    if txt == BTN_LIST:
        rows = db_list_mailboxes(chat_id)
        if not rows:
            await update.message.reply_text("No saved mails yet.", reply_markup=MAIN_MENU)
            return

        active = db_get_active_mailbox(chat_id)
        active_db_id = active[0] if active else None

        lines = ["📜 <b>Your saved mails</b>\n"]
        for db_id, user_seq, addr, _created_at in rows[:30]:
            mark = "✅" if db_id == active_db_id else "▫️"
            lines.append(f"{mark} <code>{addr}</code>\n<b>ID:</b> <code>{user_seq}</code>\n")
        lines.append("To reuse: tap “♻️ Reuse a mail”, then send the ID.")
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
            reply_markup=REUSE_MENU,
        )
        return

    if txt == BTN_BACK:
        context.user_data.pop("reuse_mode", None)
        await update.message.reply_text("Menu ✅", reply_markup=MAIN_MENU)
        return

    # reuse mode
    if context.user_data.get("reuse_mode"):
        if txt.isdigit():
            seq = int(txt)
            found = db_get_mailbox_by_seq(chat_id, seq)
            if not found:
                await update.message.reply_text("Invalid ID. Try again.", reply_markup=REUSE_MENU)
                return

            mailbox_db_id, address = found
            db_set_active_mailbox(chat_id, mailbox_db_id)
            context.user_data.pop("reuse_mode", None)

            await update.message.reply_text(
                f"✅ Reusing:\n<code>{address}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=MAIN_MENU,
            )
            return

        await update.message.reply_text("Send numeric ID or tap Back.", reply_markup=REUSE_MENU)
        return

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

    # Start server so Render sees an open port
    threading.Thread(target=run_port_server, daemon=True).start()

    app = Application.builder().token(bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT | filters.COMMAND, handle_text))

    app.job_queue.run_repeating(poll_all_chats, interval=POLL_EVERY_SECONDS, first=5)
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

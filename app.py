import os
import json
import time
import random
import string
import asyncio
import imaplib
import email
import threading
import re
from datetime import datetime
from email.utils import parseaddr
from email.header import decode_header

from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CLAIM_PASSWORD = os.getenv("DEFAULT_CLAIM_PASSWORD", "change-me")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set")

# === FILES ===
DB_FILE = "db.json"
EMAIL_CONFIG_FILE = "email_config.json"

user_sessions = {}
event_loop = asyncio.get_event_loop()
db_lock = threading.Lock()

# === LOAD CONFIG ===
def load_email_config():
    with open(EMAIL_CONFIG_FILE, "r") as f:
        return json.load(f)

# === JSON DB (thread-safe) ===
def load_db():
    with db_lock:
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except:
            return {"emails": {}, "users": {}}

def save_db(db):
    with db_lock:
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=2)

# === COMMANDS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Temp Mail Bot\n\n"
        "/create - new email\n"
        "/list - your emails\n"
        "/delete - delete email\n"
        "/claim email pass\n"
        "/domains - domains"
    )

async def domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_email_config()
    await update.message.reply_text("\n".join(config.keys()))

async def list_mails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    uid = str(update.effective_user.id)

    user = db["users"].get(uid, {})
    if not user.get("emails"):
        await update.message.reply_text("No emails.")
        return

    msg = ""
    for mail in user["emails"]:
        msg += mail + "\n"

    await update.message.reply_text(msg)

async def delete_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    uid = str(update.effective_user.id)

    buttons = []
    for mail in db["users"].get(uid, {}).get("emails", {}):
        buttons.append([InlineKeyboardButton(mail, callback_data=f"delete:{mail}")])

    if not buttons:
        await update.message.reply_text("No mails.")
        return

    await update.message.reply_text(
        "Select:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("/claim email password")
        return

    email_id, password = args
    db = load_db()

    entry = db["emails"].get(email_id)
    if not entry:
        await update.message.reply_text("Not found.")
        return

    if not entry.get("protected") or entry.get("password") == password:
        uid = str(update.effective_user.id)

        db["emails"][email_id]["user_id"] = uid
        db["users"].setdefault(uid, {"emails": {}})
        db["users"][uid]["emails"][email_id] = {"created": datetime.now().isoformat()}

        save_db(db)
        await update.message.reply_text(f"Owned: {email_id}")
    else:
        await update.message.reply_text("Wrong password.")

async def create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_email_config()

    buttons = [
        [InlineKeyboardButton(d, callback_data=f"domain:{d}")]
        for d in config.keys()
    ]

    await update.message.reply_text(
        "Choose domain:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# === CALLBACK ===
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    uid = str(query.from_user.id)
    db = load_db()

    if data.startswith("domain:"):
        domain = data.split(":")[1]
        user_sessions[uid] = {"domain": domain}

        buttons = [
            [InlineKeyboardButton("Random", callback_data="type:random")],
            [InlineKeyboardButton("Custom", callback_data="type:custom")]
        ]

        await query.edit_message_text("Type:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("type:"):
        mode = data.split(":")[1]
        domain = user_sessions[uid]["domain"]

        if mode == "random":
            while True:
                name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
                email_id = f"{name}@{domain}"
                if email_id not in db["emails"]:
                    break

            db["emails"][email_id] = {
                "user_id": uid,
                "created": datetime.now().isoformat()
            }

            db["users"].setdefault(uid, {"emails": {}})
            db["users"][uid]["emails"][email_id] = {}

            save_db(db)

            await query.edit_message_text(f"Email: {email_id}")

        elif mode == "custom":
            user_sessions[uid]["awaiting"] = True
            await query.edit_message_text("Send name:")

    elif data.startswith("delete:"):
        email_id = data.split(":")[1]

        if db["emails"].get(email_id, {}).get("user_id") == uid:
            db["emails"].pop(email_id)
            db["users"][uid]["emails"].pop(email_id, None)
            save_db(db)
            await query.edit_message_text(f"Deleted {email_id}")
        else:
            await query.edit_message_text("Not yours")

# === TEXT HANDLER ===
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    session = user_sessions.get(uid)

    if session and session.get("awaiting"):
        db = load_db()
        domain = session["domain"]

        name = re.sub(r'[^a-z0-9]', '', update.message.text.lower())
        email_id = f"{name}@{domain}"

        if email_id in db["emails"]:
            await update.message.reply_text("Taken.")
        else:
            db["emails"][email_id] = {
                "user_id": uid,
                "created": datetime.now().isoformat(),
                "protected": True,
                "password": DEFAULT_CLAIM_PASSWORD
            }

            db["users"].setdefault(uid, {"emails": {}})
            db["users"][uid]["emails"][email_id] = {}

            save_db(db)

            await update.message.reply_text(f"Email: {email_id}")

        user_sessions.pop(uid, None)

# === EMAIL PROCESSING ===
def extract_links(text):
    return re.findall(r'https?://\S+', text)

def extract_otp(text):
    return re.findall(r'\b\d{4,8}\b', text)

def clean_html(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text()

async def send_message(chat_id, text):
    await app.bot.send_message(chat_id=chat_id, text=text[:4000])

def check_emails(domain, config):
    while True:
        try:
            db = load_db()

            imap = imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"])
            imap.login(config["email_user"], config["email_pass"])
            imap.select("inbox")

            status, messages = imap.search(None, "UNSEEN")

            for num in messages[0].split():
                status, msg_data = imap.fetch(num, "(RFC822)")

                for part in msg_data:
                    if isinstance(part, tuple):
                        msg = email.message_from_bytes(part[1])

                        to_email = parseaddr(msg.get("To"))[1].lower()
                        uid = db["emails"].get(to_email, {}).get("user_id")

                        if not uid:
                            continue

                        subject = msg.get("Subject", "")
                        body = ""

                        if msg.is_multipart():
                            for p in msg.walk():
                                if p.get_content_type() == "text/plain":
                                    body = p.get_payload(decode=True).decode(errors="ignore")
                                    break
                        else:
                            body = msg.get_payload(decode=True).decode(errors="ignore")

                        text = f"{subject}\n\n{body}"

                        asyncio.run_coroutine_threadsafe(
                            send_message(int(uid), text),
                            event_loop
                        )

            imap.logout()

        except Exception as e:
            print("IMAP error:", e)

        time.sleep(30)

# === START ===
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("create", create))
app.add_handler(CommandHandler("list", list_mails))
app.add_handler(CommandHandler("delete", delete_mail))
app.add_handler(CommandHandler("domains", domains))
app.add_handler(CommandHandler("claim", claim))

app.add_handler(CallbackQueryHandler(button_callback))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

for domain, cfg in load_email_config().items():
    threading.Thread(target=check_emails, args=(domain, cfg), daemon=True).start()

print("Bot running...")
app.run_polling()

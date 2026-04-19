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

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CLAIM_PASSWORD = os.getenv("DEFAULT_CLAIM_PASSWORD", "change-me")
DB_FILE = "db.json"
EMAIL_CONFIG_FILE = "email_config.json"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing")

user_sessions = {}
db_lock = threading.Lock()
app = None
main_loop = None


def load_email_config():
    with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_db():
    with db_lock:
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"emails": {}, "users": {}}
        except json.JSONDecodeError:
            return {"emails": {}, "users": {}}


def save_db(db):
    with db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)


def extract_links(text):
    return re.findall(r'(https?://\S+)', text or "")


def extract_otp(text):
    return re.findall(r'\b\d{4,8}\b', text or "")


def clean_html_to_view(html_content):
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup(["script", "style", "meta", "title", "head"]):
        tag.decompose()

    for a in soup.find_all("a"):
        href = a.get("href")
        if href:
            label = a.get_text(" ", strip=True)
            a.string = f"{label} ({href})" if label else href

    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n+", "\n", text).strip()


def decode_mime_header(value):
    if not value:
        return ""
    decoded_parts = decode_header(value)
    final = []
    for part, enc in decoded_parts:
        if isinstance(part, bytes):
            final.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            final.append(part)
    return "".join(final)


async def send_large_message(chat_id, text, prefix=""):
    limit = 3500
    message = (prefix or "") + (text or "")

    if len(message) <= limit:
        await app.bot.send_message(chat_id=chat_id, text=message)
        return

    parts = [message[i:i + limit] for i in range(0, len(message), limit)]
    for part in parts:
        await app.bot.send_message(chat_id=chat_id, text=part)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Temp Mail Bot.\n\n"
        "Commands:\n"
        "/create - create a temp mail\n"
        "/list - list your temp mails\n"
        "/delete - delete a temp mail\n"
        "/claim email@domain.com password - claim an email\n"
        "/domains - show available domains"
    )


async def domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_email_config()
    await update.message.reply_text("Available domains:\n" + "\n".join(config.keys()))


async def list_mails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    uid = str(update.effective_user.id)
    user_data = db["users"].get(uid, {})

    if not user_data or not user_data.get("emails"):
        await update.message.reply_text("You do not have any temp mails yet.")
        return

    lines = ["Your temp mails:\n"]
    for mail, details in user_data["emails"].items():
        protected = "yes" if details.get("protected") else "no"
        lines.append(f"{mail} | protected: {protected}")

    await update.message.reply_text("\n".join(lines))


async def delete_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    uid = str(update.effective_user.id)
    user_data = db["users"].get(uid, {})
    buttons = []

    for mail in user_data.get("emails", {}):
        buttons.append([InlineKeyboardButton(mail, callback_data=f"delete:{mail}")])

    if not buttons:
        await update.message.reply_text("No mails to delete.")
        return

    await update.message.reply_text(
        "Select the email to delete:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Usage:\n/claim email@domain.com password")
        return

    email_id, password = args
    db = load_db()
    entry = db["emails"].get(email_id)

    if not entry:
        await update.message.reply_text("This email is not registered.")
        return

    if (not entry.get("protected")) or (entry.get("password") == password):
        uid = str(update.effective_user.id)
        db["emails"][email_id]["user_id"] = uid
        db["users"].setdefault(uid, {"emails": {}})
        db["users"][uid]["emails"][email_id] = {
            "created": datetime.now().isoformat(),
            "protected": True
        }
        save_db(db)
        await update.message.reply_text(f"You now own: {email_id}")
    else:
        await update.message.reply_text("Incorrect password.")


async def create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_email_config()
    buttons = [
        [InlineKeyboardButton(domain, callback_data=f"domain:{domain}")]
        for domain in config.keys()
    ]
    await update.message.reply_text(
        "Choose a domain:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = str(query.from_user.id)
    db = load_db()

    if data.startswith("domain:"):
        domain = data.split(":", 1)[1]
        user_sessions[uid] = {"domain": domain}
        buttons = [
            [InlineKeyboardButton("Random", callback_data="type:random")],
            [InlineKeyboardButton("Custom", callback_data="type:custom")]
        ]
        await query.edit_message_text(
            "Choose email type:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("type:"):
        mode = data.split(":", 1)[1]
        domain = user_sessions[uid]["domain"]

        if mode == "random":
            while True:
                name = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
                email_id = f"{name}@{domain}"
                if email_id not in db["emails"]:
                    break

            db["emails"][email_id] = {
                "user_id": uid,
                "created": datetime.now().isoformat(),
                "protected": False
            }
            db["users"].setdefault(uid, {"emails": {}})
            db["users"][uid]["emails"][email_id] = {
                "created": datetime.now().isoformat(),
                "protected": False
            }
            save_db(db)
            await query.edit_message_text(f"Your email:\n{email_id}")

        elif mode == "custom":
            user_sessions[uid]["awaiting_custom"] = True
            await query.edit_message_text("Send your custom username now:")

    elif data.startswith("delete:"):
        email_id = data.split(":", 1)[1]

        if email_id in db["emails"] and db["emails"][email_id]["user_id"] == uid:
            db["emails"].pop(email_id, None)
            db["users"].setdefault(uid, {"emails": {}})
            db["users"][uid]["emails"].pop(email_id, None)
            save_db(db)
            await query.edit_message_text(f"Deleted: {email_id}")
        else:
            await query.edit_message_text("You do not own this email.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    session = user_sessions.get(uid)

    if session and session.get("awaiting_custom"):
        db = load_db()
        domain = session["domain"]
        raw_name = update.message.text.strip().lower()
        name = re.sub(r"[^a-z0-9._-]", "", raw_name)

        if not name:
            await update.message.reply_text("Invalid username. Use letters and numbers.")
            return

        email_id = f"{name}@{domain}"

        if email_id in db["emails"]:
            await update.message.reply_text(
                f"{email_id} is taken.\nUse /claim {email_id} password"
            )
        else:
            db["emails"][email_id] = {
                "user_id": uid,
                "created": datetime.now().isoformat(),
                "protected": True,
                "password": DEFAULT_CLAIM_PASSWORD
            }
            db["users"].setdefault(uid, {"emails": {}})
            db["users"][uid]["emails"][email_id] = {
                "created": datetime.now().isoformat(),
                "protected": True
            }
            save_db(db)
            await update.message.reply_text(f"Your email:\n{email_id}")

        user_sessions.pop(uid, None)


def extract_target_email(msg, raw_message, domain):
    for header in ["To", "Delivered-To", "Envelope-To", "X-Original-To"]:
        value = msg.get(header)
        if value:
            parsed = parseaddr(value)[1].lower()
            if parsed.endswith(f"@{domain}"):
                return parsed

    for line in raw_message.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            if key.strip().lower() in {"envelope-to", "delivered-to", "x-original-to"}:
                parsed = parseaddr(val.strip())[1].lower()
                if parsed.endswith(f"@{domain}"):
                    return parsed
    return ""


def build_message_text(msg):
    subject = decode_mime_header(msg.get("Subject", ""))
    from_ = decode_mime_header(msg.get("From", ""))

    body_text = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition.lower():
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            if ctype == "text/plain" and not body_text:
                body_text = payload.decode(errors="ignore")
            elif ctype == "text/html" and not body_html:
                body_html = payload.decode(errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            if msg.get_content_type() == "text/plain":
                body_text = payload.decode(errors="ignore")
            elif msg.get_content_type() == "text/html":
                body_html = payload.decode(errors="ignore")

    final_body = body_text.strip()
    if body_html:
        viewable_text = clean_html_to_view(body_html)
        if len(viewable_text) > len(final_body):
            final_body = viewable_text

    links = extract_links(final_body)
    otps = extract_otp(final_body)

    text = (
        f"New Mail\n"
        f"From: {from_}\n"
        f"Subject: {subject}\n\n"
        f"{final_body}"
    )

    if links:
        text += "\n\nLinks:\n" + "\n".join(links[:10])

    if otps:
        text += "\n\nOTPs Found:\n" + ", ".join(otps[:10])

    return text


def check_emails(domain, config):
    while True:
        try:
            db = load_db()

            imap = imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"])
            imap.login(config["email_user"], config["email_pass"])
            imap.select("INBOX")

            status, messages = imap.search(None, "UNSEEN")
            if status == "OK":
                for num in messages[0].split():
                    status, msg_data = imap.fetch(num, "(RFC822)")
                    if status != "OK":
                        continue

                    for part in msg_data:
                        if not isinstance(part, tuple):
                            continue

                        raw_bytes = part[1]
                        raw_text = raw_bytes.decode(errors="ignore")
                        msg = email.message_from_bytes(raw_bytes)

                        to_email = extract_target_email(msg, raw_text, domain)
                        if not to_email:
                            continue

                        uid = db["emails"].get(to_email, {}).get("user_id")
                        if not uid:
                            continue

                        text = build_message_text(msg)

                        asyncio.run_coroutine_threadsafe(
                            send_large_message(chat_id=int(uid), text=text),
                            main_loop
                        )

            imap.logout()

        except Exception as e:
            print(f"[{domain}] IMAP error: {e}", flush=True)

        time.sleep(30)


async def post_init(application):
    global main_loop
    main_loop = asyncio.get_running_loop()

    config = load_email_config()
    for domain, cfg in config.items():
        thread = threading.Thread(
            target=check_emails,
            args=(domain, cfg),
            daemon=True
        )
        thread.start()
        print(f"Started IMAP watcher for {domain}", flush=True)


def main():
    global app

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create))
    app.add_handler(CommandHandler("list", list_mails))
    app.add_handler(CommandHandler("delete", delete_mail))
    app.add_handler(CommandHandler("domains", domains))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot is starting...", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()

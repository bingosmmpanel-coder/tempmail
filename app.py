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
from http.server import BaseHTTPRequestHandler, HTTPServer

from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_CLAIM_PASSWORD = os.getenv("DEFAULT_CLAIM_PASSWORD", "change-me")
PORT = int(os.getenv("PORT", "10000"))

DB_FILE = "db.json"
EMAIL_CONFIG_FILE = "email_config.json"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing")

user_sessions = {}
db_lock = threading.Lock()
imap_check_lock = threading.Lock()
app = None
main_loop = None


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/healthz"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, format, *args):
        return


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Health server listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


def load_email_config():
    with open(EMAIL_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_db():
    with db_lock:
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
        except FileNotFoundError:
            db = {"emails": {}, "users": {}, "delivered_uids": {}}
        except json.JSONDecodeError:
            db = {"emails": {}, "users": {}, "delivered_uids": {}}

        if "emails" not in db:
            db["emails"] = {}
        if "users" not in db:
            db["users"] = {}
        if "delivered_uids" not in db:
            db["delivered_uids"] = {}

        return db


def save_db(db):
    with db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2)


def is_uid_already_delivered(db, domain, uid_value):
    domain_store = db["delivered_uids"].setdefault(domain, [])
    return uid_value in domain_store


def mark_uid_delivered(db, domain, uid_value):
    domain_store = db["delivered_uids"].setdefault(domain, [])
    if uid_value not in domain_store:
        domain_store.append(uid_value)

    if len(domain_store) > 2000:
        db["delivered_uids"][domain] = domain_store[-2000:]


def extract_links(text):
    return re.findall(r"(https?://\S+)", text or "")


def extract_otp(text):
    return re.findall(r"\b\d{4,8}\b", text or "")


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
    parts = decode_header(value)
    out = []
    for part, enc in parts:
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            out.append(part)
    return "".join(out)


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
        "/domains - show available domains\n"
        "/check - force check mail now"
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


async def force_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text("Checking inbox now...")

    def run_manual_check():
        try:
            found = check_all_domains_once(trigger_user_id=uid, only_user_id=uid)
            text = "Check complete."
            if found > 0:
                text += f" Found {found} new mail(s)."
            else:
                text += " No new mail found."
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=int(uid), text=text),
                main_loop
            )
        except Exception as e:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=int(uid), text=f"Manual check failed: {e}"),
                main_loop
            )

    threading.Thread(target=run_manual_check, daemon=True).start()


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
            await update.message.reply_text("Invalid username. Use letters, numbers, ., _, -")
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


def extract_target_email(msg, raw_message, domain, db=None):
    candidates = []

    for header in ["To", "Delivered-To", "Envelope-To", "X-Original-To", "Cc", "Bcc"]:
        value = msg.get(header)
        if value:
            parsed = parseaddr(value)[1].lower()
            if parsed:
                candidates.append(parsed)

    for line in raw_message.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            if key.strip().lower() in {
                "envelope-to", "delivered-to", "x-original-to", "to", "cc", "bcc"
            }:
                parsed = parseaddr(val.strip())[1].lower()
                if parsed:
                    candidates.append(parsed)

    for candidate in candidates:
        if candidate.endswith(f"@{domain}"):
            return candidate

    if db:
        owned_emails = db.get("emails", {}).keys()
        raw_lower = raw_message.lower()
        for owned in owned_emails:
            if owned.endswith(f"@{domain}") and owned.lower() in raw_lower:
                return owned.lower()

    return ""


def build_message_text(msg, received_email):
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
        f"Received On: {received_email}\n"
        f"From: {from_}\n"
        f"Subject: {subject}\n\n"
        f"{final_body}"
    )

    if links:
        text += "\n\nLinks:\n" + "\n".join(links[:10])

    if otps:
        text += "\n\nOTPs Found:\n" + ", ".join(otps[:10])

    return text


def get_message_uid(imap, msg_num):
    uid_status, uid_data = imap.fetch(msg_num, "(UID)")
    if uid_status != "OK" or not uid_data:
        return None

    for item in uid_data:
        if isinstance(item, tuple):
            text = item[0].decode(errors="ignore")
            match = re.search(r"UID\s+(\d+)", text)
            if match:
                return match.group(1)

    return None


def scan_single_domain(domain, config, trigger_user_id=None, only_user_id=None):
    db = load_db()
    found_count = 0

    imap = imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"])
    imap.login(config["email_user"], config["email_pass"])
    imap.select("INBOX")

    status, messages = imap.search(None, "ALL")
    if status != "OK":
        imap.logout()
        return found_count

    for msg_num in messages[0].split():
        uid_value = get_message_uid(imap, msg_num)
        if not uid_value:
            continue

        if is_uid_already_delivered(db, domain, uid_value):
            continue

        status, msg_data = imap.fetch(msg_num, "(RFC822)")
        if status != "OK":
            continue

        for part in msg_data:
            if not isinstance(part, tuple):
                continue

            raw_bytes = part[1]
            raw_text = raw_bytes.decode(errors="ignore")
            msg = email.message_from_bytes(raw_bytes)

            to_email = extract_target_email(msg, raw_text, domain, db=db)
            if not to_email:
                print(f"[{domain}] could not detect recipient for UID={uid_value}", flush=True)
                continue

            owner_id = db["emails"].get(to_email, {}).get("user_id")
            if not owner_id:
                print(f"[{domain}] recipient {to_email} not owned in db for UID={uid_value}", flush=True)
                continue

            if only_user_id and str(owner_id) != str(only_user_id):
                continue

            text = build_message_text(msg, to_email)

            asyncio.run_coroutine_threadsafe(
                send_large_message(chat_id=int(owner_id), text=text),
                main_loop
            )

            mark_uid_delivered(db, domain, uid_value)
            save_db(db)
            found_count += 1
            print(f"[{domain}] delivered UID={uid_value} to {to_email}", flush=True)

    imap.logout()

    if trigger_user_id and found_count == 0:
        print(f"Manual check by {trigger_user_id}: no new mail in {domain}", flush=True)

    return found_count


def check_all_domains_once(trigger_user_id=None, only_user_id=None):
    if not imap_check_lock.acquire(blocking=False):
        if trigger_user_id:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(
                    chat_id=int(trigger_user_id),
                    text="A mail check is already running. Please wait a few seconds."
                ),
                main_loop
            )
        return 0

    try:
        config = load_email_config()
        total_found = 0

        for domain, cfg in config.items():
            try:
                total_found += scan_single_domain(
                    domain=domain,
                    config=cfg,
                    trigger_user_id=trigger_user_id,
                    only_user_id=only_user_id
                )
            except Exception as e:
                print(f"[{domain}] IMAP error: {e}", flush=True)
                if trigger_user_id:
                    asyncio.run_coroutine_threadsafe(
                        app.bot.send_message(
                            chat_id=int(trigger_user_id),
                            text=f"Check failed for {domain}: {e}"
                        ),
                        main_loop
                    )

        return total_found
    finally:
        imap_check_lock.release()


def check_emails_loop():
    while True:
        try:
            check_all_domains_once()
        except Exception as e:
            print(f"Background check error: {e}", flush=True)

        time.sleep(30)


async def post_init(application):
    global main_loop
    main_loop = asyncio.get_running_loop()

    thread = threading.Thread(target=check_emails_loop, daemon=True)
    thread.start()
    print("Started background IMAP watcher", flush=True)


def main():
    global app

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("create", create))
    app.add_handler(CommandHandler("list", list_mails))
    app.add_handler(CommandHandler("delete", delete_mail))
    app.add_handler(CommandHandler("domains", domains))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CommandHandler("check", force_check))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    async def runner():
        global main_loop
        main_loop = asyncio.get_running_loop()
        await post_init(app)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        print("Bot is starting...", flush=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(runner())
    loop.run_forever()


if __name__ == "__main__":
    main()

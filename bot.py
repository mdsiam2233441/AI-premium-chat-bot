#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 Claude AI Telegram Bot — everything in a single file (bot.py)

Features:
  • 👤 Account system (Telegram ID = login, auto-registration, logout = delete account)
  • 💎 Premium (admin can grant it, or users redeem premium codes)
  • 🔑 Personal API key setup (validated before saving)
  • 🎭 6 modes (General, Coding, Writer, Translator, Study, OTT Guide)
  • 🖼 Ask questions about photos
  • 🌈 Colorful buttons and a neatly styled menu
  • 🛒 Premium plans with automatic payment verification (bKash/Nagad/Rocket SMS webhook)
  • 👑 Admin panel with buttons: API key, channel logo, channel link, premium contact,
       free limit, stats, grant premium, generate codes, broadcast
  • 👑 Admin commands (grant, revoke, gencode, ban, unban, stats, broadcast)

Install:  pip install "python-telegram-bot>=20" anthropic aiohttp
Run:      python bot.py
"""

# ══════════════════════════════════════════════════════════════
# STEP 1: Configuration (fill in your details here, or use environment variables)
# ══════════════════════════════════════════════════════════════
import os

BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_BOT_TOKEN"                      # token from @BotFather
OWNER_API_KEY = os.getenv("ANTHROPIC_API_KEY") or "YOUR_ANTHROPIC_API_KEY"  # from console.anthropic.com
ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS") or "123456789").split(",") if x.strip()}  # your Telegram ID(s)
MODEL = os.getenv("CLAUDE_MODEL") or "claude-sonnet-5-5"                    # Claude model to use
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT") or 10)                 # daily message limit for free users
PREMIUM_CONTACT = os.getenv("PREMIUM_CONTACT") or "@your_username"          # who to contact to buy premium
# Premium plans: number of days -> price in TK
PLANS = {1: 50, 7: 250, 28: 400, 90: 600, 120: 1200}
PAY_METHODS = {"bkash": "bKash", "nagad": "Nagad", "rocket": "Rocket"}
MAX_HISTORY = 20                                                            # number of messages kept per conversation
DB_PATH = os.getenv("DB_PATH") or "bot.db"                                   # on Railway, point this to a Volume path (e.g. /data/bot.db)

# ══════════════════════════════════════════════════════════════
# STEP 2: Imports
# ══════════════════════════════════════════════════════════════
import asyncio
import base64
import functools
import html
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import anthropic
from aiohttp import web
from anthropic import AsyncAnthropic
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("claude-bot")
esc = html.escape

# ══════════════════════════════════════════════════════════════
# STEP 3: Modes and system prompts
# ══════════════════════════════════════════════════════════════
SYSTEM_BASE = (
    "You are a friendly, smart assistant inside a Telegram bot. "
    "Reply in the same language the user writes in. "
    "Keep answers clear and well organised. Do not use markdown headings or tables; "
    "use short paragraphs, simple emoji and bullet points with the • character. "
    "For code, put it in a plain block and keep explanations short."
)

MODES = {
    "general":   ("🟢 General Assistant", "Help with any everyday question or task."),
    "code":      ("💻 Coding",            "You are an expert programmer. Write correct, clean code and explain bugs briefly."),
    "writer":    ("✍️ Writer",            "You are a creative writer and editor. Help write posts, stories, captions, emails and newsletters."),
    "translate": ("🌐 Translator",        "You are a translator. Translate between Bengali, English, Hindi and other languages naturally."),
    "study":     ("📚 Study Helper",      "You are a patient tutor. Explain step by step with simple examples."),
    "ott":       ("🎬 OTT Guide",         "You are an entertainment guide for movies and web series on OTT platforms. Suggest titles and explain plots without spoilers."),
}

# ══════════════════════════════════════════════════════════════
# STEP 4: Database (SQLite)
# ══════════════════════════════════════════════════════════════
def q(sql, args=(), fetch=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, args)
        if fetch == "one":
            result = cur.fetchone()
        elif fetch == "all":
            result = cur.fetchall()
        else:
            result = None
        conn.commit()
        return result
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            joined TEXT,
            premium_until TEXT,
            api_key TEXT,
            mode TEXT DEFAULT 'general',
            used_today INTEGER DEFAULT 0,
            used_day TEXT,
            banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            days INTEGER,
            price INTEGER,
            trx_id TEXT,
            status TEXT,
            how TEXT,
            created TEXT,
            approved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sms_payments(
            trx_id TEXT PRIMARY KEY,
            amount REAL,
            sender TEXT,
            method TEXT,
            received_at TEXT,
            order_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS codes(
            code TEXT PRIMARY KEY,
            days INTEGER,
            used_by INTEGER,
            used_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc)


def today():
    return now().strftime("%Y-%m-%d")


def get_setting(key, default=None):
    row = q("SELECT value FROM settings WHERE key=?", (key,), "one")
    return row["value"] if row and row["value"] else default


def set_setting(key, value):
    q("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))


def del_setting(key):
    q("DELETE FROM settings WHERE key=?", (key,))


def get_owner_key():
    """API key set from the admin panel wins; otherwise fall back to the file/env value."""
    return get_setting("owner_api_key") or OWNER_API_KEY


def get_free_limit():
    try:
        return int(get_setting("free_limit") or FREE_DAILY_LIMIT)
    except ValueError:
        return FREE_DAILY_LIMIT


def get_contact():
    return get_setting("premium_contact") or PREMIUM_CONTACT


def get_user(uid):
    return q("SELECT * FROM users WHERE user_id=?", (uid,), "one")


def register(tg_user):
    """Create the account if it doesn't exist (auto-registration); otherwise refresh the name."""
    q(
        "INSERT OR IGNORE INTO users(user_id, username, full_name, joined, used_day) VALUES(?,?,?,?,?)",
        (tg_user.id, tg_user.username or "", tg_user.full_name or "", now().isoformat(), today()),
    )
    q(
        "UPDATE users SET username=?, full_name=? WHERE user_id=?",
        (tg_user.username or "", tg_user.full_name or "", tg_user.id),
    )
    return get_user(tg_user.id)


def is_premium(u):
    if u["user_id"] in ADMIN_IDS:
        return True
    if not u["premium_until"]:
        return False
    return datetime.fromisoformat(u["premium_until"]) > now()


def add_premium(uid, days):
    u = get_user(uid)
    base = now()
    if u and u["premium_until"]:
        cur = datetime.fromisoformat(u["premium_until"])
        if cur > base:
            base = cur
    until = (base + timedelta(days=days)).isoformat()
    q("UPDATE users SET premium_until=? WHERE user_id=?", (until, uid))
    return until


def fmt_date(iso):
    return datetime.fromisoformat(iso).strftime("%d-%m-%Y")


def used_count(u):
    return u["used_today"] if u["used_day"] == today() else 0


def bump_usage(uid):
    u = get_user(uid)
    if u["used_day"] == today():
        q("UPDATE users SET used_today=used_today+1 WHERE user_id=?", (uid,))
    else:
        q("UPDATE users SET used_today=1, used_day=? WHERE user_id=?", (today(), uid))


def load_history(uid):
    rows = q(
        "SELECT role, content FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (uid, MAX_HISTORY),
        "all",
    )
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    while msgs and msgs[0]["role"] != "user":  # the first message must always be from the user
        msgs.pop(0)
    return msgs


def save_history(uid, user_text, bot_text):
    q("INSERT INTO history(user_id, role, content) VALUES(?,?,?)", (uid, "user", user_text))
    q("INSERT INTO history(user_id, role, content) VALUES(?,?,?)", (uid, "assistant", bot_text))
    q(
        "DELETE FROM history WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?)",
        (uid, uid, MAX_HISTORY),
    )


def clear_history(uid):
    q("DELETE FROM history WHERE user_id=?", (uid,))

# ══════════════════════════════════════════════════════════════
# STEP 5: Colorful keyboards and text design
# ══════════════════════════════════════════════════════════════
def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def kb_main(uid=None):
    rows = [
        [B("🟢 New Chat", "new"), B("🟣 Change Mode", "modes")],
        [B("🔵 Account", "account"), B("🟡 Premium", "premium")],
        [B("🟠 Set API Key", "apikey"), B("🔴 Help", "help")],
    ]
    link = get_setting("channel_link")
    if link:
        rows.append([InlineKeyboardButton("📢 Join Our Channel", url=link)])
    if uid in ADMIN_IDS:
        rows.append([B("👑 Admin Panel", "adm:home")])
    return InlineKeyboardMarkup(rows)


def kb_home():
    return InlineKeyboardMarkup([[B("🏠 Main Menu", "home")]])


def kb_after_chat():
    return InlineKeyboardMarkup([[B("🗑 New Chat", "new"), B("🏠 Menu", "home")]])


def kb_modes(current):
    rows = []
    items = list(MODES.items())
    for i in range(0, len(items), 2):
        row = []
        for key, (label, _) in items[i:i + 2]:
            row.append(B(("✅ " if key == current else "") + label, f"mode:{key}"))
        rows.append(row)
    rows.append([B("🏠 Main Menu", "home")])
    return InlineKeyboardMarkup(rows)


def kb_account(u):
    rows = [
        [B("🔑 Set/Change API Key", "apikey"), B("🎟 Redeem Code", "redeem")],
    ]
    if u["api_key"]:
        rows.append([B("🧹 Remove API Key", "delkey")])
    rows.append([B("🚪 Logout (Delete Account)", "del_ask")])
    rows.append([B("🏠 Main Menu", "home")])
    return InlineKeyboardMarkup(rows)


def kb_premium():
    return InlineKeyboardMarkup([
        [B("🛒 Buy Premium", "buy")],
        [B("🎟 Redeem Code", "redeem"), B("🔑 Use My Own API Key", "apikey")],
        [B("🏠 Main Menu", "home")],
    ])


LINE = "━━━━━━━━━━━━━━━━━━"


def welcome_text(u):
    return (
        f"🌈 <b>Welcome, {esc(u['full_name'] or 'friend')}!</b>\n{LINE}\n"
        "🤖 I'm your smart assistant, powered by <b>Claude</b>.\n\n"
        "✨ <b>What I can do:</b>\n"
        "• 💬 Answer any question\n"
        "• 💻 Write and debug code\n"
        "• ✍️ Help with writing, emails and captions\n"
        "• 🌐 Translate text\n"
        "• 📚 Help with studying\n"
        "• 🖼 Analyze photos you send\n\n"
        f"{LINE}\n👇 Just type a message, or use the menu below."
    )


def account_text(u):
    if u["user_id"] in ADMIN_IDS:
        status = "👑 Admin (unlimited)"
    elif is_premium(u):
        status = f"💎 Premium (valid until {fmt_date(u['premium_until'])})"
    else:
        status = "🆓 Free"
    key = f"✅ Set (…{esc(u['api_key'][-4:])})" if u["api_key"] else "❌ Not set"
    unlimited = is_premium(u) or bool(u["api_key"])
    usage = "∞ Unlimited" if unlimited else f"{used_count(u)}/{get_free_limit()}"
    return (
        f"👤 <b>Your Account</b>\n{LINE}\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"🙍 Name: {esc(u['full_name'] or '-')}\n"
        f"📅 Joined: {fmt_date(u['joined'])}\n"
        f"🏷 Status: {status}\n"
        f"🔑 API Key: {key}\n"
        f"🎭 Mode: {MODES[u['mode']][0]}\n"
        f"📊 Today's usage: {usage}\n{LINE}"
    )


def plan_label(days):
    return f"{days} Day{'s' if days > 1 else ''}"


def plans_text():
    return "\n".join(f"• {plan_label(d)} — <b>{p} TK</b>" for d, p in PLANS.items())


def premium_text():
    return (
        f"💎 <b>Premium Benefits</b>\n{LINE}\n"
        "✅ Unlimited messages (no daily limit)\n"
        "✅ All modes and photo analysis\n"
        "✅ Priority support\n\n"
        f"🛒 <b>Plans</b>\n{plans_text()}\n\n"
        "Tap <b>Buy Premium</b>, pay, send your Transaction ID — it activates automatically.\n\n"
        "<b>Other ways:</b>\n"
        "• 🎟 Redeem a premium code\n"
        "• 🔑 Use your own Anthropic API key (unlimited, billed to your key)\n"
        f"• 📞 Contact the admin: {esc(get_contact())}\n"
        f"{LINE}"
    )


HELP_TEXT = (
    f"❓ <b>Help</b>\n{LINE}\n"
    "• Send any message and Claude will reply\n"
    "• 🖼 Send a photo with a caption to ask about it\n"
    "• /new — start a new chat\n"
    "• /menu — main menu\n"
    "• /account — your account\n"
    "• /redeem CODE — use a premium code\n"
    f"{LINE}\n"
    "🔒 When you send an API key, the bot deletes your message right away."
)

# ══════════════════════════════════════════════════════════════
# STEP 6: Helper functions
# ══════════════════════════════════════════════════════════════
async def show(query, text, kb):
    """When a button is pressed, edit the same message to show the new screen."""
    try:
        if query.message and query.message.photo:  # the welcome screen may carry the channel logo
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def reply_long(message, text, kb=None):
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or ["…"]
    for i, chunk in enumerate(chunks):
        await message.reply_text(chunk, reply_markup=kb if i == len(chunks) - 1 else None)


def admin_only(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await fn(update, context)
    return wrapper


def check_quota(u):
    if u["banned"]:
        return False, "🚫 Your account has been blocked."
    if is_premium(u) or u["api_key"]:
        return True, ""
    if used_count(u) >= get_free_limit():
        return False, (
            f"⏳ You've reached today's free limit ({get_free_limit()} messages).\n\n"
            "💎 Get Premium or set your own API key for unlimited use."
        )
    return True, ""

# ══════════════════════════════════════════════════════════════
# STEP 7: Chatting with Claude
# ══════════════════════════════════════════════════════════════
async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE, content, history_text):
    tg = update.effective_user
    msg = update.effective_message
    u = register(tg)

    ok, why = check_quota(u)
    if not ok:
        await msg.reply_text(why, reply_markup=kb_premium())
        return

    key = u["api_key"] or get_owner_key()
    if not key or key.startswith("YOUR_"):
        await msg.reply_text("⚠️ The admin hasn't set the bot's API key yet.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    messages = load_history(tg.id) + [{"role": "user", "content": content}]
    system = SYSTEM_BASE + "\n" + MODES.get(u["mode"], MODES["general"])[1]

    try:
        client = AsyncAnthropic(api_key=key)
        resp = await client.messages.create(
            model=MODEL, max_tokens=2000, system=system, messages=messages
        )
        answer = "".join(b.text for b in resp.content if b.type == "text").strip() or "…"
    except anthropic.AuthenticationError:
        await msg.reply_text("❌ The API key is invalid. Please set it again with 🔑.", reply_markup=kb_premium())
        return
    except anthropic.RateLimitError:
        await msg.reply_text("⏳ Too many requests right now. Please try again shortly.")
        return
    except Exception as e:
        log.exception("Claude error: %s", e)
        await msg.reply_text("⚠️ Something went wrong. Please try again in a moment.")
        return

    save_history(tg.id, history_text, answer)
    bump_usage(tg.id)
    await reply_long(msg, answer, kb_after_chat())

# ══════════════════════════════════════════════════════════════
# STEP 8: User commands and buttons
# ══════════════════════════════════════════════════════════════
async def send_welcome(message, u):
    """Welcome screen; shows the channel logo on top when the admin has set one."""
    kb = kb_main(u["user_id"])
    logo = get_setting("channel_logo")
    if logo:
        try:
            await message.reply_photo(logo, caption=welcome_text(u), parse_mode=ParseMode.HTML, reply_markup=kb)
            return
        except Exception as e:
            log.warning("Could not send channel logo: %s", e)
    await message.reply_text(welcome_text(u), parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = register(update.effective_user)
    context.user_data.pop("state", None)
    await send_welcome(update.message, u)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    clear_history(update.effective_user.id)
    await update.message.reply_text("✅ New chat started! Ask your question 👇", reply_markup=kb_main(update.effective_user.id))


async def cmd_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = register(update.effective_user)
    await update.message.reply_text(account_text(u), parse_mode=ParseMode.HTML, reply_markup=kb_account(u))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb_home())


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    if context.args:
        await redeem_code(update, context.args[0])
    else:
        context.user_data["state"] = "await_code"
        await update.message.reply_text("🎟 Send your premium code:")


async def redeem_code(update: Update, code_text):
    code = code_text.strip().upper()
    row = q("SELECT * FROM codes WHERE code=?", (code,), "one")
    if not row or row["used_by"]:
        await update.message.reply_text("❌ This code is invalid or has already been used.", reply_markup=kb_home())
        return
    until = add_premium(update.effective_user.id, row["days"])
    q("UPDATE codes SET used_by=?, used_at=? WHERE code=?", (update.effective_user.id, now().isoformat(), code))
    await update.message.reply_text(
        f"🎉 Congratulations! 💎 Premium is now active.\n📅 Valid until: {fmt_date(until)}",
        reply_markup=kb_main(update.effective_user.id),
    )


async def verify_key(key):
    """Check an Anthropic API key with a tiny request. Returns (ok, error_message)."""
    if not key.startswith("sk-ant-"):
        return False, "That doesn't look like a valid Anthropic API key (it starts with sk-ant-…)."
    try:
        await AsyncAnthropic(api_key=key).messages.create(
            model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "hi"}]
        )
    except anthropic.AuthenticationError:
        return False, "The API key is invalid. Please try again."
    except Exception as e:
        log.warning("Key check failed: %s", e)
        return False, "Couldn't verify the key (could be a credit or network issue)."
    return True, ""


async def save_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE, text):
    key = text.strip()
    try:
        await update.message.delete()  # for security, delete the message that contains the key
    except Exception:
        pass
    chat_id = update.effective_chat.id
    if not key.startswith("sk-ant-"):
        await context.bot.send_message(chat_id, "❌ That doesn't look like a valid Anthropic API key (it starts with sk-ant-…).", reply_markup=kb_home())
        return
    status = await context.bot.send_message(chat_id, "⏳ Verifying your API key…")
    try:
        await AsyncAnthropic(api_key=key).messages.create(
            model=MODEL, max_tokens=5, messages=[{"role": "user", "content": "hi"}]
        )
    except anthropic.AuthenticationError:
        await status.edit_text("❌ The API key is invalid. Please try again.", reply_markup=kb_home())
        return
    except Exception as e:
        log.warning("Key check failed: %s", e)
        await status.edit_text("⚠️ Couldn't verify the key (could be a credit or network issue).", reply_markup=kb_home())
        return
    q("UPDATE users SET api_key=? WHERE user_id=?", (key, update.effective_user.id))
    await status.edit_text("✅ API key saved! You now have unlimited usage 🎉", reply_markup=kb_main(update.effective_user.id))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    u = register(query.from_user)
    if u["banned"]:
        return

    if data.startswith("adm:"):
        if u["user_id"] in ADMIN_IDS:
            await admin_button(query, context, data)
        return

    if data == "home":
        context.user_data.pop("state", None)
        await show(query, welcome_text(u), kb_main(u["user_id"]))
    elif data == "new":
        clear_history(u["user_id"])
        await show(query, "✅ <b>New chat started!</b>\nType your question below 👇", kb_main(u["user_id"]))
    elif data == "modes":
        await show(query, f"🎭 <b>Choose a Mode</b>\n{LINE}\nCurrent: {MODES[u['mode']][0]}", kb_modes(u["mode"]))
    elif data.startswith("mode:"):
        key = data.split(":", 1)[1]
        if key in MODES:
            q("UPDATE users SET mode=? WHERE user_id=?", (key, u["user_id"]))
            clear_history(u["user_id"])
            await show(query, f"✅ Mode changed: <b>{MODES[key][0]}</b>\nSend a message to start 👇", kb_modes(key))
    elif data == "account":
        await show(query, account_text(u), kb_account(u))
    elif data == "premium":
        await show(query, premium_text(), kb_premium())
    elif data == "buy":
        await show(query, buy_text(), kb_plans())
    elif data.startswith("plan:"):
        days = int(data.split(":")[1])
        if days in PLANS:
            await show(query, plan_text(days), kb_plan(days))
    elif data.startswith("paid:"):
        days = int(data.split(":")[1])
        if days in PLANS:
            context.user_data["plan"] = days
            context.user_data["state"] = "await_trx"
            await show(
                query,
                f"🧾 <b>Send your Transaction ID</b>\nPlan: {plan_label(days)} — {PLANS[days]} TK\n\n"
                "Paste the TrxID from your payment confirmation SMS:",
                InlineKeyboardMarkup([[B("⬅️ Back to Plans", "buy")]]),
            )
    elif data == "help":
        await show(query, HELP_TEXT, kb_home())
    elif data == "apikey":
        context.user_data["state"] = "await_key"
        await show(
            query,
            "🔑 <b>API Key Setup</b>\n" + LINE + "\n"
            "1️⃣ Log in at console.anthropic.com\n"
            "2️⃣ Create a new key under API Keys\n"
            "3️⃣ Send the key (sk-ant-…) here\n\n"
            "🔒 I'll delete your message right after you send it.",
            kb_home(),
        )
    elif data == "delkey":
        q("UPDATE users SET api_key=NULL WHERE user_id=?", (u["user_id"],))
        u = get_user(u["user_id"])
        await show(query, "🧹 API key removed.\n\n" + account_text(u), kb_account(u))
    elif data == "redeem":
        context.user_data["state"] = "await_code"
        await show(query, "🎟 <b>Redeem Code</b>\nSend your premium code:", kb_home())
    elif data == "del_ask":
        kb = InlineKeyboardMarkup([[B("✅ Yes, delete", "del_yes"), B("❌ No", "account")]])
        await show(query, "⚠️ <b>Are you sure?</b>\nYour account, chat history and API key will all be deleted.", kb)
    elif data == "del_yes":
        clear_history(u["user_id"])
        q("DELETE FROM users WHERE user_id=?", (u["user_id"],))
        await show(query, "🚪 Logged out and account deleted.\nSend /start to begin again.", None)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    text = update.message.text or ""
    state = context.user_data.pop("state", None)
    if state == "await_key":
        await save_api_key(update, context, text)
    elif state == "await_code":
        await redeem_code(update, text)
    elif state == "await_trx":
        await handle_trx(update, context, text)
    elif state and state.startswith("adm_") and update.effective_user.id in ADMIN_IDS:
        await admin_input(update, context, state, text)
    else:
        await chat(update, context, text, text)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    state = context.user_data.pop("state", None)
    if state == "adm_logo" and update.effective_user.id in ADMIN_IDS:
        set_setting("channel_logo", update.message.photo[-1].file_id)
        await update.message.reply_text("✅ Channel logo saved! It now appears on the welcome screen.")
        await send_panel(context, update.effective_chat.id)
        return
    caption = update.message.caption or "Describe and explain this image."
    photo = update.message.photo[-1]
    file = await photo.get_file()
    data = bytes(await file.download_as_bytearray())
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": caption},
    ]
    await chat(update, context, content, f"[Image] {caption}")

# ══════════════════════════════════════════════════════════════
# STEP 8B: Premium plans, payments and automatic verification
#
# How it works:
#   1. The user picks a plan, sends money to your number, and pastes the Transaction ID (TrxID).
#   2. Your phone forwards the "money received" SMS to this bot's webhook (POST /sms).
#   3. The bot matches TrxID + amount and activates Premium automatically.
#   If the SMS hasn't arrived yet, the order stays pending and is approved the moment it does.
#   Admins can also approve/reject pending orders by hand from the admin panel.
# ══════════════════════════════════════════════════════════════
TRX_RE = re.compile(r"^[A-Z0-9]{6,20}$")


def pay_numbers():
    return {m: get_setting("pay_" + m) for m in PAY_METHODS if get_setting("pay_" + m)}


def kb_plans():
    rows, row = [], []
    for d, p in PLANS.items():
        row.append(B(f"⏱ {plan_label(d)} · {p} TK", f"plan:{d}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("⬅️ Back", "premium")])
    return InlineKeyboardMarkup(rows)


def buy_text():
    return f"🛒 <b>Choose a Plan</b>\n{LINE}\n{plans_text()}\n{LINE}\nPay by Send Money, then submit your Transaction ID."


def plan_text(days):
    price = PLANS[days]
    nums = pay_numbers()
    if not nums:
        return f"⚠️ Online payment isn't set up yet.\nPlease contact {esc(get_contact())} to buy Premium."
    lines = "\n".join(f"• {PAY_METHODS[m]}: <code>{esc(n)}</code>" for m, n in nums.items())
    return (
        f"🛒 <b>{plan_label(days)} Premium — {price} TK</b>\n{LINE}\n"
        f"1️⃣ <b>Send Money</b> exactly <b>{price} TK</b> to:\n{lines}\n\n"
        "2️⃣ Copy the <b>Transaction ID (TrxID)</b> from the confirmation SMS.\n"
        "3️⃣ Tap <b>I've Paid</b> and paste the TrxID here.\n\n"
        f"⚡ Premium activates automatically once the payment is verified.\n{LINE}"
    )


def kb_plan(days):
    rows = []
    if pay_numbers():
        rows.append([B("✅ I've Paid — Send TrxID", f"paid:{days}")])
    rows.append([B("⬅️ Back to Plans", "buy"), B("🏠 Menu", "home")])
    return InlineKeyboardMarkup(rows)


def parse_sms(blob):
    """Read a 'money received' SMS. Returns (trx_id, amount, sender, method) or None."""
    text = " ".join(blob.split())
    if not re.search(r"receiv", text, re.I):  # ignore sent / cash-out / other SMS
        return None
    m = re.search(r"(?:TrxID|TxnID|TxID|Trx\s*ID|Txn\s*ID|Transaction\s*ID)\s*[:\-]?\s*([A-Za-z0-9]{6,20})", text, re.I)
    if not m:
        return None
    a = re.search(r"Amount\s*[:\-]?\s*(?:Tk\.?|BDT|\u09f3)?\s*([\d,]+(?:\.\d+)?)", text, re.I) or \
        re.search(r"(?:Tk\.?|BDT|\u09f3)\s*:?\s*([\d,]+(?:\.\d+)?)", text, re.I)
    if not a:
        return None
    s = re.search(r"(?:from|sender)\D{0,6}(?:\+?88)?(01\d{9})", text, re.I)
    low = text.lower()
    method = "bkash" if "bkash" in low else "nagad" if "nagad" in low else "rocket" if "rocket" in low else "unknown"
    return m.group(1).upper(), float(a.group(1).replace(",", "")), (s.group(1) if s else ""), method


def settle(trx):
    """Match a received SMS payment with a pending order. Returns (status, order, sms) or None."""
    sms = q("SELECT * FROM sms_payments WHERE trx_id=? AND order_id IS NULL", (trx,), "one")
    order = q("SELECT * FROM orders WHERE trx_id=? AND status='pending' ORDER BY id LIMIT 1", (trx,), "one")
    if not sms or not order:
        return None
    if sms["amount"] + 0.001 < order["price"]:
        q("UPDATE orders SET status='rejected', how='amount too low' WHERE id=?", (order["id"],))
        return ("short", order, sms)
    q("UPDATE orders SET status='approved', how='auto', approved_at=? WHERE id=?", (now().isoformat(), order["id"]))
    q("UPDATE sms_payments SET order_id=? WHERE trx_id=?", (order["id"], trx))
    add_premium(order["user_id"], order["days"])
    return ("approved", order, sms)


def submit_trx(uid, days, trx):
    """Called when a user submits a TrxID. Returns (code, data)."""
    dup = q("SELECT 1 AS x FROM orders WHERE trx_id=? AND status IN ('pending','approved')", (trx,), "one")
    used = q("SELECT 1 AS x FROM sms_payments WHERE trx_id=? AND order_id IS NOT NULL", (trx,), "one")
    if dup or used:
        return "duplicate", None
    pending = q("SELECT COUNT(*) AS n FROM orders WHERE user_id=? AND status='pending'", (uid,), "one")["n"]
    if pending >= 3:
        return "too_many", None
    q(
        "INSERT INTO orders(user_id, days, price, trx_id, status, created) VALUES(?,?,?,?,'pending',?)",
        (uid, days, PLANS[days], trx, now().isoformat()),
    )
    res = settle(trx)
    if res:
        return res[0], res
    order = q("SELECT * FROM orders WHERE trx_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (trx,), "one")
    return "pending", order


def force_approve(order_id):
    order = q("SELECT * FROM orders WHERE id=? AND status='pending'", (order_id,), "one")
    if not order:
        return None
    q("UPDATE orders SET status='approved', how='admin', approved_at=? WHERE id=?", (now().isoformat(), order_id))
    q("UPDATE sms_payments SET order_id=? WHERE trx_id=? AND order_id IS NULL", (order_id, order["trx_id"]))
    add_premium(order["user_id"], order["days"])
    return order


def reject_order(order_id):
    order = q("SELECT * FROM orders WHERE id=? AND status='pending'", (order_id,), "one")
    if not order:
        return None
    q("UPDATE orders SET status='rejected', how='admin' WHERE id=?", (order_id,))
    return order


async def safe_send(bot, chat_id, text, kb=None):
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        log.warning("Could not message %s: %s", chat_id, e)


async def notify_approved(bot, order):
    u = get_user(order["user_id"])
    await safe_send(
        bot, order["user_id"],
        f"🎉 <b>Payment verified!</b>\n💎 {plan_label(order['days'])} Premium is now active.\n"
        f"📅 Valid until: {fmt_date(u['premium_until'])}",
        kb_main(order["user_id"]),
    )


async def notify_settle(bot, res):
    status, order, sms = res
    if status == "approved":
        await notify_approved(bot, order)
        for admin in ADMIN_IDS:
            await safe_send(
                bot, admin,
                f"✅ Auto-approved order #{order['id']}: user <code>{order['user_id']}</code>, "
                f"{plan_label(order['days'])} ({order['price']} TK), TrxID <code>{esc(order['trx_id'])}</code>",
            )
    else:
        await safe_send(
            bot, order["user_id"],
            f"❌ <b>Amount mismatch.</b> We received {sms['amount']:g} TK but this plan costs {order['price']} TK.\n"
            f"Choose a plan that matches what you paid (the same TrxID can be used again), or contact {esc(get_contact())}.",
            kb_plans(),
        )
        for admin in ADMIN_IDS:
            await safe_send(
                bot, admin,
                f"⚠️ Order #{order['id']} rejected: paid {sms['amount']:g} TK, plan costs {order['price']} TK "
                f"(TrxID <code>{esc(order['trx_id'])}</code>).",
            )


async def notify_admins_pending(bot, order):
    kb = InlineKeyboardMarkup([[B("✅ Approve", f"adm:ok:{order['id']}"), B("❌ Reject", f"adm:no:{order['id']}")]])
    for admin in ADMIN_IDS:
        await safe_send(
            bot, admin,
            f"⏳ <b>Pending order #{order['id']}</b>\nUser: <code>{order['user_id']}</code>\n"
            f"Plan: {plan_label(order['days'])} ({order['price']} TK)\nTrxID: <code>{esc(order['trx_id'])}</code>\n\n"
            "No matching payment SMS yet. It will approve automatically when the SMS arrives, "
            "or approve/reject it by hand:",
            kb,
        )


async def handle_trx(update: Update, context: ContextTypes.DEFAULT_TYPE, text):
    uid = update.effective_user.id
    days = context.user_data.get("plan")
    if days not in PLANS:
        await update.message.reply_text("Please choose a plan first.", reply_markup=kb_plans())
        return
    trx = re.sub(r"\s+", "", text).upper()
    if not TRX_RE.match(trx):
        context.user_data["state"] = "await_trx"  # keep waiting for a valid ID
        await update.message.reply_text(
            "❌ That doesn't look like a valid Transaction ID (6–20 letters/numbers). Please send it again:",
            reply_markup=kb_home(),
        )
        return
    code, data = submit_trx(uid, days, trx)
    if code == "duplicate":
        await update.message.reply_text("❌ This Transaction ID has already been used.", reply_markup=kb_home())
    elif code == "too_many":
        await update.message.reply_text(
            "⏳ You already have 3 payments waiting for verification. Please wait for them to finish.",
            reply_markup=kb_home(),
        )
    elif code in ("approved", "short"):
        await notify_settle(context.bot, data)
    else:
        await update.message.reply_text(
            "⏳ <b>Payment received for review.</b>\n"
            "We haven't seen this payment yet (this can take a minute). "
            "Premium activates automatically as soon as it's verified — you'll get a message here.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_home(),
        )
        await notify_admins_pending(context.bot, data)


async def handle_sms(bot, blob):
    """Process one forwarded SMS. Returns a short status string."""
    info = parse_sms(blob)
    if not info:
        return "ignored"
    trx, amount, sender, method = info
    if q("SELECT 1 AS x FROM sms_payments WHERE trx_id=?", (trx,), "one"):
        return "duplicate"
    q(
        "INSERT INTO sms_payments(trx_id, amount, sender, method, received_at) VALUES(?,?,?,?,?)",
        (trx, amount, sender, method, now().isoformat()),
    )
    res = settle(trx)
    if res:
        await notify_settle(bot, res)
        return "matched"
    return "saved"


def base_url():
    u = os.getenv("PUBLIC_URL")
    if u:
        return u.rstrip("/")
    d = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    return ("https://" + d) if d else "https://YOUR-DOMAIN"


async def sms_webhook(request):
    secret = get_setting("webhook_secret") or ""
    given = request.query.get("secret") or request.headers.get("X-Secret") or ""
    if not secret or not secrets.compare_digest(given, secret):
        return web.Response(status=403, text="forbidden")
    parts = [v for k, v in request.query.items() if k != "secret"]
    if request.method == "POST":
        if "form" in (request.content_type or ""):
            data = await request.post()
            parts += [str(v) for v in data.values()]
        else:
            parts.append(await request.text())
    blob = " ".join(parts)
    log.info("SMS webhook received: %s", blob[:160])
    result = await handle_sms(request.app["tg"].bot, blob)
    return web.json_response({"ok": True, "result": result})


async def start_web(app: Application):
    web_app = web.Application()
    web_app["tg"] = app
    web_app.router.add_route("*", "/sms", sms_webhook)
    web_app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT") or 8080)
    await web.TCPSite(runner, "0.0.0.0", port).start()
    app.bot_data["web_runner"] = runner
    log.info("SMS webhook listening on port %s", port)


async def stop_web(app: Application):
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()

# ══════════════════════════════════════════════════════════════
# STEP 9: Admin panel (buttons) and admin commands
# ══════════════════════════════════════════════════════════════
SETTING_KEYS = {
    "key": "owner_api_key",
    "logo": "channel_logo",
    "link": "channel_link",
    "contact": "premium_contact",
    "limit": "free_limit",
}

PROMPTS = {
    "key": "🔑 Send the bot's Anthropic API key (sk-ant-…).\nI'll verify it and delete your message.",
    "logo": "🖼 Send the channel logo as a <b>photo</b> (not as a file).",
    "link": "🔗 Send the channel link, e.g. <code>https://t.me/yourchannel</code> or <code>@yourchannel</code>.",
    "contact": "📞 Send the contact shown to users who want Premium, e.g. <code>@your_username</code>.",
    "limit": "📊 Send the free daily message limit as a number, e.g. <code>10</code>.",
    "pay_bkash": "📱 Send your <b>bKash</b> number (users will send money here), e.g. <code>01XXXXXXXXX</code>.",
    "pay_nagad": "📱 Send your <b>Nagad</b> number, e.g. <code>01XXXXXXXXX</code>.",
    "pay_rocket": "📱 Send your <b>Rocket</b> number, e.g. <code>01XXXXXXXXX</code>.",
    "testsms": "🧪 Paste a sample payment SMS (the full text). I'll show how the bot reads it — nothing is saved.",
    "grant": "💎 Send: <code>user_id days</code>\nExample: <code>123456789 30</code>",
    "codes": "🎟 Send: <code>days [count]</code>\nExample: <code>30 5</code> creates five 30-day codes.",
    "broadcast": "📢 Send the message to deliver to all users:",
}


def normalize_link(t):
    t = t.strip()
    if t.startswith("@") and len(t) > 1:
        return "https://t.me/" + t[1:]
    if t.startswith("t.me/"):
        return "https://" + t
    if t.startswith("https://"):
        return t
    return None


def make_codes(days, count):
    codes = []
    for _ in range(min(count, 50)):
        code = f"PRM-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
        q("INSERT INTO codes(code, days) VALUES(?,?)", (code, days))
        codes.append(code)
    return codes


async def do_broadcast(context, text):
    sent = 0
    for u in q("SELECT user_id FROM users WHERE banned=0", (), "all"):
        try:
            await context.bot.send_message(u["user_id"], f"📢 {text}")
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return sent


async def give_premium(context, uid, days):
    until = add_premium(uid, days)
    try:
        await context.bot.send_message(uid, f"🎉 You've been given 💎 Premium!\n📅 Valid until: {fmt_date(until)}")
    except Exception:
        pass
    return until


def stats_text():
    users = q("SELECT * FROM users", (), "all")
    total = len(users)
    premium = sum(1 for u in users if is_premium(u))
    own_key = sum(1 for u in users if u["api_key"])
    active = sum(1 for u in users if u["used_day"] == today() and u["used_today"] > 0)
    return (
        f"📊 <b>Statistics</b>\n{LINE}\n"
        f"👥 Total users: {total}\n💎 Premium: {premium}\n🔑 With own key: {own_key}\n🔥 Active today: {active}"
    )


def kb_admin():
    return InlineKeyboardMarkup([
        [B("🔑 API Key", "adm:key"), B("🖼 Channel Logo", "adm:logo")],
        [B("🔗 Channel Link", "adm:link"), B("📞 Premium Contact", "adm:contact")],
        [B("📊 Free Limit", "adm:limit"), B("📈 Stats", "adm:stats")],
        [B("💎 Grant Premium", "adm:set:grant"), B("🎟 Generate Codes", "adm:set:codes")],
        [B("💳 Payments", "adm:pay"), B("📢 Broadcast", "adm:set:broadcast")],
        [B("🏠 Main Menu", "home")],
    ])


def kb_admin_back():
    return InlineKeyboardMarkup([[B("⬅️ Back to Admin Panel", "adm:home")]])


def admin_text():
    total = q("SELECT COUNT(*) AS n FROM users", (), "one")["n"]
    key = get_owner_key()
    if key and not key.startswith("YOUR_"):
        src = "admin panel" if get_setting("owner_api_key") else "file/env"
        key_s = f"✅ …{esc(key[-4:])} ({src})"
    else:
        key_s = "❌ Not set"
    link = get_setting("channel_link")
    return (
        f"👑 <b>Admin Panel</b>\n{LINE}\n"
        f"🔑 API Key: {key_s}\n"
        f"🖼 Channel Logo: {'✅ Set' if get_setting('channel_logo') else '❌ Not set'}\n"
        f"🔗 Channel Link: {esc(link) if link else '❌ Not set'}\n"
        f"📞 Premium Contact: {esc(get_contact())}\n"
        f"📊 Free Daily Limit: {get_free_limit()}\n"
        f"👥 Users: {total}\n{LINE}\n"
        "Other commands: /revoke /ban /unban /stats"
    )


async def send_panel(context, chat_id):
    await context.bot.send_message(chat_id, admin_text(), parse_mode=ParseMode.HTML, reply_markup=kb_admin())


def pay_screen():
    nums = pay_numbers()
    lines = "\n".join(
        f"{PAY_METHODS[m]}: {esc(nums[m]) if m in nums else '❌ Not set'}" for m in PAY_METHODS
    )
    pending = q("SELECT COUNT(*) AS n FROM orders WHERE status='pending'", (), "one")["n"]
    url = f"{base_url()}/sms?secret={get_setting('webhook_secret')}"
    text = (
        f"💳 <b>Payments</b>\n{LINE}\n{lines}\n\n"
        f"🔗 <b>SMS Webhook URL</b>\n<code>{esc(url)}</code>\n\n"
        "Set your SMS-forwarder app to POST received-payment SMS to this URL. "
        "Matching payments are approved automatically.\n"
        f"⏳ Pending orders: {pending}"
    )
    kb = InlineKeyboardMarkup([
        [B("✏️ bKash", "adm:set:pay_bkash"), B("🗑", "adm:delpay:bkash")],
        [B("✏️ Nagad", "adm:set:pay_nagad"), B("🗑", "adm:delpay:nagad")],
        [B("✏️ Rocket", "adm:set:pay_rocket"), B("🗑", "adm:delpay:rocket")],
        [B("🧪 Test SMS Parser", "adm:set:testsms"), B("🔐 New Secret", "adm:secret")],
        [B("📋 Pending Orders", "adm:orders")],
        [B("⬅️ Back to Admin Panel", "adm:home")],
    ])
    return text, kb


def orders_screen():
    rows = q("SELECT * FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 6", (), "all")
    if not rows:
        text = f"📋 <b>Pending Orders</b>\n{LINE}\nNo pending orders."
    else:
        lines = "\n".join(
            f"#{r['id']} · user <code>{r['user_id']}</code> · {plan_label(r['days'])} ({r['price']} TK)\n"
            f"   TrxID: <code>{esc(r['trx_id'])}</code>"
            for r in rows
        )
        text = f"📋 <b>Pending Orders</b>\n{LINE}\n{lines}"
    kb_rows = [[B(f"✅ #{r['id']}", f"adm:ok:{r['id']}"), B(f"❌ #{r['id']}", f"adm:no:{r['id']}")] for r in rows]
    kb_rows.append([B("⬅️ Back", "adm:pay")])
    return text, InlineKeyboardMarkup(kb_rows)


def setting_screen(name):
    """Detail screen for one setting: current value + change/remove buttons."""
    if name == "key":
        key = get_owner_key()
        cur = f"✅ …{esc(key[-4:])}" if key and not key.startswith("YOUR_") else "❌ Not set"
        text = (
            f"🔑 <b>Bot API Key</b>\n{LINE}\nCurrent: {cur}\n\n"
            "This key pays for free and premium users' messages.\n"
            "Removing the panel key falls back to the key in the file/environment."
        )
    elif name == "logo":
        text = (
            f"🖼 <b>Channel Logo</b>\n{LINE}\n"
            f"Current: {'✅ Set' if get_setting('channel_logo') else '❌ Not set'}\n\n"
            "Shown at the top of the welcome screen (/start)."
        )
    elif name == "link":
        link = get_setting("channel_link")
        text = (
            f"🔗 <b>Channel Link</b>\n{LINE}\nCurrent: {esc(link) if link else '❌ Not set'}\n\n"
            "Adds a “📢 Join Our Channel” button to the main menu."
        )
    elif name == "contact":
        text = (
            f"📞 <b>Premium Contact</b>\n{LINE}\nCurrent: {esc(get_contact())}\n\n"
            "Shown on the Premium screen so users know who to contact."
        )
    else:  # limit
        text = (
            f"📊 <b>Free Daily Limit</b>\n{LINE}\nCurrent: {get_free_limit()} messages per day\n\n"
            "Premium users and users with their own key are unlimited."
        )
    kb = InlineKeyboardMarkup([
        [B("✏️ Change", f"adm:set:{name}"), B("🗑 Remove / Reset", f"adm:del:{name}")],
        [B("⬅️ Back to Admin Panel", "adm:home")],
    ])
    return text, kb


async def admin_button(query, context, data):
    parts = data.split(":")
    action = parts[1]
    if action == "home":
        context.user_data.pop("state", None)
        await show(query, admin_text(), kb_admin())
    elif action == "stats":
        await show(query, stats_text(), kb_admin_back())
    elif action == "pay":
        context.user_data.pop("state", None)
        text, kb = pay_screen()
        await show(query, text, kb)
    elif action == "delpay" and len(parts) == 3 and parts[2] in PAY_METHODS:
        del_setting("pay_" + parts[2])
        text, kb = pay_screen()
        await show(query, text, kb)
    elif action == "secret":
        set_setting("webhook_secret", secrets.token_urlsafe(16))
        text, kb = pay_screen()
        await show(query, "🔐 New secret created — update the URL in your SMS forwarder.\n\n" + text, kb)
    elif action == "orders":
        text, kb = orders_screen()
        await show(query, text, kb)
    elif action in ("ok", "no") and len(parts) == 3 and parts[2].isdigit():
        oid = int(parts[2])
        order = force_approve(oid) if action == "ok" else reject_order(oid)
        if not order:
            await show(query, "ℹ️ This order was already handled.", kb_admin_back())
            return
        if action == "ok":
            await notify_approved(context.bot, order)
            await show(query, f"✅ Approved order #{oid} — Premium activated for user {order['user_id']}.", kb_admin_back())
        else:
            await safe_send(
                context.bot, order["user_id"],
                f"❌ Your payment (TrxID <code>{esc(order['trx_id'])}</code>) couldn't be verified. "
                f"Please contact {esc(get_contact())}.",
            )
            await show(query, f"❌ Rejected order #{oid}.", kb_admin_back())
    elif action in SETTING_KEYS:
        context.user_data.pop("state", None)
        text, kb = setting_screen(action)
        await show(query, text, kb)
    elif action == "set" and len(parts) == 3 and parts[2] in PROMPTS:
        context.user_data["state"] = "adm_" + parts[2]
        kb = InlineKeyboardMarkup([[B("❌ Cancel", "adm:home")]])
        await show(query, PROMPTS[parts[2]], kb)
    elif action == "del" and len(parts) == 3 and parts[2] in SETTING_KEYS:
        del_setting(SETTING_KEYS[parts[2]])
        text, kb = setting_screen(parts[2])
        await show(query, "✅ Removed / reset.\n\n" + text, kb)


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state, text):
    """Handle the text an admin sends after pressing a panel button."""
    name = state[4:]
    chat_id = update.effective_chat.id
    text = text.strip()

    async def retry(msg):
        context.user_data["state"] = state  # keep waiting for a valid answer
        await update.message.reply_text(msg, reply_markup=kb_admin_back())

    if name == "key":
        try:
            await update.message.delete()  # remove the message that contains the key
        except Exception:
            pass
        status = await context.bot.send_message(chat_id, "⏳ Verifying the API key…")
        ok, err = await verify_key(text)
        if not ok:
            context.user_data["state"] = state
            await status.edit_text("❌ " + err, reply_markup=kb_admin_back())
            return
        set_setting("owner_api_key", text)
        await status.edit_text("✅ Bot API key saved and active.")
    elif name == "logo":
        await retry("🖼 Please send the logo as a photo.")
        return
    elif name == "link":
        link = normalize_link(text)
        if not link:
            await retry("❌ Invalid link. Use https://t.me/yourchannel or @yourchannel.")
            return
        set_setting("channel_link", link)
        await update.message.reply_text("✅ Channel link saved! The button now appears on the main menu.")
    elif name == "contact":
        if not text or len(text) > 64:
            await retry("❌ Please send a short contact (up to 64 characters).")
            return
        set_setting("premium_contact", text)
        await update.message.reply_text("✅ Premium contact saved.")
    elif name == "limit":
        if not text.isdigit():
            await retry("❌ Please send a number, e.g. 10.")
            return
        set_setting("free_limit", str(int(text)))
        await update.message.reply_text(f"✅ Free daily limit is now {int(text)}.")
    elif name.startswith("pay_"):
        digits = re.sub(r"[\s+\-]", "", text)
        if not digits.isdigit() or not 10 <= len(digits) <= 14:
            await retry("❌ Please send a valid phone number (digits only).")
            return
        set_setting(name, digits)
        await update.message.reply_text(f"✅ {PAY_METHODS[name[4:]]} number saved.")
    elif name == "testsms":
        info = parse_sms(text)
        if info:
            trx, amount, sender, method = info
            await update.message.reply_text(
                f"✅ Recognised as a received payment:\nTrxID: {trx}\nAmount: {amount:g} TK\n"
                f"Sender: {sender or 'not found'}\nMethod: {method}"
            )
        else:
            await update.message.reply_text(
                "❌ Not recognised. The SMS must say the money was received and include a TrxID and an amount."
            )
    elif name == "grant":
        try:
            uid, days = [int(x) for x in text.split()[:2]]
        except Exception:
            await retry("❌ Format: user_id days (e.g. 123456789 30).")
            return
        if not get_user(uid):
            await retry("❌ User not found (they must send /start to the bot first).")
            return
        until = await give_premium(context, uid, days)
        await update.message.reply_text(f"✅ Granted {days} days of premium to {uid} (valid until {fmt_date(until)}).")
    elif name == "codes":
        try:
            parts = [int(x) for x in text.split()[:2]]
            days, count = parts[0], (parts[1] if len(parts) > 1 else 1)
        except Exception:
            await retry("❌ Format: days [count] (e.g. 30 5).")
            return
        codes = make_codes(days, count)
        body = "\n".join(f"<code>{c}</code>" for c in codes)
        await update.message.reply_text(f"🎟 {days}-day codes:\n{body}", parse_mode=ParseMode.HTML)
    elif name == "broadcast":
        sent = await do_broadcast(context, text)
        await update.message.reply_text(f"✅ Sent to {sent} users.")
    await send_panel(context, chat_id)


@admin_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    await send_panel(context, update.effective_chat.id)


@admin_only
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid, days = int(context.args[0]), int(context.args[1])
    except Exception:
        await update.message.reply_text("Usage: /grant user_id days")
        return
    if not get_user(uid):
        await update.message.reply_text("❌ User not found (they must send /start to the bot first).")
        return
    until = await give_premium(context, uid, days)
    await update.message.reply_text(f"✅ Granted {days} days of premium to {uid} (valid until {fmt_date(until)}).")


@admin_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("Usage: /revoke user_id")
        return
    q("UPDATE users SET premium_until=NULL WHERE user_id=?", (uid,))
    await update.message.reply_text(f"✅ Premium revoked for {uid}.")


@admin_only
async def cmd_gencode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(context.args[0])
        count = int(context.args[1]) if len(context.args) > 1 else 1
    except Exception:
        await update.message.reply_text("Usage: /gencode days [count]")
        return
    codes = make_codes(days, count)
    body = "\n".join(f"<code>{c}</code>" for c in codes)
    await update.message.reply_text(f"🎟 {days}-day codes:\n{body}", parse_mode=ParseMode.HTML)


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("Usage: /ban user_id")
        return
    q("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await update.message.reply_text(f"🚫 {uid} has been blocked.")


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("Usage: /unban user_id")
        return
    q("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await update.message.reply_text(f"✅ {uid} has been unblocked.")


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(stats_text(), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast message")
        return
    sent = await do_broadcast(context, text)
    await update.message.reply_text(f"✅ Sent to {sent} users.")

# ══════════════════════════════════════════════════════════════
# STEP 10: Start the bot
# ══════════════════════════════════════════════════════════════
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("menu", "Main menu"),
        BotCommand("new", "New chat"),
        BotCommand("account", "My account"),
        BotCommand("redeem", "Redeem a premium code"),
        BotCommand("help", "Help"),
    ])
    await start_web(app)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Update error", exc_info=context.error)


def main():
    if BOT_TOKEN.startswith("YOUR_"):
        raise SystemExit("❌ Set BOT_TOKEN at the top of the file (the token from @BotFather).")
    init_db()
    if not get_setting("webhook_secret"):
        set_setting("webhook_secret", secrets.token_urlsafe(16))
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(stop_web).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("account", cmd_account))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("redeem", cmd_redeem))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("gencode", cmd_gencode))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("🚀 Bot is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

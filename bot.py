"""
EVM Wallet Generator Bot
BIP-44 HD wallets for all EVM chains (Ethereum, BSC, Polygon, Arbitrum, etc.)

Features:
  - Private chat only (ignores all group messages except the admin group)
  - Admin group: User Info (lookup by ID/username), Balance, Rate, Global Wallet Limit
  - Main menu: Profile, Settings, Bulk Wallet Generator, Balance Checker
  - Export as CSV or TG Messages (both support up to 100k, rate limited)
  - Global 28 msg/sec rate limiter with sliding window (multiple users work simultaneously)
  - Global wallet generation limit (default 100k, admin can change for everyone)
  - Startup benchmark for accurate ETA

Requirements:
    pip install python-telegram-bot eth-account mnemonic
"""

import asyncio
import csv
import logging
import math
import os
import tempfile
import time
from collections import deque
from datetime import datetime

from eth_account import Account
from mnemonic import Mnemonic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ============================================================================
# STARTUP
# ============================================================================
Account.enable_unaudited_hdwallet_features()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIG  (set BOT_TOKEN and ADMIN_GROUP_ID as Railway environment variables)
# ============================================================================
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID: int = int(os.environ.get("ADMIN_GROUP_ID", "0"))

DERIVATION_PATH = "m/44'/60'/0'/0/0"
GLOBAL_MSG_RATE_LIMIT = 28       # Telegram allows 30/sec, we use 28 for safety
BENCHMARK_SAMPLE = 30
STRENGTH_MAP = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
VALID_WORD_COUNTS = {12, 15, 18, 21, 24}
TG_WALLETS_PER_MSG = 10          # wallets per TG message chunk
TG_MSG_DELAY = 0.05              # seconds between TG message chunks (flood safety)

# ============================================================================
# GLOBAL WALLET LIMIT  (admin can change this for everyone at runtime)
# ============================================================================
_global_wallet_limit: int = 100_000

def get_global_limit() -> int:
    return _global_wallet_limit

def set_global_limit(val: int) -> None:
    global _global_wallet_limit
    _global_wallet_limit = val

# ============================================================================
# CONVERSATION STATES
# ============================================================================
ASK_COUNT, ASK_WORDS, ASK_EXPORT = range(3)
ADM_USERINFO_QUERY              = 10
ADM_SET_LIMIT_VAL               = 11

# ============================================================================
# IN-MEMORY USER STORE
# Swap for SQLite/Postgres when adding payments. Resets on redeploy.
# ============================================================================
USER_DB: dict[int, dict] = {}

def get_user(uid: int, tg_user=None) -> dict:
    if uid not in USER_DB:
        USER_DB[uid] = {
            "name": tg_user.full_name if tg_user else str(uid),
            "username": tg_user.username if tg_user else None,
            "wallets_generated": 0,
            "credits": 0,
            "balance_checked": 0,
        }
    elif tg_user:
        USER_DB[uid]["name"] = tg_user.full_name
        USER_DB[uid]["username"] = tg_user.username
    return USER_DB[uid]

def find_user_by_query(query_str: str) -> tuple[int | None, dict | None]:
    """Find a user by numeric ID or @username. Returns (uid, user_dict) or (None, None)."""
    q = query_str.strip().lstrip("@")

    # Try numeric ID first
    if q.isdigit():
        uid = int(q)
        if uid in USER_DB:
            return uid, USER_DB[uid]
        return None, None

    # Try username match (case-insensitive)
    q_lower = q.lower()
    for uid, u in USER_DB.items():
        if u["username"] and u["username"].lower() == q_lower:
            return uid, u
    return None, None

# ============================================================================
# RATE LIMITER  (28 messages/sec, bot-wide sliding window)
# ============================================================================
_msg_timestamps: deque = deque()
_rate_lock = asyncio.Lock()

async def send_safe(coro):
    """Enforce global 28 msg/sec limit. Queues if window is full."""
    while True:
        async with _rate_lock:
            now = time.monotonic()
            while _msg_timestamps and now - _msg_timestamps[0] >= 1.0:
                _msg_timestamps.popleft()
            if len(_msg_timestamps) < GLOBAL_MSG_RATE_LIMIT:
                _msg_timestamps.append(now)
                break
            sleep_for = 1.0 - (now - _msg_timestamps[0]) + 0.01
        await asyncio.sleep(sleep_for)
    return await coro

# ============================================================================
# BENCHMARK
# ============================================================================
_wallets_per_sec: float = 0.0

def _run_benchmark(sample: int = BENCHMARK_SAMPLE) -> float:
    mnemo = Mnemonic("english")
    t0 = time.perf_counter()
    for _ in range(sample):
        phrase = mnemo.generate(strength=128)
        Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
    rate = sample / (time.perf_counter() - t0)
    logger.info("Benchmark: %.1f wallets/sec", rate)
    return rate

def eta_string(count: int) -> str:
    if _wallets_per_sec <= 0:
        return "calculating..."
    secs = count / _wallets_per_sec
    if secs < 60:
        return f"~{math.ceil(secs)}s"
    return f"~{secs / 60:.1f} min"

# ============================================================================
# WALLET GENERATION
# ============================================================================
def _generate_wallets(count: int, word_count: int) -> tuple[list, float]:
    """Returns (rows, elapsed_secs). Each row: [#, address, private_key, mnemonic]."""
    mnemo = Mnemonic("english")
    strength = STRENGTH_MAP[word_count]
    rows = []
    t0 = time.perf_counter()
    for i in range(count):
        phrase = mnemo.generate(strength=strength)
        acct = Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
        rows.append([i + 1, acct.address, acct.key.hex(), phrase])
    return rows, time.perf_counter() - t0

def _write_csv(rows: list) -> str:
    """Write rows to a temp CSV and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(tmp)
        writer.writerow(["#", "address", "private_key", "mnemonic"])
        writer.writerows(rows)
        tmp.flush()
    finally:
        tmp.close()
    return tmp.name

# ============================================================================
# HELPERS
# ============================================================================
def escape_md(text) -> str:
    specials = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in str(text))

def is_admin_group(update: Update) -> bool:
    return (
        ADMIN_GROUP_ID != 0
        and update.effective_chat is not None
        and update.effective_chat.id == ADMIN_GROUP_ID
    )

def is_private(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.type == "private"

# ============================================================================
# KEYBOARDS
# ============================================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 Profile",  callback_data="menu_profile"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
        ],
        [
            InlineKeyboardButton("🪪 Bulk Wallet Generator", callback_data="menu_bulk"),
            InlineKeyboardButton("💰 Balance Checker",       callback_data="menu_balance"),
        ],
    ])

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 User Info",             callback_data="adm_userinfo"),
            InlineKeyboardButton("💰 Balance",               callback_data="adm_balance"),
        ],
        [
            InlineKeyboardButton("📊 Rate",                  callback_data="adm_rate"),
            InlineKeyboardButton("🔢 Wallet Generate Limit", callback_data="adm_wlimit"),
        ],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")
    ]])

def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="adm_home")
    ]])

# ============================================================================
# STATIC TEXTS
# ============================================================================
def welcome_user_text() -> str:
    limit = get_global_limit()
    return (
        "*EVM Wallet Generator*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Generates BIP\\-44 HD wallets compatible with all EVM chains "
        "\\(Ethereum, BSC, Polygon, Arbitrum, etc\\.\\)\n\n"
        "🔒 *Privacy:* Nothing is stored on the server\\.\n"
        f"📦 *Limit:* Up to {escape_md('{:,}'.format(limit))} wallets per batch\n\n"
        "Choose an option below\\:"
    )

WELCOME_ADMIN = (
    "*EVM Wallet Generator*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "🛡️ *Admin Panel*\n\n"
    "Use the buttons below to manage users and bot settings\\."
)

HELP_TEXT = (
    "*EVM Wallet Generator — Help*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "*Commands*\n"
    "/start \\- Open the main menu\n"
    "/help \\- Show this help message\n"
    "/cancel \\- Cancel the current session\n\n"
    "*Bulk Wallet Generator flow*\n"
    "1\\. Tap *Bulk Wallet Generator*\n"
    "2\\. Enter the number of wallets\n"
    "3\\. Choose mnemonic word count\n"
    "4\\. Choose export type \\(CSV or TG Messages\\)\n"
    "5\\. Receive your wallets\n\n"
    "*CSV columns*\n"
    "`#` \\| `address` \\| `private_key` \\| `mnemonic`\n\n"
    "*Security*\n"
    "\\- BIP\\-44 standard derivation path\n"
    "\\- Compatible with MetaMask, Trust Wallet, Rabby, Bitget, etc\\.\n"
    "\\- Store your CSV in an encrypted location\n"
    "\\- Never share your private keys or mnemonic phrases"
)

# ============================================================================
# /start
# ============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    if is_admin_group(update):
        await send_safe(update.message.reply_text(
            WELCOME_ADMIN,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=admin_menu_kb(),
        ))
        return ConversationHandler.END

    if not is_private(update):
        return ConversationHandler.END

    get_user(update.effective_user.id, update.effective_user)
    await send_safe(update.message.reply_text(
        welcome_user_text(),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(),
    ))
    return ConversationHandler.END

# ============================================================================
# MAIN MENU CALLBACKS (private)
# ============================================================================
async def menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        welcome_user_text(),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(),
    ))
    return ConversationHandler.END

async def menu_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    user = get_user(uid, update.effective_user)

    name     = escape_md(user["name"] or "N/A")
    username = escape_md("@" + user["username"] if user["username"] else "N/A")
    limit    = escape_md("{:,}".format(get_global_limit()))
    generated = escape_md("{:,}".format(user["wallets_generated"]))
    checked   = escape_md("{:,}".format(user["balance_checked"]))

    text = (
        "*👤 Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Name:* {name}\n"
        f"*User ID:* `{uid}`\n"
        f"*Username:* {username}\n\n"
        f"*Wallets Generated:* {generated}\n"
        f"*Wallet Limit:* {limit}\n"
        f"*Balance Checked:* {checked}\n"
    )
    await send_safe(query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb()
    ))

async def menu_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*⚙️ Settings*\n\n🚧 Coming soon\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_kb(),
    ))

async def menu_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*💰 Balance Checker*\n\n🚧 Coming soon\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_kb(),
    ))

# ============================================================================
# BULK WALLET GENERATOR FLOW
# ============================================================================
async def bulk_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    limit = get_global_limit()
    await send_safe(query.edit_message_text(
        "*🪪 Bulk Wallet Generator*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "How many wallets do you need?\n"
        f"_Enter a number between `1` and `{escape_md('{:,}'.format(limit))}`_",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))
    return ASK_COUNT

async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_private(update):
        return ASK_COUNT

    text  = update.message.text.strip()
    limit = get_global_limit()

    if not text.isdigit():
        await send_safe(update.message.reply_text(
            "⚠️ *Invalid input*\n\nPlease enter a number\\. Example: `1000`",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ASK_COUNT

    count = int(text)
    if count < 1 or count > limit:
        await send_safe(update.message.reply_text(
            f"⚠️ *Out of range*\n\n"
            f"Please enter a number between `1` and `{escape_md('{:,}'.format(limit))}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ASK_COUNT

    context.user_data["count"] = count
    await send_safe(update.message.reply_text(
        f"✅ *{escape_md('{:,}'.format(count))} wallets* selected\\.\n\n"
        "Choose your *mnemonic phrase length*\\:\n\n"
        "_Longer phrase \\= higher entropy \\= more secure_",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("12 words", callback_data="wc_12"),
                InlineKeyboardButton("15 words", callback_data="wc_15"),
                InlineKeyboardButton("18 words", callback_data="wc_18"),
            ],
            [
                InlineKeyboardButton("21 words", callback_data="wc_21"),
                InlineKeyboardButton("24 words  (most secure)", callback_data="wc_24"),
            ],
        ]),
    ))
    return ASK_WORDS

async def receive_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("wc_"):
        return ASK_WORDS

    word_count = int(query.data.split("_")[1])
    if word_count not in VALID_WORD_COUNTS:
        await send_safe(query.edit_message_text(
            "Invalid option\\. Use /start to begin again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ConversationHandler.END

    count = context.user_data.get("count")
    if not count:
        await send_safe(query.edit_message_text(
            "Session expired\\. Use /start to begin again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ConversationHandler.END

    context.user_data["word_count"] = word_count

    await send_safe(query.edit_message_text(
        f"✅ *{escape_md('{:,}'.format(count))} wallets* \\| *{word_count} words* selected\\.\n\n"
        "Choose your *export type*\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 CSV File",    callback_data="exp_csv"),
            InlineKeyboardButton("💬 TG Messages", callback_data="exp_tg"),
        ]]),
    ))
    return ASK_EXPORT

async def receive_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    export_type = query.data  # "exp_csv" or "exp_tg"
    count      = context.user_data.get("count")
    word_count = context.user_data.get("word_count")

    if not count or not word_count:
        await send_safe(query.edit_message_text(
            "Session expired\\. Use /start to begin again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ConversationHandler.END

    est = eta_string(count)
    export_label = "CSV file" if export_type == "exp_csv" else "TG messages"

    await send_safe(query.edit_message_text(
        f"⚙️ *Generating {escape_md('{:,}'.format(count))} wallets\\.\\.\\.*\n\n"
        f"\\- Mnemonic: `{word_count} words`\n"
        f"\\- Derivation: `{escape_md(DERIVATION_PATH)}`\n"
        f"\\- Export as: `{escape_md(export_label)}`\n"
        f"\\- Estimated time: `{escape_md(est)}`\n\n"
        "_Please wait — this happens automatically\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))

    # Run CPU-bound generation off the event loop
    try:
        rows, actual_secs = await asyncio.to_thread(_generate_wallets, count, word_count)
    except Exception as exc:
        logger.exception("Wallet generation failed: %s", exc)
        await send_safe(query.message.reply_text(
            "❌ *Generation failed*\n\nAn unexpected error occurred\\. "
            "Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        context.user_data.clear()
        return ConversationHandler.END

    elapsed = f"{actual_secs:.1f}s" if actual_secs < 60 else f"{actual_secs / 60:.1f} min"

    if export_type == "exp_csv":
        await _deliver_csv(query, rows, count, elapsed)
    else:
        await _deliver_tg(query, context, rows, count, elapsed)

    # Update user stats
    user = get_user(update.effective_user.id)
    user["wallets_generated"] += count
    context.user_data.clear()

    await send_safe(query.message.reply_text(
        "🏁 *All done\\!*\n\nNeed another batch? Use /start anytime\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_kb(),
    ))
    return ConversationHandler.END

async def _deliver_csv(query, rows: list, count: int, elapsed: str) -> None:
    csv_path  = await asyncio.to_thread(_write_csv, rows)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename  = f"wallets_{count}_{timestamp}.csv"
    try:
        with open(csv_path, "rb") as f:
            await send_safe(query.message.reply_document(
                document=f,
                filename=filename,
                caption=(
                    f"✅ *{escape_md('{:,}'.format(count))} wallets generated*\n\n"
                    f"📄 File: `{escape_md(filename)}`\n"
                    f"⏱ Time: `{escape_md(elapsed)}`\n"
                    f"🔑 Columns: `#`, `address`, `private_key`, `mnemonic`\n\n"
                    "⚠️ *Security reminder*\n"
                    "Store this file in an encrypted location\\.\n"
                    "Never share your private keys or mnemonic phrases\\."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            ))
    except Exception as exc:
        logger.exception("Failed to send CSV: %s", exc)
        await send_safe(query.message.reply_text(
            "❌ *Delivery failed*\n\nCould not send the file\\. Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass

async def _deliver_tg(query, context, rows: list, count: int, elapsed: str) -> None:
    total_msgs = math.ceil(len(rows) / TG_WALLETS_PER_MSG)
    await send_safe(query.message.reply_text(
        f"📨 Sending *{escape_md('{:,}'.format(count))} wallets* "
        f"in *{escape_md(str(total_msgs))} messages* "
        f"\\({TG_WALLETS_PER_MSG} per message\\)\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))

    for i in range(0, len(rows), TG_WALLETS_PER_MSG):
        chunk     = rows[i: i + TG_WALLETS_PER_MSG]
        part_num  = i // TG_WALLETS_PER_MSG + 1
        lines     = [f"*Part {part_num}/{total_msgs}*\n"]
        for row in chunk:
            idx, address, pk, mnemonic = row
            lines.append(
                f"*\\#{idx}*\n"
                f"Address: `{address}`\n"
                f"Private Key: `{pk}`\n"
                f"Mnemonic: `{escape_md(mnemonic)}`"
            )
        try:
            await send_safe(context.bot.send_message(
                chat_id=query.message.chat_id,
                text="\n\n".join(lines),
                parse_mode=ParseMode.MARKDOWN_V2,
            ))
        except Exception as exc:
            logger.exception("Failed to send TG chunk %s: %s", part_num, exc)
        await asyncio.sleep(TG_MSG_DELAY)

    await send_safe(query.message.reply_text(
        f"✅ *{escape_md('{:,}'.format(count))} wallets sent*\n"
        f"⏱ Time: `{escape_md(elapsed)}`\n\n"
        "⚠️ *Security reminder*\n"
        "Never share your private keys or mnemonic phrases\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))

# ============================================================================
# ADMIN HANDLERS
# ============================================================================
async def admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        WELCOME_ADMIN,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_menu_kb(),
    ))
    return ConversationHandler.END

# -- User Info: ask for ID or username, then show profile --------------------
async def adm_userinfo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*👤 User Info*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the user's *Telegram ID* or *@username* to look up their profile\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    return ADM_USERINFO_QUERY

async def adm_userinfo_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_USERINFO_QUERY

    q = update.message.text.strip()
    uid, user = find_user_by_query(q)

    if user is None:
        await send_safe(update.message.reply_text(
            f"⚠️ No user found for `{escape_md(q)}`\\.\n\n"
            "Make sure the user has started the bot first\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_USERINFO_QUERY

    name     = escape_md(user["name"] or "N/A")
    username = escape_md("@" + user["username"] if user["username"] else "N/A")
    generated = escape_md("{:,}".format(user["wallets_generated"]))
    checked   = escape_md("{:,}".format(user["balance_checked"]))
    limit     = escape_md("{:,}".format(get_global_limit()))

    text = (
        "*👤 User Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Name:* {name}\n"
        f"*User ID:* `{uid}`\n"
        f"*Username:* {username}\n\n"
        f"*Wallets Generated:* {generated}\n"
        f"*Wallet Limit:* {limit}\n"
        f"*Balance Checked:* {checked}\n"
    )
    await send_safe(update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    return ADM_USERINFO_QUERY  # stay in state so admin can look up another user

async def adm_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*💰 Balance*\n\n🚧 Coming soon\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))

async def adm_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*📊 Rate*\n\n🚧 Coming soon\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))

# -- Global Wallet Limit -----------------------------------------------------
async def adm_wlimit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    current = escape_md("{:,}".format(get_global_limit()))
    await send_safe(query.edit_message_text(
        "*🔢 Wallet Generate Limit*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current global limit: `{current}`\n\n"
        "This limit applies to *all users*\\.\n\n"
        "Send the *new limit* \\(1 to 100,000\\)\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    return ADM_SET_LIMIT_VAL

async def adm_wlimit_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_SET_LIMIT_VAL

    text = update.message.text.strip()
    if not text.isdigit():
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid number\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_SET_LIMIT_VAL

    new_limit = int(text)
    if new_limit < 1 or new_limit > 100_000:
        await send_safe(update.message.reply_text(
            "⚠️ Limit must be between `1` and `100,000`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_SET_LIMIT_VAL

    set_global_limit(new_limit)
    await send_safe(update.message.reply_text(
        f"✅ *Global wallet limit updated\\!*\n\n"
        f"New limit: `{escape_md('{:,}'.format(new_limit))}`\n"
        "This applies to all users immediately\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    context.user_data.clear()
    return ConversationHandler.END

# ============================================================================
# CANCEL / FALLBACK
# ============================================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if is_private(update):
        await send_safe(update.message.reply_text(
            "🚫 *Session cancelled\\.*\n\nUse /start whenever you're ready\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
    elif is_admin_group(update):
        await send_safe(update.message.reply_text(
            "🚫 Admin action cancelled\\. Use /start to open the admin panel\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await send_safe(update.message.reply_text(
            HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2
        ))

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await send_safe(update.message.reply_text(
            "Use /start to open the menu or /help for more info\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))

# ============================================================================
# MAIN
# ============================================================================
def main() -> None:
    global _wallets_per_sec

    logger.info("Running startup benchmark...")
    _wallets_per_sec = _run_benchmark()

    app = Application.builder().token(BOT_TOKEN).build()

    # User conversation: Bulk Wallet Generator (private chat)
    user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bulk_entry, pattern="^menu_bulk$")],
        states={
            ASK_COUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_count,
                )
            ],
            ASK_WORDS: [
                CallbackQueryHandler(receive_words, pattern=r"^wc_(12|15|18|21|24)$")
            ],
            ASK_EXPORT: [
                CallbackQueryHandler(receive_export, pattern="^exp_(csv|tg)$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(menu_home, pattern="^menu_home$"),
        ],
        allow_reentry=True,
    )

    # Admin conversation: User Info lookup
    admin_userinfo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_userinfo_prompt, pattern="^adm_userinfo$")],
        states={
            ADM_USERINFO_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_userinfo_lookup)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(admin_home, pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Admin conversation: Global Wallet Limit
    admin_wlimit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_wlimit_prompt, pattern="^adm_wlimit$")],
        states={
            ADM_SET_LIMIT_VAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_wlimit_set)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(admin_home, pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Commands
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("cancel", cancel))

    # Conversations
    app.add_handler(user_conv)
    app.add_handler(admin_userinfo_conv)
    app.add_handler(admin_wlimit_conv)

    # Static callbacks (private)
    app.add_handler(CallbackQueryHandler(menu_home,     pattern="^menu_home$"))
    app.add_handler(CallbackQueryHandler(menu_profile,  pattern="^menu_profile$"))
    app.add_handler(CallbackQueryHandler(menu_settings, pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(menu_balance,  pattern="^menu_balance$"))

    # Static callbacks (admin)
    app.add_handler(CallbackQueryHandler(admin_home,  pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(adm_balance, pattern="^adm_balance$"))
    app.add_handler(CallbackQueryHandler(adm_rate,    pattern="^adm_rate$"))

    # Unknown commands (private only)
    app.add_handler(
        MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown_command)
    )

    logger.info("Bot started. Speed: %.1f wallets/sec", _wallets_per_sec)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

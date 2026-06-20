"""
EVM Wallet Generator Bot
BIP-44 HD wallets for all EVM chains (Ethereum, BSC, Polygon, Arbitrum, etc.)

Features:
  - Private chat only (ignores all group messages except the admin group)
  - Admin group: User Info, Balance, Rate, Wallet Generate Limit controls
  - Main menu: Profile, Settings, Bulk Wallet Generator, Balance Checker
  - CSV export up to 100k wallets with accurate time estimate
  - Global 28 msg/sec rate limiter with async queue (multiple users work simultaneously)
  - Per-user wallet generation limit (default 100k, admin can change per user)
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
# CONFIG  (set these as Railway environment variables)
# ============================================================================
BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# Telegram group ID where the bot is admin (e.g. -1001234567890)
# Leave empty to disable admin group features
ADMIN_GROUP_ID: int = int(os.environ.get("ADMIN_GROUP_ID", "0"))

DERIVATION_PATH = "m/44'/60'/0'/0/0"
DEFAULT_WALLET_LIMIT = 100_000       # per-user default, admin can override
GLOBAL_MSG_RATE_LIMIT = 28          # max messages bot sends per second (Telegram allows 30)
BENCHMARK_SAMPLE = 30               # wallets generated during startup benchmark
STRENGTH_MAP = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
VALID_WORD_COUNTS = {12, 15, 18, 21, 24}

# ============================================================================
# CONVERSATION STATES
# ============================================================================
ASK_COUNT, ASK_WORDS = range(2)
ADMIN_SET_LIMIT_UID, ADMIN_SET_LIMIT_VAL = range(10, 12)

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
            "wallet_limit": DEFAULT_WALLET_LIMIT,
            "credits": 0,
            "balance_checked": 0,
        }
    elif tg_user:
        USER_DB[uid]["name"] = tg_user.full_name
        USER_DB[uid]["username"] = tg_user.username
    return USER_DB[uid]

# ============================================================================
# RATE LIMITER  (28 messages per second, bot-wide)
# Uses a sliding window deque. All outgoing sends go through send_safe().
# ============================================================================
_msg_timestamps: deque = deque()
_rate_lock = asyncio.Lock()

async def send_safe(coro):
    """
    Wraps any outgoing Telegram send coroutine and enforces the global
    28 msg/sec rate limit. If the limit is hit, waits until the next
    1-second window opens before sending.
    """
    while True:
        async with _rate_lock:
            now = time.monotonic()
            # Drop timestamps older than 1 second
            while _msg_timestamps and now - _msg_timestamps[0] >= 1.0:
                _msg_timestamps.popleft()

            if len(_msg_timestamps) < GLOBAL_MSG_RATE_LIMIT:
                _msg_timestamps.append(now)
                break  # slot acquired, send below
            # Window is full: sleep until the oldest timestamp expires
            sleep_for = 1.0 - (now - _msg_timestamps[0]) + 0.01
        await asyncio.sleep(sleep_for)
    return await coro

# ============================================================================
# BENCHMARK  (runs at startup so ETA is accurate on first request)
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
# WALLET GENERATION + CSV  (runs in asyncio thread pool, non-blocking)
# ============================================================================
def _generate_csv(count: int, word_count: int) -> tuple[str, float]:
    """Streams wallets directly to disk. Returns (csv_path, elapsed_secs)."""
    mnemo = Mnemonic("english")
    strength = STRENGTH_MAP[word_count]
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    )
    t0 = time.perf_counter()
    try:
        writer = csv.writer(tmp)
        writer.writerow(["#", "address", "private_key", "mnemonic"])
        for i in range(count):
            phrase = mnemo.generate(strength=strength)
            acct = Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
            writer.writerow([i + 1, acct.address, acct.key.hex(), phrase])
        tmp.flush()
    finally:
        tmp.close()
    return tmp.name, time.perf_counter() - t0

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
            InlineKeyboardButton("👤 Profile",   callback_data="menu_profile"),
            InlineKeyboardButton("⚙️ Settings",  callback_data="menu_settings"),
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
# WELCOME TEXTS
# ============================================================================
WELCOME_USER = (
    "*EVM Wallet Generator*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Generates BIP\\-44 HD wallets compatible with all EVM chains "
    "\\(Ethereum, BSC, Polygon, Arbitrum, etc\\.\\)\n\n"
    "🔒 *Privacy:* Nothing is stored on the server\\.\n"
    f"📦 *Limit:* Up to {DEFAULT_WALLET_LIMIT:,} wallets per batch\n\n"
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
    "4\\. Receive a CSV file\n\n"
    "*CSV columns*\n"
    "`#` \\| `address` \\| `private_key` \\| `mnemonic`\n\n"
    "*Security*\n"
    "\\- BIP\\-44 standard derivation path\n"
    "\\- Compatible with MetaMask, Trust Wallet, Rabby, Bitget, etc\\.\n"
    "\\- Store your CSV in an encrypted location\n"
    "\\- Never share your private keys or mnemonic phrases\n\n"
    f"*Batch limit:* {DEFAULT_WALLET_LIMIT:,} wallets per request"
)

# ============================================================================
# /start HANDLER
# ============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    # Admin group: show admin panel
    if is_admin_group(update):
        await send_safe(update.message.reply_text(
            WELCOME_ADMIN,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=admin_menu_kb(),
        ))
        return ConversationHandler.END

    # All other groups: silently ignore
    if not is_private(update):
        return ConversationHandler.END

    # Private chat: register user and show main menu
    get_user(update.effective_user.id, update.effective_user)
    await send_safe(update.message.reply_text(
        WELCOME_USER,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(),
    ))
    return ConversationHandler.END

# ============================================================================
# MAIN MENU CALLBACK HANDLERS (private chat)
# ============================================================================
async def menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        WELCOME_USER,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(),
    ))
    return ConversationHandler.END

async def menu_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    user = get_user(uid, update.effective_user)

    name     = escape_md(user["name"] or "N/A")
    username = escape_md(f"@{user['username']}" if user["username"] else "N/A")
    limit    = escape_md(f"{user['wallet_limit']:,}")

    text = (
        "*👤 Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Name:* {name}\n"
        f"*User ID:* `{uid}`\n"
        f"*Username:* {username}\n\n"
        f"*Wallets Generated:* {escape_md(f\"{user['wallets_generated']:,}\")}\n"
        f"*Wallet Limit:* {limit}\n"
        f"*Balance Checked:* {escape_md(f\"{user['balance_checked']:,}\")}\n"
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

    uid = update.effective_user.id
    user = get_user(uid)
    limit = user["wallet_limit"]

    await send_safe(query.edit_message_text(
        f"*🪪 Bulk Wallet Generator*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"How many wallets do you need?\n"
        f"_Enter a number between `1` and `{limit:,}`_",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))
    return ASK_COUNT

async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Ignore group messages inside the conversation
    if not is_private(update):
        return ASK_COUNT

    text = update.message.text.strip()
    uid  = update.effective_user.id
    user = get_user(uid)
    limit = user["wallet_limit"]

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
            f"Please enter a number between `1` and `{escape_md(str(limit))}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ASK_COUNT

    context.user_data["count"] = count

    keyboard = [
        [
            InlineKeyboardButton("12 words", callback_data="wc_12"),
            InlineKeyboardButton("15 words", callback_data="wc_15"),
            InlineKeyboardButton("18 words", callback_data="wc_18"),
        ],
        [
            InlineKeyboardButton("21 words", callback_data="wc_21"),
            InlineKeyboardButton("24 words  (most secure)", callback_data="wc_24"),
        ],
    ]
    await send_safe(update.message.reply_text(
        f"✅ *{count:,} wallets* selected\\.\n\n"
        "Choose your *mnemonic phrase length*\\:\n\n"
        "_Longer phrase \\= higher entropy \\= more secure_",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(keyboard),
    ))
    return ASK_WORDS

async def receive_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("wc_"):
        return ASK_WORDS

    word_count = int(query.data.split("_")[1])
    if word_count not in VALID_WORD_COUNTS:
        await send_safe(query.edit_message_text("Invalid option\\. Use /start to begin again\\.",
                                                parse_mode=ParseMode.MARKDOWN_V2))
        return ConversationHandler.END

    count = context.user_data.get("count")
    if not count:
        await send_safe(query.edit_message_text("Session expired\\. Use /start to begin again\\.",
                                                parse_mode=ParseMode.MARKDOWN_V2))
        return ConversationHandler.END

    est = eta_string(count)
    await send_safe(query.edit_message_text(
        f"⚙️ *Generating {escape_md(str(count))} wallets\\.\\.\\.*\n\n"
        f"\\- Mnemonic: `{word_count} words`\n"
        f"\\- Derivation: `{escape_md(DERIVATION_PATH)}`\n"
        f"\\- Estimated time: `{escape_md(est)}`\n\n"
        "_Please wait — CSV will be sent automatically\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))

    # Run CPU-bound generation off the event loop so other users are not blocked
    try:
        csv_path, actual_secs = await asyncio.to_thread(_generate_csv, count, word_count)
    except Exception as exc:
        logger.exception("Wallet generation failed: %s", exc)
        await send_safe(query.message.reply_text(
            "❌ *Generation failed*\n\nAn unexpected error occurred\\. "
            "Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        context.user_data.clear()
        return ConversationHandler.END

    # Format actual elapsed time
    elapsed = f"{actual_secs:.1f}s" if actual_secs < 60 else f"{actual_secs / 60:.1f} min"
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename  = f"wallets_{count}_{timestamp}.csv"

    try:
        with open(csv_path, "rb") as f:
            await send_safe(query.message.reply_document(
                document=f,
                filename=filename,
                caption=(
                    f"✅ *{escape_md(str(count))} wallets generated*\n\n"
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
            "❌ *Delivery failed*\n\nCould not send the file\\. "
            "Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass

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

# ============================================================================
# ADMIN GROUP HANDLERS
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

async def adm_userinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not USER_DB:
        await send_safe(query.edit_message_text(
            "*👤 User Info*\n\n_No users registered yet\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=admin_back_kb(),
        ))
        return

    lines = ["*👤 User Info*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for uid, u in USER_DB.items():
        name     = escape_md(u["name"])
        username = escape_md(f"@{u['username']}" if u["username"] else "N/A")
        lines.append(
            f"*ID:* `{uid}`\n"
            f"*Name:* {name}\n"
            f"*Username:* {username}\n"
            f"*Wallets Generated:* {escape_md(str(u['wallets_generated']))}\n"
            f"*Wallet Limit:* {escape_md(str(u['wallet_limit']))}\n"
            "\\-\\-\\-"
        )

    # Telegram message limit: split if too long
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3800] + "\n\n_\\.\\.\\. truncated\\. Too many users to display\\._"

    await send_safe(query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=admin_back_kb()
    ))

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

async def adm_wlimit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*🔢 Wallet Generate Limit*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Reply with the *User ID* of the user whose limit you want to change\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    return ADMIN_SET_LIMIT_UID

async def adm_wlimit_get_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Only accept messages in admin group
    if not is_admin_group(update):
        return ADMIN_SET_LIMIT_UID

    text = update.message.text.strip()
    if not text.isdigit():
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid numeric *User ID*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADMIN_SET_LIMIT_UID

    uid = int(text)
    if uid not in USER_DB:
        await send_safe(update.message.reply_text(
            f"⚠️ User `{uid}` not found\\. Make sure the user has started the bot first\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADMIN_SET_LIMIT_UID

    context.user_data["target_uid"] = uid
    user = USER_DB[uid]
    name = escape_md(user["name"])

    await send_safe(update.message.reply_text(
        f"User found: *{name}* \\(`{uid}`\\)\n"
        f"Current limit: `{escape_md(str(user['wallet_limit']))}`\n\n"
        "Now send the *new wallet limit* \\(1 to 100,000\\)\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))
    return ADMIN_SET_LIMIT_VAL

async def adm_wlimit_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADMIN_SET_LIMIT_VAL

    text = update.message.text.strip()
    if not text.isdigit():
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid number\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADMIN_SET_LIMIT_VAL

    new_limit = int(text)
    if new_limit < 1 or new_limit > 100_000:
        await send_safe(update.message.reply_text(
            "⚠️ Limit must be between `1` and `100,000`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADMIN_SET_LIMIT_VAL

    uid = context.user_data.get("target_uid")
    if not uid or uid not in USER_DB:
        await send_safe(update.message.reply_text(
            "Session expired\\. Please use the Wallet Generate Limit button again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        context.user_data.clear()
        return ConversationHandler.END

    USER_DB[uid]["wallet_limit"] = new_limit
    name = escape_md(USER_DB[uid]["name"])

    await send_safe(update.message.reply_text(
        f"✅ *Done\\!*\n\n"
        f"Wallet limit for *{name}* \\(`{uid}`\\) has been set to "
        f"`{escape_md(str(new_limit))}`\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
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

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await send_safe(update.message.reply_text(
            "Use /start to open the menu or /help for more info\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await send_safe(update.message.reply_text(
            HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2
        ))

# ============================================================================
# MAIN
# ============================================================================
def main() -> None:
    global _wallets_per_sec

    logger.info("Running startup benchmark...")
    _wallets_per_sec = _run_benchmark()

    app = Application.builder().token(BOT_TOKEN).build()

    # ---- User conversation: Bulk Wallet Generator flow ----
    user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bulk_entry, pattern="^menu_bulk$")],
        states={
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_count)],
            ASK_WORDS: [CallbackQueryHandler(receive_words, pattern=r"^wc_(12|15|18|21|24)$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(menu_home, pattern="^menu_home$"),
        ],
        allow_reentry=True,
    )

    # ---- Admin conversation: Wallet Generate Limit flow ----
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_wlimit_menu, pattern="^adm_wlimit$")],
        states={
            ADMIN_SET_LIMIT_UID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_wlimit_get_uid)
            ],
            ADMIN_SET_LIMIT_VAL: [
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
    app.add_handler(admin_conv)

    # Static menu callbacks (private chat)
    app.add_handler(CallbackQueryHandler(menu_home,    pattern="^menu_home$"))
    app.add_handler(CallbackQueryHandler(menu_profile, pattern="^menu_profile$"))
    app.add_handler(CallbackQueryHandler(menu_settings, pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(menu_balance,  pattern="^menu_balance$"))

    # Static admin callbacks
    app.add_handler(CallbackQueryHandler(admin_home,    pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(adm_userinfo,  pattern="^adm_userinfo$"))
    app.add_handler(CallbackQueryHandler(adm_balance,   pattern="^adm_balance$"))
    app.add_handler(CallbackQueryHandler(adm_rate,      pattern="^adm_rate$"))

    # Unknown commands (private only)
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown_command))

    logger.info("Bot started. Speed: %.1f wallets/sec", _wallets_per_sec)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

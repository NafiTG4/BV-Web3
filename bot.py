"""
EVM Wallet Generator Bot
BIP-44 HD wallets for all EVM chains (Ethereum, BSC, Polygon, Arbitrum, etc.)

Features:
  - Private chat only (ignores all groups except the admin group)
  - Admin: Balance panel (Add User Balance, New User Join Balance), Rate tree, Wallet Limit, User Info
  - Balance deducted per export type on wallet generate; blocked if insufficient
  - New users get global join balance on first /start
  - Export as CSV or TG Messages (both up to 100k, rate limited)
  - Global 28 msg/sec rate limiter with sliding window
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
# CONFIG
# ============================================================================
BOT_TOKEN: str      = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID: int = int(os.environ.get("ADMIN_GROUP_ID", "0"))

DERIVATION_PATH       = "m/44'/60'/0'/0/0"
GLOBAL_MSG_RATE_LIMIT = 28
BENCHMARK_SAMPLE      = 30
STRENGTH_MAP          = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
VALID_WORD_COUNTS     = {12, 15, 18, 21, 24}
TG_WALLETS_PER_MSG    = 10
TG_MSG_DELAY          = 0.05

# ============================================================================
# GLOBAL RUNTIME CONFIG  (admin-settable at runtime)
# ============================================================================
_global_wallet_limit: int  = 100_000_000
_rate_csv_per_1000: float  = 0.01    # PTS charged per 1000 wallets, CSV export
_rate_tg_per_1000: float   = 0.03    # PTS charged per 1000 wallets, TG export
_join_balance: float        = 0.0    # PTS given to every new user on first /start

def get_global_limit() -> int:        return _global_wallet_limit
def set_global_limit(v: int) -> None:
    global _global_wallet_limit;      _global_wallet_limit = v

def get_rate_csv() -> float:          return _rate_csv_per_1000
def set_rate_csv(v: float) -> None:
    global _rate_csv_per_1000;        _rate_csv_per_1000 = v

def get_rate_tg() -> float:           return _rate_tg_per_1000
def set_rate_tg(v: float) -> None:
    global _rate_tg_per_1000;         _rate_tg_per_1000 = v

def get_join_balance() -> float:      return _join_balance
def set_join_balance(v: float) -> None:
    global _join_balance;             _join_balance = v

def cost_for(count: int, export_type: str) -> float:
    """Calculate credit cost for a given wallet count and export type."""
    rate = get_rate_csv() if export_type == "exp_csv" else get_rate_tg()
    return (count / 1000) * rate

# ============================================================================
# CONVERSATION STATES
# ============================================================================
ASK_COUNT, ASK_WORDS, ASK_EXPORT = range(3)
ADM_USERINFO_QUERY               = 10
ADM_SET_LIMIT_VAL                = 11
ADM_SET_RATE_CSV_VAL             = 12
ADM_SET_RATE_TG_VAL              = 13
ADM_ADD_BAL_UID                  = 14
ADM_ADD_BAL_AMT                  = 15
ADM_SET_JOIN_BAL                 = 16

# ============================================================================
# IN-MEMORY USER STORE
# ============================================================================
USER_DB: dict[int, dict] = {}

def get_user(uid: int, tg_user=None) -> dict:
    is_new = uid not in USER_DB
    if is_new:
        USER_DB[uid] = {
            "name":              tg_user.full_name if tg_user else str(uid),
            "username":          tg_user.username  if tg_user else None,
            "credits":           get_join_balance(),   # grant join balance on first seen
            "wallets_generated": 0,
            "balance_checked":   0,
        }
    elif tg_user:
        USER_DB[uid]["name"]     = tg_user.full_name
        USER_DB[uid]["username"] = tg_user.username
    return USER_DB[uid]

def find_user_by_query(q: str) -> tuple:
    """Find user by numeric Telegram ID or @username. Returns (uid, user) or (None, None)."""
    q = q.strip().lstrip("@")
    if q.isdigit():
        uid = int(q)
        return (uid, USER_DB[uid]) if uid in USER_DB else (None, None)
    q_lower = q.lower()
    for uid, u in USER_DB.items():
        if u["username"] and u["username"].lower() == q_lower:
            return uid, u
    return None, None

# ============================================================================
# RATE LIMITER  (28 msg/sec, bot-wide sliding window)
# ============================================================================
_msg_timestamps: deque = deque()
_rate_lock = asyncio.Lock()

async def send_safe(coro):
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
    t0    = time.perf_counter()
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
    return f"~{math.ceil(secs)}s" if secs < 60 else f"~{secs / 60:.1f} min"

# ============================================================================
# WALLET GENERATION
# ============================================================================
def _generate_wallets(count: int, word_count: int) -> tuple:
    mnemo    = Mnemonic("english")
    strength = STRENGTH_MAP[word_count]
    rows     = []
    t0       = time.perf_counter()
    for i in range(count):
        phrase = mnemo.generate(strength=strength)
        acct   = Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
        rows.append([i + 1, acct.address, acct.key.hex(), phrase])
    return rows, time.perf_counter() - t0

def _write_csv(rows: list) -> str:
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

def fmt(n) -> str:
    return "{:,}".format(n)

def fmt_credits(v: float) -> str:
    return "{:.4f}".format(v)

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

def adm_balance_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add User Balance",      callback_data="adm_bal_add")],
        [InlineKeyboardButton("🎁 New User Join Balance", callback_data="adm_bal_join")],
        [InlineKeyboardButton("⬅️ Back",                  callback_data="adm_home")],
    ])

def adm_rate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Wallet Generate Rate", callback_data="adm_rate_wallet")],
        [InlineKeyboardButton("⬅️ Back",                 callback_data="adm_home")],
    ])

def adm_rate_wallet_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Per 1000 CSV Rate",        callback_data="adm_rate_csv")],
        [InlineKeyboardButton("💬 Per 1000 TG Message Rate", callback_data="adm_rate_tg")],
        [InlineKeyboardButton("⬅️ Back",                     callback_data="adm_rate")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")
    ]])

def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="adm_home")
    ]])

def adm_balance_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Back", callback_data="adm_balance")
    ]])

# ============================================================================
# DYNAMIC TEXTS
# ============================================================================
def welcome_user_text() -> str:
    return (
        "*EVM Wallet Generator*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Generates BIP\\-44 HD wallets compatible with all EVM chains "
        "\\(Ethereum, BSC, Polygon, Arbitrum, etc\\.\\)\n\n"
        "🔒 *Privacy:* Nothing is stored on the server\\.\n"
        f"📦 *Limit:* Up to {escape_md(fmt(get_global_limit()))} wallets per batch\n\n"
        "Choose an option below\\:"
    )

WELCOME_ADMIN = (
    "*EVM Wallet Generator*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "🛡️ *Admin Panel*\n\n"
    "Use the buttons below to manage users and bot settings\\."
)

HELP_TEXT = (
    "*EVM Wallet Generator \\- Help*\n"
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

    # get_user() grants join balance on first call for this uid
    get_user(update.effective_user.id, update.effective_user)
    await send_safe(update.message.reply_text(
        welcome_user_text(),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_menu_kb(),
    ))
    return ConversationHandler.END

# ============================================================================
# MAIN MENU CALLBACKS (private chat)
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

    name      = escape_md(user["name"] or "N/A")
    username  = escape_md("@" + user["username"] if user["username"] else "N/A")
    credits   = escape_md(fmt_credits(user["credits"]))
    generated = escape_md(fmt(user["wallets_generated"]))
    checked   = escape_md(fmt(user["balance_checked"]))
    rate_tg   = escape_md(fmt_credits(get_rate_tg()))
    rate_csv  = escape_md(fmt_credits(get_rate_csv()))

    text = (
        "*👤 Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Name:* {name}\n"
        f"*User ID:* `{uid}`\n"
        f"*Username:* {username}\n\n"
        f"*Remaining PTS:* `{credits}`\n\n"
        f"*Wallets Generated:* `{generated}`\n"
        f"*Balance Checked:* `{checked}`\n\n"
        f"*Rate Per 1,000 Generate \\(TG\\):* `{rate_tg}` PTS\n"
        f"*Rate Per 1,000 Generate \\(CSV\\):* `{rate_csv}` PTS\n"
        f"*Rate Per 1,000 CSV Balance Check:* `coming soon`\n"
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
        f"_Enter a number between `1` and `{escape_md(fmt(limit))}`_",
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
            f"Please enter a number between `1` and `{escape_md(fmt(limit))}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ASK_COUNT

    context.user_data["count"] = count
    await send_safe(update.message.reply_text(
        f"✅ *{escape_md(fmt(count))} wallets* selected\\.\n\n"
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

    # Show cost preview on export selection screen
    cost_csv = escape_md(fmt_credits(cost_for(count, "exp_csv")))
    cost_tg  = escape_md(fmt_credits(cost_for(count, "exp_tg")))

    await send_safe(query.edit_message_text(
        f"✅ *{escape_md(fmt(count))} wallets* \\| *{word_count} words* selected\\.\n\n"
        "Choose your *export type*\\:\n\n"
        f"📄 CSV \\- costs `{cost_csv}` PTS\n"
        f"💬 TG Messages \\- costs `{cost_tg}` PTS",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 CSV File",    callback_data="exp_csv"),
            InlineKeyboardButton("💬 TG Messages", callback_data="exp_tg"),
        ]]),
    ))
    return ASK_EXPORT

async def receive_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query       = update.callback_query
    await query.answer()

    export_type = query.data
    count       = context.user_data.get("count")
    word_count  = context.user_data.get("word_count")

    if not count or not word_count:
        await send_safe(query.edit_message_text(
            "Session expired\\. Use /start to begin again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ConversationHandler.END

    uid  = update.effective_user.id
    user = get_user(uid)

    # ---- Balance check ----
    required = cost_for(count, export_type)
    if user["credits"] < required:
        shortage     = required - user["credits"]
        export_label = "CSV" if export_type == "exp_csv" else "TG Messages"
        await send_safe(query.edit_message_text(
            f"❌ *Insufficient Credits*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"*Export type:* `{escape_md(export_label)}`\n"
            f"*Wallets:* `{escape_md(fmt(count))}`\n"
            f"*Cost:* `{escape_md(fmt_credits(required))}` PTS\n"
            f"*Your balance:* `{escape_md(fmt_credits(user['credits']))}` PTS\n"
            f"*Shortfall:* `{escape_md(fmt_credits(shortage))}` PTS\n\n"
            "Please contact an admin to top up your balance\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(),
        ))
        context.user_data.clear()
        return ConversationHandler.END

    est          = eta_string(count)
    export_label = "CSV file" if export_type == "exp_csv" else "TG messages"

    await send_safe(query.edit_message_text(
        f"⚙️ *Generating {escape_md(fmt(count))} wallets\\.\\.\\.*\n\n"
        f"\\- Mnemonic: `{word_count} words`\n"
        f"\\- Derivation: `{escape_md(DERIVATION_PATH)}`\n"
        f"\\- Export as: `{escape_md(export_label)}`\n"
        f"\\- Estimated time: `{escape_md(est)}`\n"
        f"\\- Cost: `{escape_md(fmt_credits(required))}` PTS\n\n"
        "_Please wait \\- this happens automatically\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))

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

    # Deduct balance AFTER successful generation, before delivery
    user["credits"]           -= required
    user["wallets_generated"] += count

    elapsed = f"{actual_secs:.1f}s" if actual_secs < 60 else f"{actual_secs / 60:.1f} min"

    if export_type == "exp_csv":
        await _deliver_csv(query, rows, count, elapsed)
    else:
        await _deliver_tg(query, context, rows, count, elapsed)

    context.user_data.clear()

    remaining = escape_md(fmt_credits(user["credits"]))
    await send_safe(query.message.reply_text(
        "🏁 *All done\\!*\n\n"
        f"💳 *Remaining PTS:* `{remaining}`\n\n"
        "Need another batch? Use /start anytime\\.",
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
                    f"✅ *{escape_md(fmt(count))} wallets generated*\n\n"
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
        f"📨 Sending *{escape_md(fmt(count))} wallets* "
        f"in *{escape_md(str(total_msgs))} messages* "
        f"\\({TG_WALLETS_PER_MSG} per message\\)\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))
    for i in range(0, len(rows), TG_WALLETS_PER_MSG):
        chunk    = rows[i: i + TG_WALLETS_PER_MSG]
        part_num = i // TG_WALLETS_PER_MSG + 1
        lines    = [f"*Part {part_num}/{total_msgs}*\n"]
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
        f"✅ *{escape_md(fmt(count))} wallets sent*\n"
        f"⏱ Time: `{escape_md(elapsed)}`\n\n"
        "⚠️ *Security reminder*\n"
        "Never share your private keys or mnemonic phrases\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))

# ============================================================================
# ADMIN: BALANCE PANEL
# ============================================================================
async def adm_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    join_bal = escape_md(fmt_credits(get_join_balance()))
    await send_safe(query.edit_message_text(
        "*💰 Balance*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Current join balance:* `{join_bal}` PTS\n\n"
        "Select an action\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=adm_balance_kb(),
    ))
    return ConversationHandler.END

# -- Add User Balance --------------------------------------------------------
async def adm_bal_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*➕ Add User Balance*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the *Telegram ID* or *@username* of the user you want to top up\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=adm_balance_back_kb(),
    ))
    return ADM_ADD_BAL_UID

async def adm_bal_add_uid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_ADD_BAL_UID

    q        = update.message.text.strip()
    uid, user = find_user_by_query(q)

    if user is None:
        await send_safe(update.message.reply_text(
            f"⚠️ No user found for `{escape_md(q)}`\\.\n\n"
            "Make sure the user has started the bot first\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_ADD_BAL_UID

    context.user_data["bal_target_uid"] = uid
    name    = escape_md(user["name"])
    current = escape_md(fmt_credits(user["credits"]))

    await send_safe(update.message.reply_text(
        f"User found: *{name}* \\(`{uid}`\\)\n"
        f"Current balance: `{current}` PTS\n\n"
        "Send the *amount to add* \\(e\\.g\\. `5` or `0\\.5`\\)\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
    ))
    return ADM_ADD_BAL_AMT

async def adm_bal_add_amt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_ADD_BAL_AMT

    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid positive number \\(e\\.g\\. `5` or `0\\.5`\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_ADD_BAL_AMT

    uid = context.user_data.get("bal_target_uid")
    if uid is None or uid not in USER_DB:
        await send_safe(update.message.reply_text(
            "Session expired\\. Please use Add User Balance again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        context.user_data.clear()
        return ConversationHandler.END

    USER_DB[uid]["credits"] += amount
    new_bal = USER_DB[uid]["credits"]
    name    = escape_md(USER_DB[uid]["name"])

    await send_safe(update.message.reply_text(
        f"✅ *Balance updated\\!*\n\n"
        f"User: *{name}* \\(`{uid}`\\)\n"
        f"Added: `{escape_md(fmt_credits(amount))}` PTS\n"
        f"New balance: `{escape_md(fmt_credits(new_bal))}` PTS",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add to Another User", callback_data="adm_bal_add")],
            [InlineKeyboardButton("⬅️ Back",               callback_data="adm_balance")],
        ]),
    ))
    context.user_data.clear()
    return ConversationHandler.END

# -- New User Join Balance ---------------------------------------------------
async def adm_bal_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    current = escape_md(fmt_credits(get_join_balance()))
    await send_safe(query.edit_message_text(
        "*🎁 New User Join Balance*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current join balance: `{current}` PTS\n\n"
        "Every new user gets this amount when they first start the bot\\.\n\n"
        "Send the *new join balance* \\(e\\.g\\. `1` or `0`\\)\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=adm_balance_back_kb(),
    ))
    return ADM_SET_JOIN_BAL

async def adm_bal_join_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_SET_JOIN_BAL

    try:
        val = float(update.message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid non-negative number \\(e\\.g\\. `1` or `0`\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_SET_JOIN_BAL

    set_join_balance(val)
    await send_safe(update.message.reply_text(
        f"✅ *Join balance updated\\!*\n\n"
        f"New users will receive `{escape_md(fmt_credits(val))}` PTS on first /start\\.\n"
        "_Existing users are not affected\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Balance", callback_data="adm_balance")
        ]]),
    ))
    context.user_data.clear()
    return ConversationHandler.END

# ============================================================================
# ADMIN: RATE TREE
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

async def adm_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*📊 Rate*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Select a category to configure rates\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=adm_rate_kb(),
    ))

async def adm_rate_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    csv_rate = escape_md(fmt_credits(get_rate_csv()))
    tg_rate  = escape_md(fmt_credits(get_rate_tg()))
    await send_safe(query.edit_message_text(
        "*💸 Wallet Generate Rate*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Per 1,000 CSV Export:* `{csv_rate}` PTS\n"
        f"*Per 1,000 TG Message Export:* `{tg_rate}` PTS\n\n"
        "Select a rate to update\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=adm_rate_wallet_kb(),
    ))

async def adm_rate_csv_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    current = escape_md(fmt_credits(get_rate_csv()))
    await send_safe(query.edit_message_text(
        "*📄 Per 1,000 CSV Rate*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current rate: `{current}` PTS per 1,000 wallets\n\n"
        "Send the *new rate* \\(e\\.g\\. `0\\.01`\\)\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data="adm_rate_wallet")
        ]]),
    ))
    return ADM_SET_RATE_CSV_VAL

async def adm_rate_csv_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_SET_RATE_CSV_VAL
    try:
        val = float(update.message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid positive number \\(e\\.g\\. `0\\.01`\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_SET_RATE_CSV_VAL

    set_rate_csv(val)
    await send_safe(update.message.reply_text(
        f"✅ *CSV rate updated\\!*\n\n"
        f"New rate: `{escape_md(fmt_credits(val))}` PTS per 1,000 wallets\n"
        "Applied globally immediately\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Rate Menu", callback_data="adm_rate")
        ]]),
    ))
    context.user_data.clear()
    return ConversationHandler.END

async def adm_rate_tg_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    current = escape_md(fmt_credits(get_rate_tg()))
    await send_safe(query.edit_message_text(
        "*💬 Per 1,000 TG Message Rate*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current rate: `{current}` PTS per 1,000 wallets\n\n"
        "Send the *new rate* \\(e\\.g\\. `0\\.03`\\)\\:",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data="adm_rate_wallet")
        ]]),
    ))
    return ADM_SET_RATE_TG_VAL

async def adm_rate_tg_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_SET_RATE_TG_VAL
    try:
        val = float(update.message.text.strip())
        if val < 0:
            raise ValueError
    except ValueError:
        await send_safe(update.message.reply_text(
            "⚠️ Please send a valid positive number \\(e\\.g\\. `0\\.03`\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_SET_RATE_TG_VAL

    set_rate_tg(val)
    await send_safe(update.message.reply_text(
        f"✅ *TG Message rate updated\\!*\n\n"
        f"New rate: `{escape_md(fmt_credits(val))}` PTS per 1,000 wallets\n"
        "Applied globally immediately\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back to Rate Menu", callback_data="adm_rate")
        ]]),
    ))
    context.user_data.clear()
    return ConversationHandler.END

# ============================================================================
# ADMIN: USER INFO
# ============================================================================
async def adm_userinfo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await send_safe(query.edit_message_text(
        "*👤 User Info*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send the user's *Telegram ID* or *@username* to look up their profile\\.\n\n"
        "_You can look up multiple users in a row\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    return ADM_USERINFO_QUERY

async def adm_userinfo_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_group(update):
        return ADM_USERINFO_QUERY

    q         = update.message.text.strip()
    uid, user = find_user_by_query(q)

    if user is None:
        await send_safe(update.message.reply_text(
            f"⚠️ No user found for `{escape_md(q)}`\\.\n\n"
            "Make sure the user has started the bot first\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_USERINFO_QUERY

    name      = escape_md(user["name"] or "N/A")
    username  = escape_md("@" + user["username"] if user["username"] else "N/A")
    credits   = escape_md(fmt_credits(user["credits"]))
    generated = escape_md(fmt(user["wallets_generated"]))
    checked   = escape_md(fmt(user["balance_checked"]))
    rate_tg   = escape_md(fmt_credits(get_rate_tg()))
    rate_csv  = escape_md(fmt_credits(get_rate_csv()))
    limit     = escape_md(fmt(get_global_limit()))

    text = (
        "*👤 User Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Name:* {name}\n"
        f"*User ID:* `{uid}`\n"
        f"*Username:* {username}\n\n"
        f"*Remaining PTS:* `{credits}`\n\n"
        f"*Wallets Generated:* `{generated}`\n"
        f"*Wallet Limit:* `{limit}`\n"
        f"*Balance Checked:* `{checked}`\n\n"
        f"*Rate Per 1,000 Generate \\(TG\\):* `{rate_tg}` PTS\n"
        f"*Rate Per 1,000 Generate \\(CSV\\):* `{rate_csv}` PTS\n"
        f"*Rate Per 1,000 CSV Balance Check:* `coming soon`\n"
    )
    await send_safe(update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=admin_back_kb(),
    ))
    return ADM_USERINFO_QUERY

# ============================================================================
# ADMIN: WALLET LIMIT
# ============================================================================
async def adm_wlimit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    current = escape_md(fmt(get_global_limit()))
    await send_safe(query.edit_message_text(
        "*🔢 Wallet Generate Limit*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current global limit: `{current}` wallets\n\n"
        "This applies to *all users* immediately\\.\n\n"
        "Send the *new limit* \\(1 to 100,000,000\\)\\:",
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
    if new_limit < 1 or new_limit > 100_000_000:
        await send_safe(update.message.reply_text(
            "⚠️ Limit must be between `1` and `100,000,000`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        ))
        return ADM_SET_LIMIT_VAL

    set_global_limit(new_limit)
    await send_safe(update.message.reply_text(
        f"✅ *Global wallet limit updated\\!*\n\n"
        f"New limit: `{escape_md(fmt(new_limit))}` wallets\n"
        "Applied to all users immediately\\.\n\n"
        "_You can update this limit again anytime\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Update Again", callback_data="adm_wlimit"),
            InlineKeyboardButton("⬅️ Back",         callback_data="adm_home"),
        ]]),
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

    # User: Bulk Wallet Generator
    user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bulk_entry, pattern="^menu_bulk$")],
        states={
            ASK_COUNT:  [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, receive_count
            )],
            ASK_WORDS:  [CallbackQueryHandler(receive_words,  pattern=r"^wc_(12|15|18|21|24)$")],
            ASK_EXPORT: [CallbackQueryHandler(receive_export, pattern="^exp_(csv|tg)$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(menu_home, pattern="^menu_home$"),
        ],
        allow_reentry=True,
    )

    # Admin: User Info lookup
    admin_userinfo_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_userinfo_prompt, pattern="^adm_userinfo$")],
        states={
            ADM_USERINFO_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_userinfo_lookup)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(admin_home, pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Admin: Global Wallet Limit
    admin_wlimit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_wlimit_prompt, pattern="^adm_wlimit$")],
        states={
            ADM_SET_LIMIT_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_wlimit_set)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(admin_home, pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Admin: CSV Rate
    admin_rate_csv_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_rate_csv_prompt, pattern="^adm_rate_csv$")],
        states={
            ADM_SET_RATE_CSV_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_rate_csv_set)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(adm_rate_wallet, pattern="^adm_rate_wallet$"),
            CallbackQueryHandler(adm_rate,        pattern="^adm_rate$"),
            CallbackQueryHandler(admin_home,       pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Admin: TG Rate
    admin_rate_tg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_rate_tg_prompt, pattern="^adm_rate_tg$")],
        states={
            ADM_SET_RATE_TG_VAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_rate_tg_set)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(adm_rate_wallet, pattern="^adm_rate_wallet$"),
            CallbackQueryHandler(adm_rate,        pattern="^adm_rate$"),
            CallbackQueryHandler(admin_home,       pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Admin: Add User Balance
    admin_add_bal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_bal_add_prompt, pattern="^adm_bal_add$")],
        states={
            ADM_ADD_BAL_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bal_add_uid)],
            ADM_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bal_add_amt)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(adm_balance, pattern="^adm_balance$"),
            CallbackQueryHandler(admin_home,  pattern="^adm_home$"),
        ],
        allow_reentry=True,
    )

    # Admin: New User Join Balance
    admin_join_bal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_bal_join_prompt, pattern="^adm_bal_join$")],
        states={
            ADM_SET_JOIN_BAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_bal_join_set)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(adm_balance, pattern="^adm_balance$"),
            CallbackQueryHandler(admin_home,  pattern="^adm_home$"),
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
    app.add_handler(admin_rate_csv_conv)
    app.add_handler(admin_rate_tg_conv)
    app.add_handler(admin_add_bal_conv)
    app.add_handler(admin_join_bal_conv)

    # Static callbacks (private)
    app.add_handler(CallbackQueryHandler(menu_home,     pattern="^menu_home$"))
    app.add_handler(CallbackQueryHandler(menu_profile,  pattern="^menu_profile$"))
    app.add_handler(CallbackQueryHandler(menu_settings, pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(menu_balance,  pattern="^menu_balance$"))

    # Static callbacks (admin)
    app.add_handler(CallbackQueryHandler(admin_home,       pattern="^adm_home$"))
    app.add_handler(CallbackQueryHandler(adm_balance,      pattern="^adm_balance$"))
    app.add_handler(CallbackQueryHandler(adm_rate,         pattern="^adm_rate$"))
    app.add_handler(CallbackQueryHandler(adm_rate_wallet,  pattern="^adm_rate_wallet$"))

    # Unknown commands (private only)
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown_command))

    logger.info("Bot started. Speed: %.1f wallets/sec", _wallets_per_sec)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

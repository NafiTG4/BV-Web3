"""
EVM Wallet Generator Bot - BIP-44 HD Wallet Generator
Compatible with MetaMask, Rabby, Bitget Wallet, and all EVM chains.
Optimized for Railway Trial Plan (2 vCPU, 0.5 GB RAM).

Features:
  - Main menu: Profile / Settings / Bulk Wallet Generator / Balance Checker
  - Bulk Wallet Generator: count -> mnemonic length -> export type (CSV / TG Message)
  - Profile: shows user stats and rate card (no private key shown here)
"""

import logging
import asyncio
import os
import csv
import tempfile
import math
from datetime import datetime

from concurrent.futures import ProcessPoolExecutor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from eth_account import Account
from mnemonic import Mnemonic

# ========================================================================
#  CONFIG / GLOBAL SETTINGS
# ========================================================================
Account.enable_unaudited_hdwallet_features()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

GENERATION_SPEED = int(os.environ.get("GEN_SPEED", 800))  # wallets/sec (approx, mnemonic lib)

# Conversation states
ASK_COUNT, ASK_WORDS, ASK_EXPORT = range(3)

MAX_WALLETS = 100_000
VALID_WORD_COUNTS = {12, 15, 18, 21, 24}
STRENGTH_MAP = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
DERIVATION_PATH = "m/44'/60'/0'/0/0"  # standard Ethereum path

MAX_WORKERS = 2          # Railway trial = 2 vCPU
BATCH_SIZE = 5000        # wallets per multiprocessing chunk (keeps RAM low)

WALLETS_PER_TG_MESSAGE = 15
# Sending one-by-one TG messages does not scale to 100k wallets (flood limits,
# message count). Above this, CSV is forced. Raise later if you add a queue.
MAX_WALLETS_FOR_TG_EXPORT = 3000

DEFAULT_CREDITS = 1000  # starting credit balance for new users (placeholder)

# Rate card (placeholders, edit freely, not yet deducted automatically)
RATE_PER_100K_TG = "Free (testing)"
RATE_PER_100K_CSV = "Free (testing)"
RATE_PER_100K_BALANCE_CSV = "Free (testing)"

# ========================================================================
#  IN-MEMORY USER STORE
#  NOTE: resets on every restart/redeploy. Swap this for a real DB
#  (SQLite/Postgres/Redis) when you get to the credits/payments part.
# ========================================================================
USER_DB: dict[int, dict] = {}


def get_user(update: Update) -> dict:
    tg_user = update.effective_user
    uid = tg_user.id
    if uid not in USER_DB:
        USER_DB[uid] = {
            "name": tg_user.full_name,
            "username": tg_user.username,
            "credits": DEFAULT_CREDITS,
            "wallets_generated": 0,
            "balance_checked": 0,
        }
    else:
        # keep name/username fresh in case the user changed them
        USER_DB[uid]["name"] = tg_user.full_name
        USER_DB[uid]["username"] = tg_user.username
    return USER_DB[uid]


def escape_md(text) -> str:
    """Escape MarkdownV2 special characters for dynamic text."""
    specials = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in specials else c for c in str(text))


# ========================================================================
#  TEXT BLOCKS
# ========================================================================
WELCOME_TEXT = (
    "*EVM Wallet Generator*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Generates BIP\\-44 HD wallets compatible with all EVM chains "
    "\\(Ethereum, BSC, Polygon, Arbitrum, etc\\.\\)\n\n"
    "*Privacy:* Nothing is stored on the server\\. "
    "Your data is deleted immediately after delivery\\.\n\n"
    "Choose an option below\\:"
)

HELP_TEXT = (
    "*EVM Wallet Generator — Help*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "*Commands*\n"
    "/start \\- Open the main menu\n"
    "/help \\- Show this help message\n"
    "/cancel \\- Cancel the current session\n\n"
    "*Bulk Wallet Generator flow*\n"
    "1\\. Send the number of wallets you need\n"
    "2\\. Choose your mnemonic word count\n"
    "3\\. Choose export type \\(CSV or TG Message\\)\n"
    "4\\. Receive your wallets\n\n"
    "*Security*\n"
    "• Wallets use BIP\\-44 standard derivation\n"
    "• Compatible with MetaMask, Trust Wallet, Rabby, Bitget, etc\\.\n"
    "• Store your wallet data in an encrypted location\n"
    "• Never share your private keys or mnemonic phrases\n\n"
    f"*Batch limit:* {MAX_WALLETS:,} wallets per request"
)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("👤 Profile", callback_data="menu_profile"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings"),
        ],
        [
            InlineKeyboardButton("🪪 Bulk Wallet Generator", callback_data="menu_bulk"),
        ],
        [
            InlineKeyboardButton("💰 Balance Checker", callback_data="menu_balance"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")]]
    )


# ========================================================================
#  WALLET GENERATION (worker process function)
# ========================================================================
def generate_batch(start_idx: int, end_idx: int, word_count: int) -> list:
    """Generates one range of wallets. Each process uses its own Mnemonic instance."""
    mnemo = Mnemonic("english")
    strength = STRENGTH_MAP[word_count]
    results = []
    for i in range(start_idx, end_idx):
        phrase = mnemo.generate(strength=strength)
        acct = Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
        results.append([i + 1, acct.address, acct.key.hex(), phrase])
    return results


def generate_wallets(count: int, word_count: int) -> list:
    """Generates `count` wallets across multiple processes, returns rows in order."""
    batches = []
    for start in range(0, count, BATCH_SIZE):
        end = min(start + BATCH_SIZE, count)
        batches.append((start, end, word_count))

    rows: list = []
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(generate_batch, s, e, wc) for s, e, wc in batches]
        for future in futures:
            rows.extend(future.result())
    return rows


def write_csv(rows: list) -> str:
    """Writes wallet rows to a temp CSV file and returns its path."""
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


def format_tg_chunk(rows_chunk: list) -> str:
    """Formats up to WALLETS_PER_TG_MESSAGE wallets as one MarkdownV2 message."""
    lines = []
    for row in rows_chunk:
        idx, address, pk, mnemonic = row
        lines.append(
            f"*\\#{idx}*\n"
            f"Address: `{address}`\n"
            f"Private Key: `{pk}`\n"
            f"Mnemonic: `{mnemonic}`"
        )
    return "\n\n".join(lines)


# ========================================================================
#  MAIN MENU HANDLERS
# ========================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    get_user(update)  # registers the user if new
    await update.message.reply_text(
        WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)


async def show_menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = get_user(update)

    name = escape_md(user["name"] or "N/A")
    username = escape_md(f"@{user['username']}" if user["username"] else "N/A")

    text = (
        "*👤 Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*Name:* {name}\n"
        f"*User ID:* `{update.effective_user.id}`\n"
        f"*Username:* {username}\n"
        f"*Remaining Credit:* {user['credits']:,} points\n"
        f"*Wallet Generated:* {user['wallets_generated']:,}\n"
        f"*Balance Checked:* {user['balance_checked']:,}\n\n"
        "*Rate Card*\n"
        f"Rate Per 100k Generate TG: {escape_md(RATE_PER_100K_TG)}\n"
        f"Rate Per 100k Generate CSV: {escape_md(RATE_PER_100K_CSV)}\n"
        f"Rate Per 100k CSV Balance Check: {escape_md(RATE_PER_100K_BALANCE_CSV)}"
    )
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_to_menu_keyboard()
    )


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*⚙️ Settings*\n\n_Coming soon\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_keyboard(),
    )


async def show_balance_checker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "*💰 Balance Checker*\n\n_Coming soon\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_keyboard(),
    )


# ========================================================================
#  BULK WALLET GENERATOR FLOW
# ========================================================================
async def bulk_wallet_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "*🪪 Bulk Wallet Generator*\n\n"
        f"How many wallets do you need\\? \\(1 \\- {MAX_WALLETS:,}\\)",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ASK_COUNT


async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ *Invalid input*\n\nPlease enter a number\\. Example: `1000`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ASK_COUNT

    count = int(text)
    if count < 1 or count > MAX_WALLETS:
        await update.message.reply_text(
            f"⚠️ *Out of range*\n\nPlease enter a number between `1` and `{MAX_WALLETS:,}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ASK_COUNT

    context.user_data["count"] = count

    keyboard = [
        [
            InlineKeyboardButton("12 words", callback_data="12"),
            InlineKeyboardButton("15 words", callback_data="15"),
            InlineKeyboardButton("18 words", callback_data="18"),
        ],
        [
            InlineKeyboardButton("21 words", callback_data="21"),
            InlineKeyboardButton("24 words (most secure)", callback_data="24"),
        ],
    ]

    await update.message.reply_text(
        f"✅ *{count:,} wallets* selected\\.\n\n"
        "Choose your *mnemonic phrase length*\\:\n\n"
        "_Longer phrase \\= higher entropy \\= more secure_",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_WORDS


async def receive_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    word_count = int(query.data)
    if word_count not in VALID_WORD_COUNTS:
        await query.edit_message_text("❌ Invalid option. Use /start to begin again.")
        return ConversationHandler.END

    count = context.user_data.get("count")
    if not count:
        await query.edit_message_text("⏳ Session expired. Use /start to begin again.")
        return ConversationHandler.END

    context.user_data["word_count"] = word_count

    export_buttons = [
        InlineKeyboardButton("📄 CSV", callback_data="export_csv"),
    ]
    if count <= MAX_WALLETS_FOR_TG_EXPORT:
        export_buttons.append(
            InlineKeyboardButton("💬 TG Message", callback_data="export_tg")
        )

    note = ""
    if count > MAX_WALLETS_FOR_TG_EXPORT:
        note = (
            f"\n\n_Note: TG Message export is disabled above "
            f"{MAX_WALLETS_FOR_TG_EXPORT:,} wallets \\(Telegram rate limits\\)\\. "
            "Please use CSV for large batches\\._"
        )

    await query.edit_message_text(
        f"✅ *{count:,} wallets* · *{word_count} words* selected\\.\n\n"
        "Choose your *export type*\\:" + note,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([export_buttons]),
    )
    return ASK_EXPORT


async def receive_export_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    export_type = query.data  # "export_csv" or "export_tg"
    count = context.user_data.get("count")
    word_count = context.user_data.get("word_count")

    if not count or not word_count:
        await query.edit_message_text("⏳ Session expired. Use /start to begin again.")
        return ConversationHandler.END

    est_seconds = max(1, count // GENERATION_SPEED)
    export_label = "CSV file" if export_type == "export_csv" else "TG messages"

    await query.edit_message_text(
        f"⚙️ *Generating {count:,} wallets…*\n\n"
        f"• Mnemonic: `{word_count} words`\n"
        f"• Derivation: `{DERIVATION_PATH}`\n"
        f"• Export as: `{export_label}`\n"
        f"• Estimated time: `~{est_seconds}s`\n\n"
        "_Please wait, this happens automatically\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        # CPU-heavy work runs off the event loop thread
        rows = await asyncio.to_thread(generate_wallets, count, word_count)
    except Exception as e:
        logger.exception("Wallet generation failed: %s", e)
        await query.message.reply_text(
            "❌ *Generation failed*\n\nAn unexpected error occurred\\. "
            "Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        context.user_data.clear()
        return ConversationHandler.END

    if export_type == "export_csv":
        await deliver_csv(query, rows, count)
    else:
        await deliver_tg_messages(query, context, rows, count)

    # update stats
    user = get_user(update)
    user["wallets_generated"] += count

    context.user_data.clear()
    await query.message.reply_text(
        "🏁 *All done\\!*\n\nNeed another batch? Use /start anytime\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_to_menu_keyboard(),
    )
    return ConversationHandler.END


async def deliver_csv(query, rows: list, count: int) -> None:
    csv_path = write_csv(rows)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"wallets_{count}_{timestamp}.csv"
    try:
        with open(csv_path, "rb") as f:
            await query.message.reply_document(
                document=f,
                filename=filename,
                caption=(
                    f"✅ *{count:,} wallets generated*\n\n"
                    f"📄 File: `{filename}`\n"
                    f"🔑 Columns: `#`, `address`, `private_key`, `mnemonic`\n\n"
                    "⚠️ *Security reminder*\n"
                    "Store this file in an encrypted location\\. "
                    "Never share your private keys or mnemonic phrases\\."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception as e:
        logger.exception("Failed to send CSV: %s", e)
        await query.message.reply_text(
            "❌ *Delivery failed*\n\nCould not send the file\\. Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass


async def deliver_tg_messages(query, context, rows: list, count: int) -> None:
    total_chunks = math.ceil(len(rows) / WALLETS_PER_TG_MESSAGE)
    await query.message.reply_text(
        f"📨 Sending *{count:,}* wallets in *{total_chunks}* messages "
        f"\\({WALLETS_PER_TG_MESSAGE} per message\\)\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    for i in range(0, len(rows), WALLETS_PER_TG_MESSAGE):
        chunk = rows[i : i + WALLETS_PER_TG_MESSAGE]
        part_num = i // WALLETS_PER_TG_MESSAGE + 1
        header = f"*Part {part_num}/{total_chunks}*\n\n"
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=header + format_tg_chunk(chunk),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            logger.exception("Failed to send TG chunk %s: %s", part_num, e)
        # small delay to stay under Telegram's per-chat flood limit (~1 msg/sec)
        await asyncio.sleep(1.1)

    await query.message.reply_text(
        "⚠️ *Security reminder*\n"
        "Never share your private keys or mnemonic phrases\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ========================================================================
#  CANCEL / FALLBACK HANDLERS
# ========================================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "🚫 *Session cancelled\\.*\n\nUse /start whenever you're ready\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Use /start to open the menu or /help for more info\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ========================================================================
#  MAIN
# ========================================================================
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(bulk_wallet_entry, pattern="^menu_bulk$")],
        states={
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_count)],
            ASK_WORDS: [CallbackQueryHandler(receive_words, pattern=r"^(12|15|18|21|24)$")],
            ASK_EXPORT: [
                CallbackQueryHandler(receive_export_type, pattern="^export_(csv|tg)$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(show_menu_home, pattern="^menu_home$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(conv_handler)

    app.add_handler(CallbackQueryHandler(show_menu_home, pattern="^menu_home$"))
    app.add_handler(CallbackQueryHandler(show_profile, pattern="^menu_profile$"))
    app.add_handler(CallbackQueryHandler(show_settings, pattern="^menu_settings$"))
    app.add_handler(CallbackQueryHandler(show_balance_checker, pattern="^menu_balance$"))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

"""
Wallet Generator Bot
Generates EVM-compatible HD wallets (BIP-44) on demand.
Exports as CSV — nothing stored server-side after file is sent.

Requirements:
    pip install python-telegram-bot eth-account mnemonic
"""

import logging
import asyncio
import os
import csv
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

Account.enable_unaudited_hdwallet_features()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

ASK_COUNT, ASK_WORDS = range(2)

MAX_WALLETS = 100_000
VALID_WORD_COUNTS = {12, 15, 18, 21, 24}


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Wallet Generator Bot\n\n"
        "EVM-compatible HD wallets generate kore CSV file dibo.\n"
        "Server-e kichhui save hobe na.\n\n"
        f"Koto ta wallet lagbe? (1 theke {MAX_WALLETS:,} er modhye)"
    )
    return ASK_COUNT


# --------------------------------------------------------------------------- #
# Receive wallet count
# --------------------------------------------------------------------------- #
async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("Shudhu number daw, jemon: 1000")
        return ASK_COUNT

    count = int(text)
    if count < 1 or count > MAX_WALLETS:
        await update.message.reply_text(
            f"1 theke {MAX_WALLETS:,} er modhye dite hobe."
        )
        return ASK_COUNT

    context.user_data["count"] = count

    keyboard = [
        [
            InlineKeyboardButton("12 word", callback_data="12"),
            InlineKeyboardButton("15 word", callback_data="15"),
            InlineKeyboardButton("18 word", callback_data="18"),
        ],
        [
            InlineKeyboardButton("21 word", callback_data="21"),
            InlineKeyboardButton("24 word", callback_data="24"),
        ],
    ]
    await update.message.reply_text(
        f"{count:,} ta wallet generate korbo.\n\nKoto word er mnemonic lagbe?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_WORDS


# --------------------------------------------------------------------------- #
# Receive word count, generate CSV, send file
# --------------------------------------------------------------------------- #
async def receive_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    word_count = int(query.data)
    if word_count not in VALID_WORD_COUNTS:
        await query.edit_message_text("Invalid option. /start diye abar shuru koro.")
        return ConversationHandler.END

    count = context.user_data.get("count")
    if not count:
        await query.edit_message_text("Session expired. /start diye abar shuru koro.")
        return ConversationHandler.END

    # Estimate time for user feedback
    est_seconds = max(1, count // 800)
    await query.edit_message_text(
        f"Generating {count:,} ta wallet ({word_count}-word mnemonic)...\n"
        f"Estimated time: ~{est_seconds}s\n"
        "CSV ready hole pathabo."
    )

    # Run CPU-bound generation in a thread so the event loop doesn't block
    csv_path = await asyncio.to_thread(_generate_csv, count, word_count)

    try:
        with open(csv_path, "rb") as f:
            await query.message.reply_document(
                document=f,
                filename=f"wallets_{count}.csv",
                caption=(
                    f"{count:,} ta wallet ready.\n"
                    "IMPORTANT: ei file safe jagay backup rakho, delete kore dao."
                ),
            )
    finally:
        # Always delete temp file after sending (or on error)
        try:
            os.remove(csv_path)
        except OSError:
            pass

    context.user_data.clear()
    await query.message.reply_text(
        "Done! Arekta batch banate chaile /start daw."
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Wallet generation + CSV write (runs in thread)
# --------------------------------------------------------------------------- #
def _generate_csv(count: int, word_count: int) -> str:
    mnemo = Mnemonic("english")
    strength_map = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
    strength = strength_map[word_count]

    # Write directly to CSV while generating — avoids holding 100k dicts in RAM
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".csv",
        delete=False,
        newline="",
        encoding="utf-8",
    )
    try:
        writer = csv.writer(tmp)
        writer.writerow(["#", "address", "private_key", "mnemonic"])

        for i in range(count):
            phrase = mnemo.generate(strength=strength)
            acct = Account.from_mnemonic(phrase, account_path="m/44'/60'/0'/0/0")
            writer.writerow([i + 1, acct.address, acct.key.hex(), phrase])

        tmp.flush()
    finally:
        tmp.close()

    return tmp.name


# --------------------------------------------------------------------------- #
# Cancel / fallback
# --------------------------------------------------------------------------- #
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Cancel kora hoyeche. /start diye abar shuru korte paro."
    )
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Bujhte parini. /start daw.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_count)],
            ASK_WORDS: [CallbackQueryHandler(receive_words)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

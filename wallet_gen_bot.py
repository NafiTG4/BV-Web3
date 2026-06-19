"""
Wallet Generator Bot
Generates EVM-compatible HD wallets (BIP-44) on demand.
Nothing is stored server-side — all output goes directly to the user in Telegram.

Requirements:
    pip install python-telegram-bot eth-account mnemonic
"""

import logging
import asyncio
import os
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

# Enable BIP-44 HD wallet derivation
Account.enable_unaudited_hdwallet_features()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Conversation states
ASK_COUNT, ASK_WORDS = range(2)

MAX_WALLETS = 1000
VALID_WORD_COUNTS = {12, 15, 18, 21, 24}


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Wallet Generator Bot\n\n"
        "ami tomake EVM-compatible HD wallets generate kore dibo.\n"
        "Kono kichhui server-e save hobe na, shob directly tomar kache pathabo.\n\n"
        "Koto ta wallet lagbe? (1 theke 1000 er modhye)"
    )
    return ASK_COUNT


# --------------------------------------------------------------------------- #
# Receive wallet count
# --------------------------------------------------------------------------- #
async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("Shudhu number daw, jemon: 50")
        return ASK_COUNT

    count = int(text)
    if count < 1 or count > MAX_WALLETS:
        await update.message.reply_text(f"1 theke {MAX_WALLETS} er modhye dite hobe.")
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
        f"{count} ta wallet generate korbo.\n\nKoto word er mnemonic lagbe?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_WORDS


# --------------------------------------------------------------------------- #
# Receive word count via inline button
# --------------------------------------------------------------------------- #
async def receive_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    word_count = int(query.data)
    if word_count not in VALID_WORD_COUNTS:
        await query.edit_message_text("Invalid option, /start diye abar shuru koro.")
        return ConversationHandler.END

    count = context.user_data.get("count")
    if not count:
        await query.edit_message_text("Session expired. /start diye abar shuru koro.")
        return ConversationHandler.END

    await query.edit_message_text(
        f"Generating {count} ta wallet ({word_count}-word mnemonic)...\n"
        "ektu wait koro..."
    )

    # Generate wallets (CPU-bound, but small enough; no blocking concern at <=1000)
    wallets = _generate_wallets(count, word_count)

    # Split into chunks so Telegram message size limit (4096 chars) isn't hit
    chunks = _format_chunks(wallets)

    user = query.from_user
    await query.message.reply_text(
        f"Done! {count} ta wallet ready.\n"
        f"Nichey send korchi (shob message check koro):\n"
        f"IMPORTANT: ebar ei gulo safe jagay backup rakho."
    )

    for chunk in chunks:
        await query.message.reply_text(chunk, parse_mode=None)
        # Small delay to avoid Telegram flood limits
        await asyncio.sleep(0.3)

    context.user_data.clear()
    await query.message.reply_text(
        "Shob wallet pathano hoyeche.\n"
        "Arekta batch banate chaile /start daw."
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Wallet generation logic
# --------------------------------------------------------------------------- #
def _generate_wallets(count: int, word_count: int) -> list[dict]:
    mnemo = Mnemonic("english")
    # BIP-39 strength mapping: 12w=128bit, 15w=160, 18w=192, 21w=224, 24w=256
    strength_map = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
    strength = strength_map[word_count]

    wallets = []
    for i in range(count):
        phrase = mnemo.generate(strength=strength)
        # BIP-44 path: m/44'/60'/0'/0/0 (standard EVM)
        acct = Account.from_mnemonic(phrase, account_path="m/44'/60'/0'/0/0")
        wallets.append(
            {
                "index": i + 1,
                "address": acct.address,
                "private_key": acct.key.hex(),
                "mnemonic": phrase,
            }
        )
    return wallets


# --------------------------------------------------------------------------- #
# Format wallets into <=4000 char chunks
# --------------------------------------------------------------------------- #
def _format_chunks(wallets: list[dict], chunk_size: int = 4000) -> list[str]:
    lines = []
    for w in wallets:
        lines.append(
            f"--- Wallet #{w['index']} ---\n"
            f"Address   : {w['address']}\n"
            f"PrivateKey: {w['private_key']}\n"
            f"Mnemonic  : {w['mnemonic']}\n"
        )

    chunks = []
    current = ""
    for block in lines:
        if len(current) + len(block) + 1 > chunk_size:
            chunks.append(current.strip())
            current = block
        else:
            current += "\n" + block

    if current.strip():
        chunks.append(current.strip())

    return chunks


# --------------------------------------------------------------------------- #
# Cancel / fallback
# --------------------------------------------------------------------------- #
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Cancel kora hoyeche. /start diye abar shuru korte paro.")
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

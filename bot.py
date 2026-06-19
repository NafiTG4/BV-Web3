"""
EVM Wallet Generator Bot – BIP-44 HD Wallet Generator
Compatible with MetaMask, Rabby, Bitget Wallet, and all EVM chains.
Optimized for Railway Trial Plan (2 vCPU, 0.5 GB RAM).
Generates up to 100,000 wallets per batch and delivers as CSV.
"""

import logging
import asyncio
import os
import csv
import tempfile
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

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
from pybip39 import Mnemonic   # Rust-based, 5-10x faster than 'mnemonic'

# ========================================================================
#  কনফিগারেশন ও গ্লোবাল সেটিংস
# ========================================================================
Account.enable_unaudited_hdwallet_features()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# আনুমানিক জেনারেশন স্পিড (ওয়ালেট/সেকেন্ড) – এনভায়রনমেন্ট ভেরিয়েবল দিয়ে কাস্টমাইজ করা যায়
GENERATION_SPEED = int(os.environ.get("GEN_SPEED", 12000))

# কনভার্সেশন স্টেট
ASK_COUNT, ASK_WORDS = range(2)

MAX_WALLETS = 100_000
VALID_WORD_COUNTS = {12, 15, 18, 21, 24}
STRENGTH_MAP = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
DERIVATION_PATH = "m/44'/60'/0'/0/0"   # Standard Ethereum path

# Railway Trial-এ ২ vCPU, তাই ২টি ওয়ার্কার
MAX_WORKERS = 2
BATCH_SIZE = 5000   # প্রতি ব্যাচে কয়টি ওয়ালেট (RAM বাঁচাতে)

# ========================================================================
#  মার্কডাউন টেক্সট (MarkdownV2‑এর জন্য সব ক্যারেক্টার এস্কেপ করা)
# ========================================================================
WELCOME_TEXT = (
    "*EVM Wallet Generator*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Generates BIP\\-44 HD wallets compatible with all EVM chains "
    "\\(Ethereum, BSC, Polygon, Arbitrum, etc\\.\\)\n\n"
    "*Privacy:* Nothing is stored on the server\\. "
    "Your CSV is deleted immediately after delivery\\.\n\n"
    f"*Limit:* Up to {MAX_WALLETS:,} wallets per batch\n\n"
    "How many wallets do you need?"
)

HELP_TEXT = (
    "*EVM Wallet Generator — Help*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "*Commands*\n"
    "/start — Generate a new batch of wallets\n"
    "/help  — Show this help message\n"
    "/cancel — Cancel the current session\n\n"
    "*How it works*\n"
    "1\\. Send the number of wallets you need\n"
    "2\\. Choose your mnemonic word count\n"
    "3\\. Receive a CSV file with all wallets\n\n"
    "*CSV columns*\n"
    "`#` · `address` · `private_key` · `mnemonic`\n\n"
    "*Security*\n"
    "• Wallets use BIP\\-44 standard derivation\n"
    "• Compatible with MetaMask, Trust Wallet, etc\\.\n"
    "• Store your CSV in an encrypted location\n"
    "• Never share your private keys or mnemonic\n\n"
    f"*Batch limit:* {MAX_WALLETS:,} wallets"
)

# ========================================================================
#  ওয়ালেট জেনারেশন ওয়ার্কার (প্রতিটি প্রসেসে চলে)
# ========================================================================
def generate_batch(start_idx: int, end_idx: int, word_count: int) -> list:
    """
    একটি নির্দিষ্ট রেঞ্জের ওয়ালেট জেনারেট করে।
    প্রতিটি প্রসেস আলাদা Mnemonic instance ব্যবহার করে।
    """
    mnemo = Mnemonic()
    strength = STRENGTH_MAP[word_count]
    results = []
    for i in range(start_idx, end_idx):
        phrase = mnemo.generate(strength=strength)
        acct = Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
        results.append([i + 1, acct.address, acct.key.hex(), phrase])
    return results

# ========================================================================
#  CSV জেনারেশন (স্ট্রিমিং + মাল্টিপ্রসেসিং)
# ========================================================================
def generate_csv_streaming(count: int, word_count: int) -> str:
    """
    ব্যাচ আকারে ওয়ালেট জেনারেট করে CSV-তে স্ট্রিম করে।
    পুরো ডেটা মেমORYতে রাখে না, তাই 0.5 GB RAM-এর জন্য নিরাপদ।
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(tmp)
        writer.writerow(["#", "address", "private_key", "mnemonic"])

        # ব্যাচে ভাগ করা
        batches = []
        for start in range(0, count, BATCH_SIZE):
            end = min(start + BATCH_SIZE, count)
            batches.append((start, end, word_count))

        # মাল্টিপ্রসেসিং পুল
        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(generate_batch, s, e, wc) for s, e, wc in batches]
            for future in futures:
                rows = future.result()
                writer.writerows(rows)   # স্ট্রিমিং-এ লেখা
                tmp.flush()              # ডিস্কে ফ্লাশ

        tmp.flush()
    finally:
        tmp.close()
    return tmp.name

# ========================================================================
#  হ্যান্ডলার ফাংশন
# ========================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.MARKDOWN_V2)
    return ASK_COUNT

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)

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
            InlineKeyboardButton("24 words  (most secure)", callback_data="24"),
        ],
    ]

    await update.message.reply_text(
        f"✅ *{count:,} wallets* selected\\.\n\n"
        "Choose your *mnemonic phrase length*:\n\n"
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

    est_seconds = max(1, count // GENERATION_SPEED)

    await query.edit_message_text(
        f"⚙️ *Generating {count:,} wallets…*\n\n"
        f"• Mnemonic: `{word_count} words`\n"
        f"• Derivation: `{DERIVATION_PATH}`\n"
        f"• Estimated time: `~{est_seconds}s`\n\n"
        "_Please wait — your CSV will be sent automatically\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        # CPU-ভারী কাজটি async thread-এ চালানো
        csv_path = await asyncio.to_thread(generate_csv_streaming, count, word_count)
    except Exception as e:
        logger.exception("CSV generation failed: %s", e)
        await query.message.reply_text(
            "❌ *Generation failed*\n\nAn unexpected error occurred\\. "
            "Please try again with /start\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        context.user_data.clear()
        return ConversationHandler.END

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

    context.user_data.clear()
    await query.message.reply_text(
        "🏁 *All done\\!*\n\nNeed another batch? Use /start anytime\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END

# ========================================================================
#  ক্যানসেল ও ফলব্যাক হ্যান্ডলার
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
        "Use /start to generate wallets or /help for more info\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

# ========================================================================
#  মেইন ফাংশন
# ========================================================================
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

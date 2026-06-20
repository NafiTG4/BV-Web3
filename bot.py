"""
EVM Wallet Generator Bot – BIP-44 HD Wallet Generator
Compatible with MetaMask, Rabby, Bitget Wallet, and all EVM chains.
Optimized for Railway Trial Plan (2 vCPU, 0.5 GB RAM).
Features:
- Rate limiter: max 28 API calls/sec
- Admin group with panel (User Info, Balance, Rate, Wallet Generate Limit)
- Global wallet limit stored in config.json
- Private chat only, ignores group messages except admin group
"""

import logging
import asyncio
import os
import csv
import tempfile
import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from typing import Optional, Dict, Any

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
from telegram.request import BaseRequest, HTTPXRequest
from eth_account import Account
from mnemonic import Mnemonic

# ========================================================================
#  কনফিগারেশন
# ========================================================================
Account.enable_unaudited_hdwallet_features()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", 0))  # সেট করা আবশ্যক

# ডিফল্ট স্পিড (wallets/sec) – mnemonic লাইব্রেরির জন্য ~800
GENERATION_SPEED = int(os.environ.get("GEN_SPEED", 800))

# কনভার্সেশন স্টেট
ASK_COUNT, ASK_WORDS, ASK_ADMIN_LIMIT = range(3)

VALID_WORD_COUNTS = {12, 15, 18, 21, 24}
STRENGTH_MAP = {12: 128, 15: 160, 18: 192, 21: 224, 24: 256}
DERIVATION_PATH = "m/44'/60'/0'/0/0"

# মাল্টিপ্রসেসিং কনফিগ
MAX_WORKERS = 2
BATCH_SIZE = 5000

# ========================================================================
#  রেট লিমিটার (প্রতি সেকেন্ডে ২৮টি কল)
# ========================================================================
class RateLimiter:
    def __init__(self, rate: int = 28, per: float = 1.0):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            if elapsed > self.per:
                self.tokens = min(self.rate, self.tokens + int(elapsed * self.rate / self.per))
                self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            # অপেক্ষা করে আবার চেষ্টা
            wait_time = self.per - elapsed
            await asyncio.sleep(wait_time)
            self.tokens = self.rate - 1
            self.last_refill = time.monotonic()

# ========================================================================
#  কাস্টম টেলিগ্রাম রিকোয়েস্ট (রেট লিমিট সহ)
# ========================================================================
class RateLimitedRequest(BaseRequest):
    def __init__(self, rate: int = 28, per: float = 1.0, **kwargs):
        super().__init__(**kwargs)
        self._inner = HTTPXRequest(**kwargs)
        self._limiter = RateLimiter(rate, per)

    async def post(self, url: str, data: Optional[Dict] = None, files=None, **kwargs):
        await self._limiter.acquire()
        return await self._inner.post(url, data, files, **kwargs)

    async def do_post(self, url: str, data: Optional[Dict] = None, files=None, **kwargs):
        await self._limiter.acquire()
        return await self._inner.do_post(url, data, files, **kwargs)

# ========================================================================
#  কনফিগ ফাইল হ্যান্ডলিং (config.json)
# ========================================================================
CONFIG_FILE = "config.json"

def load_config() -> Dict[str, Any]:
    """config.json থেকে ডেটা লোড করে, না থাকলে ডিফল্ট তৈরি করে"""
    default = {"wallet_generate_limit": 100_000}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return default
    return default

def save_config(config: Dict[str, Any]) -> None:
    """config.json-এ ডেটা সেভ করে"""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ========================================================================
#  মার্কডাউন টেক্সট
# ========================================================================
WELCOME_TEXT = (
    "*EVM Wallet Generator*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Generates BIP\\-44 HD wallets compatible with all EVM chains "
    "\\(Ethereum, BSC, Polygon, Arbitrum, etc\\.\\)\n\n"
    "*Privacy:* Nothing is stored on the server\\. "
    "Your CSV is deleted immediately after delivery\\.\n\n"
    f"*Limit:* Up to {load_config()['wallet_generate_limit']:,} wallets per batch\n\n"
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
    f"*Batch limit:* {load_config()['wallet_generate_limit']:,} wallets"
)

ADMIN_PANEL_TEXT = (
    "*Admin Panel*\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Use the buttons below to manage the bot\\."
)

# ========================================================================
#  ওয়ালেট জেনারেশন ওয়ার্কার
# ========================================================================
def generate_batch(start_idx: int, end_idx: int, word_count: int) -> list:
    mnemo = Mnemonic("english")
    strength = STRENGTH_MAP[word_count]
    results = []
    for i in range(start_idx, end_idx):
        phrase = mnemo.generate(strength=strength)
        acct = Account.from_mnemonic(phrase, account_path=DERIVATION_PATH)
        results.append([i + 1, acct.address, acct.key.hex(), phrase])
    return results

def generate_csv_streaming(count: int, word_count: int) -> str:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
    )
    try:
        writer = csv.writer(tmp)
        writer.writerow(["#", "address", "private_key", "mnemonic"])

        batches = []
        for start in range(0, count, BATCH_SIZE):
            end = min(start + BATCH_SIZE, count)
            batches.append((start, end, word_count))

        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(generate_batch, s, e, wc) for s, e, wc in batches]
            for future in futures:
                rows = future.result()
                writer.writerows(rows)
                tmp.flush()
        tmp.flush()
    finally:
        tmp.close()
    return tmp.name

# ========================================================================
#  হ্যান্ডলার – সাধারণ ইউজার ফ্লো
# ========================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start – অ্যাডমিন গ্রুপে প্যানেল, অন্যথায় সাধারণ ওয়েলকাম"""
    chat_id = update.effective_chat.id

    # অ্যাডমিন গ্রুপে প্যানেল
    if ADMIN_GROUP_ID and chat_id == ADMIN_GROUP_ID:
        keyboard = [
            [InlineKeyboardButton("👤 User Info", callback_data="admin_user_info")],
            [InlineKeyboardButton("💰 Balance", callback_data="admin_balance")],
            [InlineKeyboardButton("⚡ Rate", callback_data="admin_rate")],
            [InlineKeyboardButton("📊 Wallet Generate Limit", callback_data="admin_set_limit")],
        ]
        await update.message.reply_text(
            ADMIN_PANEL_TEXT,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    # প্রাইভেট চ্যাটে সাধারণ ফ্লো
    if update.effective_chat.type != "private":
        await update.message.reply_text("This bot works only in private chats.")
        return ConversationHandler.END

    context.user_data.clear()
    config = load_config()
    limit = config.get("wallet_generate_limit", 100_000)
    welcome = WELCOME_TEXT.replace(str(load_config()['wallet_generate_limit']), f"{limit:,}")
    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN_V2)
    return ASK_COUNT

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help – শুধু প্রাইভেট চ্যাটে"""
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN_V2)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel – শুধু প্রাইভেট চ্যাটে"""
    if update.effective_chat.type != "private":
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "🚫 *Session cancelled\\.*\n\nUse /start whenever you're ready\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END

async def receive_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ইউজারের কাউন্ট ইনপুট নেয় – শুধু প্রাইভেট চ্যাটে"""
    if update.effective_chat.type != "private":
        return ConversationHandler.END

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ *Invalid input*\n\nPlease enter a number\\. Example: `1000`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ASK_COUNT

    count = int(text)
    config = load_config()
    limit = config.get("wallet_generate_limit", 100_000)
    if count < 1 or count > limit:
        await update.message.reply_text(
            f"⚠️ *Out of range*\n\nPlease enter a number between `1` and `{limit:,}`\\.",
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
    """ওয়ার্ড কাউন্ট সিলেক্ট – শুধু প্রাইভেট চ্যাটে"""
    query = update.callback_query
    if update.effective_chat.type != "private":
        await query.answer("Not allowed here.")
        return ConversationHandler.END

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
#  অ্যাডমিন প্যানেল – ক্যালব্যাক হ্যান্ডলার
# ========================================================================
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """অ্যাডমিন প্যানেলের বাটন হ্যান্ডল করে"""
    query = update.callback_query
    await query.answer()

    # শুধু অ্যাডমিন গ্রুপে কাজ করবে
    if update.effective_chat.id != ADMIN_GROUP_ID:
        await query.edit_message_text("⛔ Unauthorized.")
        return ConversationHandler.END

    data = query.data

    if data == "admin_user_info":
        # সহজ পরিসংখ্যান (শুধু ডেমো)
        text = (
            "*User Info*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "• Total users: *N/A* (tracking not implemented)\n"
            "• Active sessions: *N/A*"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    elif data == "admin_balance":
        text = (
            "*Balance*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "This bot does not hold any funds\\.\n"
            "It only generates wallets\\."
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    elif data == "admin_rate":
        text = (
            "*Generation Rate*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"• Current speed: *{GENERATION_SPEED:,} wallets/sec*\n"
            "• Estimated time is calculated based on this value\\.\n"
            "• To change speed, set `GEN_SPEED` environment variable\\."
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return ConversationHandler.END

    elif data == "admin_set_limit":
        # কনভার্সেশন শুরু – লিমিট ইনপুট নেওয়া
        await query.edit_message_text(
            "✏️ Enter the new wallet generation limit (number):\n\n"
            "Example: `50000`\n"
            "Current limit: " + str(load_config().get("wallet_generate_limit", 100_000))
        )
        return ASK_ADMIN_LIMIT

    return ConversationHandler.END

async def admin_set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """অ্যাডমিনের দেওয়া নতুন লিমিট সেভ করে"""
    if update.effective_chat.id != ADMIN_GROUP_ID:
        return ConversationHandler.END

    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please enter a valid number.")
        return ASK_ADMIN_LIMIT

    new_limit = int(text)
    if new_limit < 1:
        await update.message.reply_text("❌ Limit must be at least 1.")
        return ASK_ADMIN_LIMIT

    config = load_config()
    config["wallet_generate_limit"] = new_limit
    save_config(config)

    await update.message.reply_text(
        f"✅ Wallet generation limit updated to *{new_limit:,}*.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    # প্যানেলে ফিরে যেতে /start দিতে বলি
    await update.message.reply_text("Use /start to return to admin panel.")
    return ConversationHandler.END

# ========================================================================
#  ফলব্যাক – অজানা কমান্ড
# ========================================================================
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """অজানা কমান্ড – শুধু প্রাইভেট চ্যাটে"""
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "Use /start to generate wallets or /help for more info\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

# ========================================================================
#  মেইন ফাংশন
# ========================================================================
def main() -> None:
    # রেট-লিমিটেড রিকোয়েস্ট ক্লাস ব্যবহার করি
    request = RateLimitedRequest(rate=28, per=1.0)
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    # কনভার্সেশন হ্যান্ডলার (ইউজার ফ্লো)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_COUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                    receive_count
                )
            ],
            ASK_WORDS: [
                CallbackQueryHandler(receive_words, pattern="^(12|15|18|21|24)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # অ্যাডমিন লিমিট সেট করার জন্য আলাদা কনভার্সেশন (শুধু অ্যাডমিন গ্রুপ)
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^admin_")],
        states={
            ASK_ADMIN_LIMIT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    admin_set_limit
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(admin_conv)
    app.add_handler(CommandHandler("help", help_command))
    # অ্যাডমিন গ্রুপে /start আবার প্যানেল দেখানোর জন্য
    app.add_handler(CommandHandler("start", start))  # এটি conv_handler-এর এন্ট্রি পয়েন্ট, তবে ওভাররাইড করবে না

    # অন্য সব কমান্ড ইগনোর (শুধু প্রাইভেট চ্যাটে)
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown))

    logger.info("Bot started with rate limiter (28 req/s) and admin panel.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

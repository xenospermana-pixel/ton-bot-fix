"""Telegram bot untuk memantau saldo dan transaksi TON serta USDT."""
import asyncio, json, logging, os, requests, time
from decimal import Decimal
from pathlib import Path
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

HOLDER_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"
JETTON_WALLET_USDT = "EQAmwNPCaojho0YTS8ZfwnK5zHjduMZeZbeie5dLHeFTAWD7"
USDT_MASTER_ADDRESS = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
TONAPI_URL = "https://tonapi.io/v2"
NANO_TON = 9
USDT_DECIMALS = 6
DATA_FILE = "last_tx.json"
logger = logging.getLogger(__name__)

def format_ton(nano_ton: int) -> str: return f"{Decimal(nano_ton) / Decimal(10**NANO_TON):.3f}"
def format_usdt(micro_usdt: int) -> str: return f"{Decimal(micro_usdt) / Decimal(10**USDT_DECIMALS):.2f}"
def parse_integer(value):
    try: return int(str(value))
    except: return 0
def short_addr(addr): return f"`{addr[:4]}...{addr[-4:]}`" if addr else "`-`"

def load_last_tx():
    if Path(DATA_FILE).exists():
        return json.loads(Path(DATA_FILE).read_text())
    return {"ton": 0, "usdt": 0, "last_hash_ton": "", "last_hash_usdt": ""}

def save_last_tx(data):
    Path(DATA_FILE).write_text(json.dumps(data))

class TonClient:
    def __init__(self) -> None:
        self.session = requests.Session()
    def close(self) -> None: self.session.close()

    async def get_balance_nano_ton(self) -> int:
        r = await asyncio.to_thread(self.session.get, f"{TONAPI_URL}/account/{HOLDER_ADDRESS}", timeout=30)
        return parse_integer(r.json().get("balance"))

    async def get_balance_micro_usdt(self) -> int:
        r = await asyncio.to_thread(self.session.get, f"{TONAPI_URL}/account/{HOLDER_ADDRESS}/jettons", timeout=30)
        for j in r.json().get("balances", []):
            if j.get("jetton", {}).get("address") == USDT_MASTER_ADDRESS:
                return parse_integer(j.get("balance"))
        return 0

async def post_init(application: Application) -> None:
    application.bot_data["ton_client"] = TonClient()
    application.bot_data["last_tx"] = load_last_tx()
    application.job_queue.run_repeating(check_balance, interval=300, first=10)
    application.job_queue.run_repeating(auto_report, interval=3600, first=60)
    logger.info("Bot TON + USDT aktif untuk wallet %s", HOLDER_ADDRESS)

async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("ton_client")
    if client: client.close()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.application.bot_data["ton_client"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    await update.message.reply_text(f"💼 MONITOR WALLET AKTIF\n─────────────────\n💎 Saldo TON: {format_ton(ton)}\n💵 Saldo USDT: {format_usdt(usdt)}\n─────────────────\nCommand: /balance /history")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.application.bot_data["ton_client"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    await update.message.reply_text(f"💼 SALDO SAAT INI\n─────────────────\n💎 TON: {format_ton(ton)}\n💵 USDT: {format_usdt(usdt)}\n─────────────────\nWallet: {short_addr(HOLDER_ADDRESS)}")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Fitur history dinonaktifkan sementara buat ngetes balance")

async def send_report(context: ContextTypes.DEFAULT_TYPE, ton: int, usdt: int, tipe: str = "PERUBAHAN"):
    chat_ids = os.getenv("CHAT_ID", "")
    if not chat_ids: return
    text = f"🔔 {tipe} SALDO\nWallet: {short_addr(HOLDER_ADDRESS)}\n─────────────────\n💎 TON: {format_ton(ton)}\n💵 USDT: {format_usdt(usdt)}\n"
    for chat_id in chat_ids.split(","):
        chat_id = chat_id.strip()
        if chat_id:
            try: await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e: logger.error(f"Gagal kirim ke {chat_id}: {e}")

async def check_balance(context: ContextTypes.DEFAULT_TYPE):
    app = context.application; client = app.bot_data["ton_client"]; last_tx = app.bot_data["last_tx"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    if ton!= last_tx["ton"] or usdt!= last_tx["usdt"]:
        await send_report(context, ton, usdt, "PERUBAHAN")
        app.bot_data["last_tx"] = {"ton": ton, "usdt": usdt, "last_hash_ton": "", "last_hash_usdt": ""}
        save_last_tx(app.bot_data["last_tx"])

async def auto_report(context: ContextTypes.DEFAULT_TYPE):
    app = context.application; client = app.bot_data["ton_client"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    await send_report(context, ton, usdt, "LAPORAN 1 JAM")

def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not telegram_token: raise RuntimeError("TELEGRAM_TOKEN belum diatur")
    app = ApplicationBuilder().token(telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("history", history_command))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    main()

"""Telegram bot untuk memantau saldo dan transaksi TON serta USDT."""
import asyncio, json, logging, os, requests
from decimal import Decimal
from pathlib import Path
from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

WALLET_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"
WALLET_ADDRESS_RAW = "0:D298146D13EF36F31E4B9AC58DEEBF9A2297E36042730812363888B236E9F000"
USDT_MASTER_ADDRESS_RAW = "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE"
TONCENTER_V2_API_URL = "https://toncenter.com/api/v2"
TONCENTER_V3_API_URL = "https://toncenter.com/api/v3"
NANO_TON = 9
USDT_DECIMALS = 6
DATA_FILE = "last_tx.json"
logger = logging.getLogger(__name__)

def format_ton(nano_ton: int) -> str: return f"{Decimal(nano_ton) / Decimal(10**NANO_TON):.3f}"
def format_usdt(micro_usdt: int) -> str: return f"{Decimal(micro_usdt) / Decimal(10**USDT_DECIMALS):.3f}"
def parse_integer(value): 
    try: return int(str(value))
    except: return 0

def load_last_tx():
    if Path(DATA_FILE).exists():
        return json.loads(Path(DATA_FILE).read_text())
    return {"ton": 0, "usdt": 0}

def save_last_tx(data):
    Path(DATA_FILE).write_text(json.dumps(data))

class TonCenterClient:
    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})
    def close(self) -> None: self.session.close()
    async def get_balance_nano_ton(self) -> int:
        r = await asyncio.to_thread(self.session.get, f"{TONCENTER_V2_API_URL}/getAddressBalance", params={"address": WALLET_ADDRESS}, timeout=30)
        return parse_integer(r.json().get("result"))
    async def get_balance_micro_usdt(self) -> int:
        r = await asyncio.to_thread(self.session.get, f"{TONCENTER_V3_API_URL}/jetton/wallets", params={"owner_address": WALLET_ADDRESS, "jetton_master": USDT_MASTER_ADDRESS_RAW}, timeout=30)
        for w in r.json().get("jetton_wallets", []): 
            if str(w.get("jetton")) == USDT_MASTER_ADDRESS_RAW: return parse_integer(w.get("balance"))
        return 0

async def post_init(application: Application) -> None:
    ton_api_key = os.getenv("TON_API_KEY", "").strip()
    if not ton_api_key: raise RuntimeError("TON_API_KEY belum diatur")
    application.bot_data["ton_client"] = TonCenterClient(ton_api_key)
    application.bot_data["last_tx"] = load_last_tx()
    application.job_queue.run_repeating(check_balance, interval=300, first=10) # cek tiap 5 menit
    application.job_queue.run_repeating(auto_report, interval=3600, first=60) # kirim tiap 1 jam
    logger.info("Bot TON + USDT aktif untuk wallet %s", WALLET_ADDRESS)

async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("ton_client")
    if client: client.close()

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Bot aktif")
    client = context.application.bot_data["ton_client"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    await update.message.reply_text(
        f"💼 MONITOR WALLET\n"
        f"─────────────────\n"
        f"💎 Saldo TON: {format_ton(ton)}\n"
        f"💵 Saldo USDT: {format_usdt(usdt)}\n"
        f"─────────────────\n"
        f"Status: Aktif 24 Jam"
    )

async def send_report(context: ContextTypes.DEFAULT_TYPE, ton: int, usdt: int, tipe: str = "PERUBAHAN"):
    chat_ids = os.getenv("CHAT_ID", "") # support banyak ID pake koma
    if not chat_ids: return
    text = f"🔔 {tipe} SALDO\n"
    text += f"Wallet: `{WALLET_ADDRESS}`\n"
    text += f"─────────────────\n"
    text += f"💎 TON: {format_ton(ton)}\n"
    text += f"💵 USDT: {format_usdt(usdt)}\n"
    
    for chat_id in chat_ids.split(","):
        chat_id = chat_id.strip()
        if chat_id:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Gagal kirim ke {chat_id}: {e}")

async def check_balance(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    client = app.bot_data["ton_client"]
    last_tx = app.bot_data["last_tx"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    
    if ton != last_tx["ton"] or usdt != last_tx["usdt"]:
        await send_report(context, ton, usdt, "PERUBAHAN")
        app.bot_data["last_tx"] = {"ton": ton, "usdt": usdt}
        save_last_tx(app.bot_data["last_tx"])

async def auto_report(context: ContextTypes.DEFAULT_TYPE):
    app = context.application
    client = app.bot_data["ton_client"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    await send_report(context, ton, usdt, "LAPORAN 1 JAM")

def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not telegram_token: raise RuntimeError("TELEGRAM_TOKEN belum diatur")
    app = ApplicationBuilder().token(telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start_command))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    main()

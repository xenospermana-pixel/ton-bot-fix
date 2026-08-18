"""Telegram bot untuk memantau saldo dan transaksi TON serta USDT."""
import asyncio, json, logging, os, requests, time
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
def short_addr(addr): return f"`{addr[:4]}...{addr[-4:]}`" if addr else "`-`"

def load_last_tx():
    if Path(DATA_FILE).exists():
        return json.loads(Path(DATA_FILE).read_text())
    return {"ton": 0, "usdt": 0, "last_hash_ton": "", "last_hash_usdt": ""}

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

    async def get_last_ton_transaction(self):
        r = await asyncio.to_thread(self.session.get, f"{TONCENTER_V2_API_URL}/getTransactions", params={"address": WALLET_ADDRESS, "limit": 1}, timeout=30)
        txs = r.json().get("result", [])
        return txs[0] if txs else None

    async def get_ton_history(self, limit=5):
        r = await asyncio.to_thread(self.session.get, f"{TONCENTER_V2_API_URL}/getTransactions", params={"address": WALLET_ADDRESS, "limit": limit}, timeout=30)
        return r.json().get("result", [])

    async def get_last_usdt_transaction(self):
        r = await asyncio.to_thread(self.session.get, f"{TONCENTER_V3_API_URL}/jetton/transfers", params={"account": WALLET_ADDRESS, "jetton": USDT_MASTER_ADDRESS_RAW, "limit": 1}, timeout=30)
        txs = r.json().get("jetton_transfers", [])
        return txs[0] if txs else None

    async def get_usdt_history(self, limit=5):
        r = await asyncio.to_thread(self.session.get, f"{TONCENTER_V3_API_URL}/jetton/transfers", params={"account": WALLET_ADDRESS, "jetton": USDT_MASTER_ADDRESS_RAW, "limit": limit}, timeout=30)
        return r.json().get("jetton_transfers", [])

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
        f"Command: /balance /history"
    )

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.application.bot_data["ton_client"]
    ton, usdt = await asyncio.gather(client.get_balance_nano_ton(), client.get_balance_micro_usdt())
    await update.message.reply_text(
        f"💼 SALDO SAAT INI\n"
        f"─────────────────\n"
        f"💎 TON: {format_ton(ton)}\n"
        f"💵 USDT: {format_usdt(usdt)}\n"
        f"─────────────────\n"
        f"Wallet: {short_addr(WALLET_ADDRESS)}"
    )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client = context.application.bot_data["ton_client"]
    await update.message.reply_text("⏳ Ngambil 5 transaksi terakhir...")

    ton_txs, usdt_txs = await asyncio.gather(
        client.get_ton_history(5),
        client.get_usdt_history(5)
    )

    text = f"📜 5 RIWAYAT TERAKHIR\n"
    text += f"Wallet: {short_addr(WALLET_ADDRESS)}\n"
    text += f"─────────────────\n"

    # Gabungin TON + USDT lalu sort by waktu
    all_txs = []
    for tx in ton_txs:
        all_txs.append({"type": "TON", "time": tx.get("utime", 0), "data": tx})
    for tx in usdt_txs:
        all_txs.append({"type": "USDT", "time": tx.get("created_at", 0), "data": tx})

    all_txs.sort(key=lambda x: x["time"], reverse=True)
    all_txs = all_txs[:5]

    if not all_txs:
        text += "Belum ada transaksi"
    else:
        for i, tx in enumerate(all_txs, 1):
            if tx["type"] == "TON":
                data = tx["data"]
                in_msg = data.get("in_msg", {})
                out_msgs = data.get("out_msgs", [])
                value = parse_integer(in_msg.get("value", 0))
                addr = in_msg.get("source", "")
                emoji, tipe = "📥", "MASUK"
                if out_msgs:
                    value = parse_integer(out_msgs[0].get("value", 0))
                    addr = out_msgs[0].get("destination", "")
                    emoji, tipe = "📤", "KELUAR"
                text += f"{i}. {emoji} {tipe} {format_ton(value)} TON\n"
                text += f" Ke: {short_addr(addr)}\n"
                text += f" {time.strftime('%d-%m %H:%M', time.localtime(tx['time']))}\n\n"
            else:
                data = tx["data"]
                amount = parse_integer(data.get("amount", 0))
                sender = data.get("sender", {}).get("address", "")
                recipient = data.get("recipient", {}).get("address", "")
                emoji, tipe, addr = "📥", "MASUK", sender
                if str(recipient) == WALLET_ADDRESS_RAW:
                    addr = sender
                else:
                    emoji, tipe, addr = "📤", "KELUAR", recipient
                text += f"{i}. {emoji} {tipe} {format_usdt(amount)} USDT\n"
                text += f" Ke: {short_addr(addr)}\n"
                text += f" {time.strftime('%d-%m %H:%M', time.localtime(tx['time']))}\n\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def send_report(context: ContextTypes.DEFAULT_TYPE, ton: int, usdt: int, tipe: str = "PERUBAHAN", tx_info: dict = None):
    chat_ids = os.getenv("CHAT_ID", "") # support banyak ID pake koma
    if not chat_ids: return

    text = f"🔔 {tipe} SALDO\n"
    text += f"Wallet: {short_addr(WALLET_ADDRESS)}\n"
    text += f"─────────────────\n"
    text += f"💎 TON: {format_ton(ton)}\n"
    text += f"💵 USDT: {format_usdt(usdt)}\n"

    if tx_info: # kalau ada data transaksi
        text += f"─────────────────\n"
        text += f"{tx_info['emoji']} {tx_info['tipe']} {tx_info['amount']} {tx_info['coin']}\n"
        text += f"Alamat: {short_addr(tx_info['address'])}\n"
        text += f"Hash: `{tx_info['hash'][:8]}...{tx_info['hash'][-6:]}`\n"
        text += f"Waktu: {tx_info['time']}"

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

    tx_info = None
    changed = False

    # 1. CEK PERUBAHAN TON
    if ton!= last_tx["ton"]:
        changed = True
        tx = await client.get_last_ton_transaction()
        if tx and tx.get("transaction_id", {}).get("hash")!= last_tx["last_hash_ton"]:
            in_msg = tx.get("in_msg", {})
            out_msgs = tx.get("out_msgs", [])
            value = parse_integer(in_msg.get("value", 0))
            dest = in_msg.get("source", "")
            tipe, emoji = "MASUK", "📥"
            if out_msgs:
                value = parse_integer(out_msgs[0].get("value", 0))
                dest = out_msgs[0].get("destination", "")
                tipe, emoji = "KELUAR", "📤"
            tx_info = {"tipe": tipe, "emoji": emoji, "amount": format_ton(value), "coin": "TON", "address": dest, "hash": tx.get("transaction_id", {}).get("hash", ""), "time": time.strftime("%d-%m-%Y %H:%M", time.localtime(tx.get("utime", 0)))}
            last_tx["last_hash_ton"] = tx.get("transaction_id", {}).get("hash", "")

    # 2. CEK PERUBAHAN USDT
    elif usdt!= last_tx["usdt"]:
        changed = True
        tx = await client.get_last_usdt_transaction()
        if tx and tx.get("tx_hash")!= last_tx["last_hash_usdt"]:
            amount = parse_integer(tx.get("amount", 0))
            sender = tx.get("sender", {}).get("address", "")
            recipient = tx.get("recipient", {}).get("address", "")
            tipe, emoji, dest = "MASUK", "📥", sender
            if str(recipient) == WALLET_ADDRESS_RAW:
                tipe, emoji, dest = "MASUK", "📥", sender
            else: # kita yg kirim
                tipe, emoji, dest = "KELUAR", "📤", recipient

            tx_info = {"tipe": tipe, "emoji": emoji, "amount": format_usdt(amount), "coin": "USDT", "address": dest, "hash": tx.get("tx_hash", ""), "time": time.strftime("%d-%m-%Y %H:%M", time.localtime(tx.get("created_at", 0)))}
            last_tx["last_hash_usdt"] = tx.get("tx_hash", "")

    if changed:
        await send_report(context, ton, usdt, "PERUBAHAN", tx_info)
        app.bot_data["last_tx"] = {"ton": ton, "usdt": usdt, "last_hash_ton": last_tx["last_hash_ton"], "last_hash_usdt": last_tx["last_hash_usdt"]}
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
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("history", history_command))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    main()

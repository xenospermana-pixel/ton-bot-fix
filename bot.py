"""Telegram bot untuk memantau saldo dan transaksi TON serta USDT."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
)


WALLET_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"
# Format raw dipakai TON Center API v3 untuk filter jetton.
WALLET_ADDRESS_RAW = (
    "0:D298146D13EF36F31E4B9AC58DEEBF9A2297E36042730812363888B236E9F000"
)
USDT_MASTER_ADDRESS = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCWT59mym7NyJTNU"
USDT_MASTER_ADDRESS_RAW = (
    "0:B113A994B5024A16719F69139328EB759596C38A25F59028B146FECDC3621DFE"
)

TONCENTER_V2_API_URL = "https://toncenter.com/api/v2"
TONCENTER_V3_API_URL = "https://toncenter.com/api/v3"
NANO_TON = 9
USDT_DECIMALS = 6
CHECK_INTERVAL_SECONDS = 5 * 60
HOURLY_INTERVAL_SECONDS = 60 * 60
STATE_FILE = Path(os.getenv("TON_BOT_STATE_FILE", "ton_bot_state.json"))

logger = logging.getLogger(__name__)


def format_amount(base_units: int, decimals: int, places: int | None = None) -> str:
    """Format token base units menjadi angka yang mudah dibaca."""
    amount = Decimal(base_units) / (Decimal(10) ** decimals)
    if places is not None:
        return f"{amount:.{places}f}"
    formatted = f"{amount:.9f}".rstrip("0").rstrip(".")
    return formatted or "0"


def format_ton(nano_ton: int) -> str:
    """Pertahankan format fleksibel untuk notifikasi TON lama."""
    return format_amount(nano_ton, NANO_TON)


def format_usdt(micro_usdt: int) -> str:
    return format_amount(micro_usdt, USDT_DECIMALS, places=3)


def parse_integer(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def extract_address(value: Any) -> str:
    """Ambil alamat dari format string maupun object response TON."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(
            value.get("address")
            or value.get("account")
            or value.get("destination")
            or ""
        )
    return ""


def crc16_xmodem(data: bytes) -> int:
    """CRC16-XMODEM yang dipakai TON untuk friendly address."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def raw_to_friendly(address: str, bounceable: bool = True) -> str:
    """Konversi alamat raw TON (0:hash) menjadi alamat EQ/UQ."""
    if not address.startswith(("0:", "-1:")):
        return address
    try:
        workchain_text, hash_hex = address.split(":", 1)
        workchain = int(workchain_text)
        account_hash = bytes.fromhex(hash_hex)
        if len(account_hash) != 32:
            return address
        tag = 0x11 if bounceable else 0x51
        payload = bytes((tag, workchain & 0xFF)) + account_hash
        checksum = crc16_xmodem(payload).to_bytes(2, byteorder="big")
        return base64.urlsafe_b64encode(payload + checksum).decode("ascii").rstrip("=")
    except (TypeError, ValueError):
        return address


def short_address(address: str) -> str:
    """Tampilkan alamat ringkas agar notifikasi mudah dibaca."""
    friendly_address = raw_to_friendly(address)
    if not friendly_address:
        return "Alamat tidak tersedia"
    if len(friendly_address) <= 12:
        return friendly_address
    return f"{friendly_address[:6]}...{friendly_address[-4:]}"


def current_wib_time() -> str:
    return datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%H:%M")


@dataclass(frozen=True)
class TransferEvent:
    event_id: str
    asset: str
    direction: str
    amount_base_units: int
    decimals: int
    transaction_hash: str
    address: str = ""
    sort_key: int = 0


def extract_ton_transfer_events(
    transaction: dict[str, Any],
) -> list[TransferEvent]:
    """Ubah satu transaksi TON menjadi event saldo masuk/keluar."""
    transaction_id = transaction.get("transaction_id") or {}
    transaction_hash = str(transaction_id.get("hash") or "")
    if not transaction_hash:
        return []

    events: list[TransferEvent] = []
    incoming_message = transaction.get("in_msg") or {}
    incoming_amount = parse_integer(incoming_message.get("value"))
    if incoming_amount > 0:
        events.append(
            TransferEvent(
                # Format ID dipertahankan agar kompatibel dengan state TON lama.
                event_id=f"{transaction_hash}:in",
                asset="TON",
                direction="in",
                amount_base_units=incoming_amount,
                decimals=NANO_TON,
                transaction_hash=transaction_hash,
                address=extract_address(incoming_message.get("source")),
                sort_key=parse_integer(transaction.get("utime")),
            )
        )

    outgoing_messages = transaction.get("out_msgs") or []
    if isinstance(outgoing_messages, dict):
        outgoing_messages = [outgoing_messages]

    for index, message in enumerate(outgoing_messages):
        if not isinstance(message, dict):
            continue
        outgoing_amount = parse_integer(message.get("value"))
        if outgoing_amount <= 0:
            continue
        destination = extract_address(
            message.get("destination") or message.get("dest") or message.get("to")
        )
        events.append(
            TransferEvent(
                event_id=f"{transaction_hash}:out:{index}",
                asset="TON",
                direction="out",
                amount_base_units=outgoing_amount,
                decimals=NANO_TON,
                transaction_hash=transaction_hash,
                address=destination,
                sort_key=parse_integer(transaction.get("utime")),
            )
        )

    return events


def extract_usdt_transfer_events(
    transfers: list[dict[str, Any]],
) -> list[TransferEvent]:
    """Ubah response TON Center v3 menjadi event transfer USDT."""
    events: list[TransferEvent] = []
    for transfer in transfers:
        if transfer.get("transaction_aborted") is True:
            continue

        transaction_hash = str(
            transfer.get("transaction_hash")
            or transfer.get("trace_id")
            or transfer.get("transaction_lt")
            or ""
        )
        query_id = str(transfer.get("query_id") or "0")
        if not transaction_hash:
            continue

        source = extract_address(transfer.get("source"))
        destination = extract_address(transfer.get("destination"))
        amount = parse_integer(transfer.get("amount"))
        if amount <= 0:
            continue

        if source == WALLET_ADDRESS_RAW:
            direction = "out"
            address = raw_to_friendly(destination)
        elif destination == WALLET_ADDRESS_RAW:
            direction = "in"
            address = raw_to_friendly(source)
        else:
            continue

        events.append(
            TransferEvent(
                event_id=f"usdt:{transaction_hash}:{query_id}:{direction}",
                asset="USDT",
                direction=direction,
                amount_base_units=amount,
                decimals=USDT_DECIMALS,
                transaction_hash=transaction_hash,
                address=address,
                sort_key=parse_integer(transfer.get("transaction_now"))
                or parse_integer(transfer.get("transaction_lt")),
            )
        )
    return events


@dataclass
class BotState:
    """State subscriber dan transaksi untuk mencegah notifikasi dobel."""

    subscribers: set[int] = field(default_factory=set)
    processed_event_ids: set[str] = field(default_factory=set)
    processed_usdt_event_ids: set[str] = field(default_factory=set)
    initialized: bool = False
    usdt_initialized: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.subscribers = {
                int(chat_id) for chat_id in raw.get("subscribers", [])
            }
            self.processed_event_ids = {
                str(event_id) for event_id in raw.get("processed_event_ids", [])
            }
            self.processed_usdt_event_ids = {
                str(event_id)
                for event_id in raw.get("processed_usdt_event_ids", [])
            }
            self.initialized = bool(raw.get("initialized", False))
            self.usdt_initialized = bool(raw.get("usdt_initialized", False))
        except (OSError, ValueError, TypeError) as error:
            logger.warning("State tidak dapat dibaca, mulai dari kosong: %s", error)

    def save(self) -> None:
        payload = {
            "subscribers": sorted(self.subscribers),
            "processed_event_ids": sorted(self.processed_event_ids)[-2000:],
            "processed_usdt_event_ids": sorted(self.processed_usdt_event_ids)[-2000:],
            "initialized": self.initialized,
            "usdt_initialized": self.usdt_initialized,
        }
        temporary_file = STATE_FILE.with_suffix(f"{STATE_FILE.suffix}.tmp")
        temporary_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_file.replace(STATE_FILE)


class TonCenterClient:
    """Client requests untuk TON Center API v2 dan API v3."""

    def __init__(self, api_key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key})

    def close(self) -> None:
        self.session.close()

    def _get_json(self, base_url: str, path: str, **params: Any) -> Any:
        response = self.session.get(
            f"{base_url}{path}",
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("Response TON Center bukan JSON valid") from error

        if not isinstance(payload, dict):
            raise RuntimeError("Format response TON Center tidak valid")
        if payload.get("ok") is False:
            raise RuntimeError(
                str(payload.get("error") or "TON Center mengembalikan error")
            )
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload

    async def get_balance_nano_ton(self) -> int:
        payload = await asyncio.to_thread(
            self._get_json,
            TONCENTER_V2_API_URL,
            "/getAddressBalance",
            address=WALLET_ADDRESS,
        )
        return parse_integer(payload.get("result"))

    async def get_transactions(self) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(
            self._get_json,
            TONCENTER_V2_API_URL,
            "/getTransactions",
            address=WALLET_ADDRESS,
            limit=100,
            archival="true",
        )
        result = payload.get("result")
        if not isinstance(result, list):
            raise RuntimeError("Format transaksi TON dari TON Center tidak valid")
        return [item for item in result if isinstance(item, dict)]

    async def get_balance_micro_usdt(self) -> int:
        payload = await asyncio.to_thread(
            self._get_json,
            TONCENTER_V3_API_URL,
            "/jetton/wallets",
            owner_address=WALLET_ADDRESS,
            jetton_master=USDT_MASTER_ADDRESS_RAW,
            limit=100,
        )
        wallets = payload.get("jetton_wallets")
        if not isinstance(wallets, list):
            raise RuntimeError("Format saldo USDT dari TON Center tidak valid")

        for wallet in wallets:
            if not isinstance(wallet, dict):
                continue
            if str(wallet.get("jetton") or "") == USDT_MASTER_ADDRESS_RAW:
                return parse_integer(wallet.get("balance"))
        return 0

    async def get_usdt_transfers(self) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(
            self._get_json,
            TONCENTER_V3_API_URL,
            "/jetton/transfers",
            owner_address=WALLET_ADDRESS,
            jetton_master=USDT_MASTER_ADDRESS_RAW,
            limit=100,
        )
        transfers = payload.get("jetton_transfers")
        if not isinstance(transfers, list):
            raise RuntimeError("Format transfer USDT dari TON Center tidak valid")
        return [item for item in transfers if isinstance(item, dict)]


state = BotState()
state.load()


def get_ton_client(context: CallbackContext[Any, Any, Any, Any]) -> TonCenterClient:
    client = context.application.bot_data.get("ton_client")
    if not isinstance(client, TonCenterClient):
        raise RuntimeError("TON Center client belum siap")
    return client


async def send_to_subscribers(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    """Kirim pesan ke semua chat yang pernah menjalankan /start."""
    async with state.lock:
        chat_ids = list(state.subscribers)

    blocked_chat_ids: set[int] = set()
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Forbidden:
            logger.info("Chat %s memblokir bot, subscriber dihapus", chat_id)
            blocked_chat_ids.add(chat_id)
        except TelegramError:
            logger.exception("Gagal mengirim notifikasi ke chat %s", chat_id)

    if blocked_chat_ids:
        async with state.lock:
            state.subscribers.difference_update(blocked_chat_ids)
            state.save()


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    async with state.lock:
        state.subscribers.add(chat.id)
        state.save()

    await message.reply_text("Bot aktif")

    client = get_ton_client(context)
    ton_result, usdt_result = await asyncio.gather(
        client.get_balance_nano_ton(),
        client.get_balance_micro_usdt(),
        return_exceptions=True,
    )

    if isinstance(ton_result, Exception):
        logger.exception("Gagal mengambil saldo TON saat /start", exc_info=ton_result)
        ton_text = "N/A"
    else:
        ton_text = format_amount(ton_result, NANO_TON, places=3)

    if isinstance(usdt_result, Exception):
        logger.exception("Gagal mengambil saldo USDT saat /start", exc_info=usdt_result)
        usdt_text = "N/A"
    else:
        usdt_text = format_usdt(usdt_result)

    await message.reply_text(
        "💼 MONITOR WALLET\n"
        "────────────────\n"
        f"💎 Saldo TON: {ton_text}\n"
        f"💵 Saldo USDT: {usdt_text}\n"
        "────────────────\n"
        "Status: Aktif 24 Jam"
    )


def event_message(event: TransferEvent) -> str:
    amount = (
        format_usdt(event.amount_base_units)
        if event.asset == "USDT"
        else format_ton(event.amount_base_units)
    )
    if event.direction == "in":
        return (
            "✅ SALDO MASUK\n"
            f"+{amount} {event.asset}\n"
            f"Dari: {short_address(event.address)}"
        )

    return (
        "❌ SALDO KELUAR\n"
        f"-{amount} {event.asset}\n"
        f"Ke: {short_address(event.address)}"
    )


async def process_transfer_events(
    context: ContextTypes.DEFAULT_TYPE,
    events: list[TransferEvent],
    asset: str,
) -> None:
    """Baseline dan kirim hanya event baru untuk satu jenis aset."""
    events.sort(key=lambda event: (event.sort_key, event.event_id))

    async with state.lock:
        if asset == "USDT":
            processed_ids = state.processed_usdt_event_ids
            is_initialized = state.usdt_initialized
        else:
            processed_ids = state.processed_event_ids
            is_initialized = state.initialized

        if not is_initialized:
            processed_ids.update(event.event_id for event in events)
            if asset == "USDT":
                state.usdt_initialized = True
            else:
                state.initialized = True
            state.save()
            logger.info(
                "State %s diinisialisasi dari %s event terbaru",
                asset,
                len(events),
            )
            return

        new_events = [
            event for event in events if event.event_id not in processed_ids
        ]

    for event in new_events:
        await send_to_subscribers(context, event_message(event))
        async with state.lock:
            if asset == "USDT":
                state.processed_usdt_event_ids.add(event.event_id)
            else:
                state.processed_event_ids.add(event.event_id)
            state.save()


async def check_transactions_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cek transaksi TON dan USDT setiap 5 menit."""
    client = get_ton_client(context)

    try:
        transactions = await client.get_transactions()
        ton_events = [
            event
            for transaction in transactions
            for event in extract_ton_transfer_events(transaction)
        ]
        await process_transfer_events(context, ton_events, "TON")
    except (requests.RequestException, RuntimeError, ValueError) as error:
        logger.exception("Gagal mengecek transaksi TON: %s", error)

    try:
        transfers = await client.get_usdt_transfers()
        usdt_events = extract_usdt_transfer_events(transfers)
        await process_transfer_events(context, usdt_events, "USDT")
    except (requests.RequestException, RuntimeError, ValueError) as error:
        logger.exception("Gagal mengecek transaksi USDT: %s", error)


async def hourly_balance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kirim saldo TON dan USDT terbaru setiap satu jam."""
    client = get_ton_client(context)
    ton_result, usdt_result = await asyncio.gather(
        client.get_balance_nano_ton(),
        client.get_balance_micro_usdt(),
        return_exceptions=True,
    )

    if isinstance(ton_result, Exception) or isinstance(usdt_result, Exception):
        logger.error(
            "Gagal mengambil update saldo: TON=%s USDT=%s",
            type(ton_result).__name__ if isinstance(ton_result, Exception) else "ok",
            type(usdt_result).__name__
            if isinstance(usdt_result, Exception)
            else "ok",
        )
        return

    await send_to_subscribers(
        context,
        "📊 UPDATE SALDO\n"
        f"💎 Saldo TON: {format_amount(ton_result, NANO_TON, places=3)}\n"
        f"💵 Saldo USDT: {format_usdt(usdt_result)}\n"
        f"🕐 {current_wib_time()} WIB",
    )


async def post_init(application: Application) -> None:
    ton_api_key = os.getenv("TON_API_KEY", "").strip()
    if not ton_api_key:
        raise RuntimeError("Secret TON_API_KEY belum diatur")

    app.bot_data["ton_client"] = TonCenterClient(ton_api_key)
    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue tidak tersedia. Install python-telegram-bot[job-queue]==20.7."
        )

    app.job_queue.run_repeating(
        check_transactions_job,
        interval=CHECK_INTERVAL_SECONDS,
        first=10,
        name="ton-usdt-transaction-check",
    )
    app.job_queue.run_repeating(
        hourly_balance_job,
        interval=HOURLY_INTERVAL_SECONDS,
        first=HOURLY_INTERVAL_SECONDS,
        name="hourly-ton-usdt-balance-update",
    )
    logger.info("Bot TON + USDT aktif untuk wallet %s", WALLET_ADDRESS)


async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("ton_client")
    if isinstance(client, TonCenterClient):
        client.close()


def main() -> None:
    telegram_token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not telegram_token:
        raise RuntimeError("Secret TELEGRAM_TOKEN belum diatur")

        app = ApplicationBuilder().token(telegram_token).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", start_command))
    
    logger.info("Bot TON + USDT aktif untuk wallet %s", WALLET_ADDRESS)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    # URL request Telegram mengandung bot token; jangan tulis log HTTP tersebut.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    try:
        main()
    except (RuntimeError, InvalidOperation) as error:
        logger.error("%s", error)
        raise SystemExit(1) from error

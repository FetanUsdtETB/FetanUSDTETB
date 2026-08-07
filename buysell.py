#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetan USDT ETB - Ethiopian USDT Buy/Sell OTC Relay Bot
======================================================
Single-file, production-ready Telegram bot built on python-telegram-bot v20+
using async/await syntax and Motor (async MongoDB driver) against MongoDB Atlas.

Updated with:
  - Fetan USDT ETB Branding & Concise About Section (<512 chars)
  - Zero/Ultra-Low Fee Crypto Deposit Routes ($0.00 – $0.10): Binance Pay/UID,
    Bybit UID, Aptos (APT), Solana (SOL), Polygon (POL), Arbitrum One, TON, BEP-20.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import string
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Optional

from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID_RAW = os.getenv("ADMIN_CHAT_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is not set.")
if not ADMIN_CHAT_ID_RAW or not re.fullmatch(r"-?\d+", ADMIN_CHAT_ID_RAW):
    raise RuntimeError("Environment variable ADMIN_CHAT_ID must be a numeric Telegram chat id.")

ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "otc_ethiopia").strip()

if not MONGODB_URI:
    raise RuntimeError(
        "Environment variable MONGODB_URI is not set. "
        "Get it from Atlas -> Database -> Connect -> Drivers (Python)."
    )

DEFAULT_RATE = float(os.getenv("USDT_ETB_RATE", "145.0"))

# Admin receiving details for local currency (BUY orders)
ADMIN_PAYMENT_DETAILS = {
    "Telebirr": os.getenv("ADMIN_TELEBIRR", "0912345678 (Account Name: Fetan USDT ETB)"),
    "CBE / CBE Birr": os.getenv("ADMIN_CBE", "1000123456789 (Account Name: Fetan USDT ETB)"),
    "Other Local Bank": os.getenv(
        "ADMIN_BANK", "Awash Bank - 013200123456 (Account Name: Fetan USDT ETB)"
    ),
}

# Admin receiving crypto/UID addresses for SELL orders ($0.00 – $0.10 gas fee routes)
ADMIN_WALLET_ADDRESSES = {
    "Binance Pay / UID": os.getenv("ADMIN_BINANCE_UID", "123456789 (Name: Fetan_OTC)"),
    "Bybit UID": os.getenv("ADMIN_BYBIT_UID", "987654321"),
    "Aptos (APT)": os.getenv("ADMIN_WALLET_APTOS", "0xYourAptosAddressHere"),
    "Solana (SOL)": os.getenv("ADMIN_WALLET_SOLANA", "YourSolanaAddressHere"),
    "Polygon (POL)": os.getenv("ADMIN_WALLET_POLYGON", "0xYourPolygonAddressHere"),
    "Arbitrum One": os.getenv("ADMIN_WALLET_ARBITRUM", "0xYourArbitrumAddressHere"),
    "TON Network": os.getenv("ADMIN_WALLET_TON", "UQYourTonAddressHere"),
    "BEP-20 (BNB Chain)": os.getenv("ADMIN_WALLET_BEP20", "0xYourBEP20AddressHere"),
}

SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@Fetan_USDT_Support")
BRAND_NAME = os.getenv("BRAND_NAME", "Fetan USDT ETB (ፈጣን USDT ETB)")
WORKING_HOURS = os.getenv("WORKING_HOURS", "8:00 AM – 10:00 PM EAT")

KEEPALIVE_PORT = int(os.getenv("PORT", "8080"))

# --------------------------------------------------------------------------- #
#  Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("otc_bot")

# --------------------------------------------------------------------------- #
#  Conversation states
# --------------------------------------------------------------------------- #

class State(IntEnum):
    CHOOSING_AMOUNT_MODE = 1
    TYPING_AMOUNT = 2
    CHOOSING_PAYMENT = 3
    CHOOSING_NETWORK = 4
    TYPING_DETAILS = 5
    TYPING_ACCOUNT_NAME = 6
    CONFIRMING = 7


PAYMENT_METHODS = ["Telebirr", "CBE / CBE Birr", "Other Local Bank"]
NETWORKS = list(ADMIN_WALLET_ADDRESSES.keys())

STATUS_LABELS = {
    "PENDING": "🕓 Pending Review",
    "ACCEPTED": "✅ Accepted - Awaiting Payment",
    "REJECTED": "❌ Rejected",
    "COMPLETED": "🎉 Completed",
}

# --------------------------------------------------------------------------- #
#  Utility helpers
# --------------------------------------------------------------------------- #

def esc(text) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def sanitize_input(text: str, max_len: int = 256) -> str:
    if text is None:
        return ""
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")
    return cleaned.strip()[:max_len]


def fmt_usdt(value: float) -> str:
    return f"{value:,.2f} USDT"


def fmt_etb(value: float) -> str:
    return f"{value:,.2f} ETB"


def new_order_ref_suffix() -> str:
    return "".join(random.choices(string.digits, k=6))


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🟢 Buy USDT (USDT መግዛት)", callback_data="trade_BUY")],
            [InlineKeyboardButton("🔴 Sell USDT (USDT መሸጥ)", callback_data="trade_SELL")],
            [InlineKeyboardButton("📊 My Orders (የእኔ ትዕዛዞች)", callback_data="menu_myorders")],
            [InlineKeyboardButton("ℹ️ About Us (ስለ እኛ)", callback_data="about_us")],
            [InlineKeyboardButton("💬 Contact Support (ድጋፍ)", callback_data="menu_support")],
        ]
    )


def cancel_row() -> list:
    return [InlineKeyboardButton("✖️ Cancel (ይቅር)", callback_data="cancel_order")]


def amount_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Enter in USDT", callback_data="amtmode_USDT")],
            [InlineKeyboardButton("Enter in ETB (ብር)", callback_data="amtmode_ETB")],
            cancel_row(),
        ]
    )


def payment_method_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(pm, callback_data=f"pay_{i}")] for i, pm in enumerate(PAYMENT_METHODS)]
    rows.append(cancel_row())
    return InlineKeyboardMarkup(rows)


def network_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for i, nw in enumerate(NETWORKS):
        if "Binance" in nw or "Bybit" in nw:
            label = f"⚡ {nw} ($0.00)"
        elif "Aptos" in nw or "Solana" in nw:
            label = f"🟢 {nw} (<$0.01)"
        elif "Polygon" in nw or "Arbitrum" in nw:
            label = f"🟣 {nw} (~$0.02)"
        else:
            label = f"💎 {nw}"
        
        row.append(InlineKeyboardButton(label, callback_data=f"net_{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(cancel_row())
    return InlineKeyboardMarkup(rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Confirm & Submit (አረጋግጥ)", callback_data="confirm_submit")],
            cancel_row(),
        ]
    )


def admin_action_keyboard(order_id: str, status: str) -> InlineKeyboardMarkup:
    if status == "PENDING":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Accept Order", callback_data=f"adm_accept_{order_id}"),
                    InlineKeyboardButton("❌ Reject Order", callback_data=f"adm_reject_{order_id}"),
                ]
            ]
        )
    if status == "ACCEPTED":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏁 Complete Trade", callback_data=f"adm_complete_{order_id}")]]
        )
    return InlineKeyboardMarkup([])


# --------------------------------------------------------------------------- #
#  Database layer
# --------------------------------------------------------------------------- #

@dataclass
class Order:
    order_id: str
    user_id: int
    username: Optional[str]
    trade_type: str
    usdt_amount: float
    etb_amount: float
    payment_method: str
    network: str
    account_details: str
    account_name: Optional[str]
    status: str
    admin_msg_id: Optional[int]
    reject_reason: Optional[str]
    created_at: str
    updated_at: Optional[str]

    @classmethod
    def from_doc(cls, doc: dict) -> "Order":
        return cls(
            order_id=doc["order_id"],
            user_id=doc["user_id"],
            username=doc.get("username"),
            trade_type=doc["trade_type"],
            usdt_amount=doc["usdt_amount"],
            etb_amount=doc["etb_amount"],
            payment_method=doc["payment_method"],
            network=doc["network"],
            account_details=doc["account_details"],
            account_name=doc.get("account_name"),
            status=doc["status"],
            admin_msg_id=doc.get("admin_msg_id"),
            reject_reason=doc.get("reject_reason"),
            created_at=doc["created_at"],
            updated_at=doc.get("updated_at"),
        )


class Database:
    def __init__(self, uri: str, db_name: str):
        self.uri = uri
        self.db_name = db_name
        self._client: Optional[AsyncIOMotorClient] = None
        self._db = None
        self._orders = None
        self._settings = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(self.uri, serverSelectionTimeoutMS=8000)
        await self._client.admin.command("ping")

        self._db = self._client[self.db_name]
        self._orders = self._db["orders"]
        self._settings = self._db["settings"]

        await self._orders.create_index("order_id", unique=True)
        await self._orders.create_index("user_id")
        await self._orders.create_index("admin_msg_id")
        await self._orders.create_index("status")
        await self._orders.create_index([("user_id", 1), ("status", 1)])

        logger.info("Connected to MongoDB Atlas database '%s'.", self.db_name)

    async def close(self) -> None:
        if self._client:
            self._client.close()

    async def get_setting(self, key: str, default: str) -> str:
        doc = await self._settings.find_one({"_id": key})
        return doc["value"] if doc else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._settings.update_one(
            {"_id": key}, {"$set": {"value": value}}, upsert=True
        )

    async def get_rate(self) -> float:
        val = await self.get_setting("usdt_etb_rate", str(DEFAULT_RATE))
        try:
            return float(val)
        except ValueError:
            return DEFAULT_RATE

    async def generate_order_id(self) -> str:
        for _ in range(25):
            candidate = f"ORD-ET-{new_order_ref_suffix()}"
            exists = await self._orders.find_one({"order_id": candidate}, {"_id": 1})
            if not exists:
                return candidate
        raise RuntimeError("Could not generate a unique order id.")

    async def create_order(self, data: dict) -> str:
        order_id = await self.generate_order_id()
        now = datetime.utcnow().isoformat(timespec="seconds")
        doc = {
            "order_id": order_id,
            "user_id": data["user_id"],
            "username": data.get("username"),
            "trade_type": data["trade_type"],
            "usdt_amount": data["usdt_amount"],
            "etb_amount": data["etb_amount"],
            "payment_method": data["payment_method"],
            "network": data["network"],
            "account_details": data["account_details"],
            "account_name": data.get("account_name"),
            "status": "PENDING",
            "admin_msg_id": None,
            "reject_reason": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self._orders.insert_one(doc)
        except DuplicateKeyError:
            doc["order_id"] = await self.generate_order_id()
            order_id = doc["order_id"]
            await self._orders.insert_one(doc)
        return order_id

    async def set_admin_msg_id(self, order_id: str, msg_id: int) -> None:
        await self._orders.update_one(
            {"order_id": order_id}, {"$set": {"admin_msg_id": msg_id}}
        )

    async def update_status(
        self, order_id: str, status: str, reject_reason: Optional[str] = None
    ) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        update_fields = {"status": status, "updated_at": now}
        if reject_reason is not None:
            update_fields["reject_reason"] = reject_reason
        await self._orders.update_one({"order_id": order_id}, {"$set": update_fields})

    async def get_order(self, order_id: str) -> Optional[Order]:
        doc = await self._orders.find_one({"order_id": order_id})
        return Order.from_doc(doc) if doc else None

    async def get_order_by_admin_msg(self, admin_msg_id: int) -> Optional[Order]:
        doc = await self._orders.find_one({"admin_msg_id": admin_msg_id})
        return Order.from_doc(doc) if doc else None

    async def get_active_order_for_user(self, user_id: int) -> Optional[Order]:
        doc = await self._orders.find_one(
            {"user_id": user_id, "status": {"$in": ["PENDING", "ACCEPTED"]}},
            sort=[("_id", -1)],
        )
        return Order.from_doc(doc) if doc else None

    async def get_user_orders(self, user_id: int, limit: int = 10) -> list[Order]:
        cursor = self._orders.find({"user_id": user_id}).sort("_id", -1).limit(limit)
        return [Order.from_doc(doc) async for doc in cursor]


db = Database(MONGODB_URI, MONGODB_DB_NAME)
awaiting_reject_reason: dict[int, str] = {}

# --------------------------------------------------------------------------- #
#  Guard & Menu Handlers
# --------------------------------------------------------------------------- #

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        chat = update.effective_chat
        if chat is None or chat.id != ADMIN_CHAT_ID:
            if update.callback_query:
                await update.callback_query.answer("Not authorized.", show_alert=True)
            return
        return await func(update, context, *a, **kw)

    return wrapper


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    text = (
        f"👋 <b>Welcome to {esc(BRAND_NAME)}</b>\n"
        "እንኳን ወደ Fetan USDT ETB ገበያ በደህና መጡ!\n\n"
        "Trade USDT against Ethiopian Birr (ETB) safely via Telebirr, CBE, or bank transfer.\n"
        "Please choose an option below to get started 👇"
    )
    if update.message:
        await update.message.reply_html(text, reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await safe_edit(update.callback_query.message, text, main_menu_keyboard())


async def show_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    text = "🏠 <b>Main Menu</b>\nPlease choose an option below 👇"
    await safe_edit(query.message, text, main_menu_keyboard())


async def safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit_text failed, sending new message instead: %s", e)
            await message.reply_html(text, reply_markup=markup)

# --------------------------------------------------------------------------- #
#  Conversation Handlers
# --------------------------------------------------------------------------- #

async def trade_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    trade_type = query.data.split("_", 1)[1]
    context.user_data.clear()
    context.user_data["trade_type"] = trade_type

    label = "🟢 Buy USDT" if trade_type == "BUY" else "🔴 Sell USDT"
    text = (
        f"<b>{label}</b>\n\n"
        "Step 1/6: How would you like to enter the amount?\n"
        "የመጠን አይነት ይምረጡ (USDT ወይም ETB)"
    )
    await safe_edit(query.message, text, amount_mode_keyboard())
    return State.CHOOSING_AMOUNT_MODE


async def amount_mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    mode = query.data.split("_", 1)[1]
    context.user_data["amount_mode"] = mode

    unit = "USDT" if mode == "USDT" else "ETB (ብር)"
    text = (
        f"Step 2/6: Please type the amount in <b>{unit}</b>.\n"
        "Example: <code>100</code>\n\n"
        "Send /cancel at any time to abort."
    )
    await safe_edit(query.message, text, InlineKeyboardMarkup([cancel_row()]))
    return State.TYPING_AMOUNT


async def amount_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = sanitize_input(update.message.text, max_len=32)
    cleaned = raw.replace(",", "").replace("ETB", "").replace("USDT", "").strip()
    try:
        value = float(cleaned)
        if value <= 0 or value > 10_000_000:
            raise ValueError
    except ValueError:
        await update.message.reply_html(
            "⚠️ Invalid amount. Please type a positive number, e.g. <code>100</code>.\n"
            "ትክክለኛ ቁጥር ያስገቡ።"
        )
        return State.TYPING_AMOUNT

    rate = await db.get_rate()
    mode = context.user_data.get("amount_mode", "USDT")
    if mode == "USDT":
        usdt_amount = round(value, 2)
        etb_amount = round(value * rate, 2)
    else:
        etb_amount = round(value, 2)
        usdt_amount = round(value / rate, 2)

    context.user_data["usdt_amount"] = usdt_amount
    context.user_data["etb_amount"] = etb_amount

    text = (
        f"✅ Amount set: <b>{fmt_usdt(usdt_amount)}</b> ≈ <b>{fmt_etb(etb_amount)}</b>\n"
        f"(Rate used: 1 USDT = {rate:,.2f} ETB)\n\n"
        "Step 3/6: Select your local payment method.\n"
        "የክፍያ መንገድ ይምረጡ"
    )
    await update.message.reply_html(text, reply_markup=payment_method_keyboard())
    return State.CHOOSING_PAYMENT


async def payment_method_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_", 1)[1])
    method = PAYMENT_METHODS[idx]
    context.user_data["payment_method"] = method

    text = (
        f"✅ Payment method: <b>{esc(method)}</b>\n\n"
        "Step 4/6: Select deposit method / crypto network.\n"
        "💡 <i>Tip: Binance/Bybit UIDs, Aptos, and SOL cost under $0.01 in fees!</i>\n"
        "የቴክኖሎጂ አውታር (Network) ይምረጡ"
    )
    await safe_edit(query.message, text, network_keyboard())
    return State.CHOOSING_NETWORK


async def network_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    idx = int(query.data.split("_", 1)[1])
    network = NETWORKS[idx]
    context.user_data["network"] = network

    trade_type = context.user_data["trade_type"]
    if trade_type == "BUY":
        text = (
            f"✅ Network: <b>{esc(network)}</b>\n\n"
            "Step 5/6: Please type your <b>receiving address / UID</b> for "
            f"<b>{esc(network)}</b>.\n"
            "USDT የሚደርስበትን የዋሌት አድራሻ ወይም UID ያስገቡ"
        )
    else:
        text = (
            f"✅ Network: <b>{esc(network)}</b>\n\n"
            "Step 5/6: Please type your local receiving details "
            "(Telebirr number / bank account number).\n"
            "የቴሌብር ቁጥር ወይም የባንክ አካውንት ቁጥር ያስገቡ"
        )
    await safe_edit(query.message, text, InlineKeyboardMarkup([cancel_row()]))
    return State.TYPING_DETAILS


async def details_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    details = sanitize_input(update.message.text, max_len=160)
    if len(details) < 4:
        await update.message.reply_html(
            "⚠️ That looks too short. Please enter a valid wallet address, UID, or account number."
        )
        return State.TYPING_DETAILS

    context.user_data["account_details"] = details
    trade_type = context.user_data["trade_type"]

    if trade_type == "BUY":
        context.user_data["account_name"] = "N/A"
        await update.message.reply_html(build_confirmation_text(context.user_data))
        await update.message.reply_html(
            "Please review the order above 👆", reply_markup=confirm_keyboard()
        )
        return State.CONFIRMING

    text = (
        "Step 6/6: Please type the <b>full name</b> on the account "
        "(Telebirr/Bank Account Holder Name).\n"
        "የአካውንት ባለቤት ሙሉ ስም ያስገቡ"
    )
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup([cancel_row()]))
    return State.TYPING_ACCOUNT_NAME


async def account_name_typed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = sanitize_input(update.message.text, max_len=100)
    if len(name) < 2 or any(ch.isdigit() for ch in name):
        await update.message.reply_html(
            "⚠️ Please enter a valid full name (letters only, e.g. Abebe Kebede)."
        )
        return State.TYPING_ACCOUNT_NAME

    context.user_data["account_name"] = name
    await update.message.reply_html(build_confirmation_text(context.user_data))
    await update.message.reply_html(
        "Please review the order above 👆", reply_markup=confirm_keyboard()
    )
    return State.CONFIRMING


def build_confirmation_text(d: dict) -> str:
    trade_label = "🟢 BUY USDT" if d["trade_type"] == "BUY" else "🔴 SELL USDT"
    lines = [
        "🧾 <b>Order Confirmation Card</b>",
        f"Type: <b>{trade_label}</b>",
        f"Amount: <b>{fmt_usdt(d['usdt_amount'])}</b> ≈ <b>{fmt_etb(d['etb_amount'])}</b>",
        f"Payment Method: <b>{esc(d['payment_method'])}</b>",
        f"Network / Route: <b>{esc(d['network'])}</b>",
    ]
    if d["trade_type"] == "BUY":
        lines.append(f"Receiving Wallet / UID: <code>{esc(d['account_details'])}</code>")
    else:
        lines.append(f"Receiving Account: <code>{esc(d['account_details'])}</code>")
        lines.append(f"Account Holder Name: <b>{esc(d['account_name'])}</b>")
    lines.append("\nPress <b>Confirm &amp; Submit</b> to send this order to our OTC desk.")
    return "\n".join(lines)


async def confirm_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    d = context.user_data
    user = update.effective_user

    order_data = {
        "user_id": user.id,
        "username": user.username,
        "trade_type": d["trade_type"],
        "usdt_amount": d["usdt_amount"],
        "etb_amount": d["etb_amount"],
        "payment_method": d["payment_method"],
        "network": d["network"],
        "account_details": d["account_details"],
        "account_name": d.get("account_name", "N/A"),
    }

    try:
        order_id = await db.create_order(order_data)
    except Exception:
        logger.exception("Failed to create order in database")
        await safe_edit(
            query.message,
            "❌ Something went wrong while saving your order. Please try again.",
            main_menu_keyboard(),
        )
        return ConversationHandler.END

    await safe_edit(
        query.message,
        f"🎉 <b>Order Submitted!</b>\nYour Order ID: <code>{order_id}</code>\n\n"
        "Our team will review it shortly. You'll be notified here once it's accepted.",
        main_menu_keyboard(),
    )

    await send_admin_order_card(context, order_id)
    context.user_data.clear()
    return ConversationHandler.END


async def send_admin_order_card(context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = await db.get_order(order_id)
    if order is None:
        return

    trade_label = "🟢 BUY USDT" if order.trade_type == "BUY" else "🔴 SELL USDT"
    username_txt = f"@{esc(order.username)}" if order.username else "(no username)"

    lines = [
        "📥 <b>New OTC Order</b>",
        f"Order ID: <code>{order.order_id}</code>",
        f"Type: <b>{trade_label}</b>",
        f"Amount: <b>{fmt_usdt(order.usdt_amount)}</b> ≈ <b>{fmt_etb(order.etb_amount)}</b>",
        f"Payment Method: <b>{esc(order.payment_method)}</b>",
        f"Network / Route: <b>{esc(order.network)}</b>",
    ]
    if order.trade_type == "BUY":
        lines.append(f"User's Receiving Wallet/UID: <code>{esc(order.account_details)}</code>")
    else:
        lines.append(f"User's Receiving Account: <code>{esc(order.account_details)}</code>")
        lines.append(f"Account Holder Name: <b>{esc(order.account_name)}</b>")

    lines.append(f"\nUser: {username_txt} (id: <code>{order.user_id}</code>)")
    lines.append(f"Status: <b>{STATUS_LABELS.get(order.status, order.status)}</b>")

    text = "\n".join(lines)
    try:
        msg = await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_action_keyboard(order.order_id, order.status),
        )
        await db.set_admin_msg_id(order.order_id, msg.message_id)
    except TelegramError:
        logger.exception("Failed to deliver order card to admin chat")


async def cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await safe_edit(query.message, "❌ Order cancelled. Back to main menu.", main_menu_keyboard())
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_html(
        "❌ Cancelled. Back to main menu.", reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END

# --------------------------------------------------------------------------- #
#  About & Support Page
# --------------------------------------------------------------------------- #

async def show_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    about_text = (
        "ℹ️ <b>About Fetan USDT ETB | ስለ እኛ</b>\n"
        "───────────────────────\n"
        "Fast, safe, and reliable <b>USDT ↔ ETB</b> exchange bot in Ethiopia.\n"
        "ፈጣን፣ አስተማማኝ እና ደህንነቱ የተጠበቀ የ USDT እና ብር መመንዘሪያ ቦት።\n\n"
        "⚡ <b>Features / ጥቅሞች:</b>\n"
        "• Fast payouts via Telebirr, CBE & Local Banks\n"
        "• Binance/Bybit UIDs, Aptos, SOL, Polygon & low-fee networks\n"
        "• Admin-verified security | ግልፅ ተመን\n\n"
        f"⏱ <b>Hours:</b> {esc(WORKING_HOURS)}\n"
        "🛡 <b>Notice:</b> Admins NEVER DM you first.\n\n"
        f"📲 <b>Support:</b> {esc(SUPPORT_CONTACT)}"
    )
    
    back_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Menu (ተመለስ)", callback_data="back_to_menu")]]
    )
    await safe_edit(query.message, about_text, back_button)


async def my_orders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await render_my_orders(query.message, update.effective_user.id, edit=True)


async def my_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await render_my_orders(update.message, update.effective_user.id, edit=False)


async def render_my_orders(message: Message, user_id: int, edit: bool) -> None:
    orders = await db.get_user_orders(user_id)
    if not orders:
        text = "📊 <b>My Orders</b>\n\nYou have no orders yet. Use the menu to start one."
    else:
        lines = ["📊 <b>My Orders</b> (most recent first)\n"]
        for o in orders:
            trade_label = "🟢 BUY" if o.trade_type == "BUY" else "🔴 SELL"
            lines.append(
                f"<code>{o.order_id}</code> — {trade_label} — {fmt_usdt(o.usdt_amount)} — "
                f"{STATUS_LABELS.get(o.status, o.status)}"
            )
        text = "\n".join(lines)

    if edit:
        await safe_edit(message, text, main_menu_keyboard())
    else:
        await message.reply_html(text, reply_markup=main_menu_keyboard())


async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        "💬 <b>Contact Support / ድጋፍ</b>\n\n"
        f"Reach our team directly: {esc(SUPPORT_CONTACT)}\n\n"
        "Tip: If you have an active order, just send your message or receipt photo "
        "here in this chat — it will be forwarded to our OTC desk automatically."
    )
    await safe_edit(query.message, text, main_menu_keyboard())

# --------------------------------------------------------------------------- #
#  Admin actions: Accept / Reject / Complete
# --------------------------------------------------------------------------- #

@admin_only
async def admin_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.split("adm_accept_", 1)[1]
    order = await db.get_order(order_id)
    if order is None or order.status != "PENDING":
        await query.answer("Order not found or already processed.", show_alert=True)
        return

    await db.update_status(order_id, "ACCEPTED")
    await query.answer("Order accepted.")
    await refresh_admin_card(context, order_id)

    if order.trade_type == "BUY":
        pay_detail = ADMIN_PAYMENT_DETAILS.get(order.payment_method, "Contact support for details.")
        text = (
            f"✅ <b>Your order {order.order_id} has been accepted!</b>\n\n"
            f"Please pay <b>{fmt_etb(order.etb_amount)}</b> via <b>{esc(order.payment_method)}</b> to:\n"
            f"<code>{esc(pay_detail)}</code>\n\n"
            "Once paid, reply here with a screenshot of your payment receipt."
        )
    else:
        wallet = ADMIN_WALLET_ADDRESSES.get(order.network, "Contact support for details.")
        text = (
            f"✅ <b>Your order {order.order_id} has been accepted!</b>\n\n"
            f"Please send <b>{fmt_usdt(order.usdt_amount)}</b> via <b>{esc(order.network)}</b> to:\n"
            f"<code>{esc(wallet)}</code>\n\n"
            "Once sent, reply here with a screenshot/transaction hash as proof."
        )
    await notify_user(context, order.user_id, text)


@admin_only
async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.split("adm_reject_", 1)[1]
    order = await db.get_order(order_id)
    if order is None or order.status != "PENDING":
        await query.answer("Order not found or already processed.", show_alert=True)
        return

    awaiting_reject_reason[ADMIN_CHAT_ID] = order_id
    await query.answer()
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"✍️ Please type the rejection reason for <code>{order_id}</code>:",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def admin_complete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.split("adm_complete_", 1)[1]
    order = await db.get_order(order_id)
    if order is None or order.status != "ACCEPTED":
        await query.answer("Order cannot be completed.", show_alert=True)
        return

    await db.update_status(order_id, "COMPLETED")
    await query.answer("Order marked completed.")
    await refresh_admin_card(context, order_id)

    if order.trade_type == "BUY":
        text = (
            f"🎉 <b>Order {order.order_id} completed!</b>\n"
            f"{fmt_usdt(order.usdt_amount)} has been dispatched to your wallet/UID. "
            "Thank you for trading with us!"
        )
    else:
        text = (
            f"🎉 <b>Order {order.order_id} completed!</b>\n"
            f"{fmt_etb(order.etb_amount)} has been sent to your account. "
            "Thank you for trading with us!"
        )
    await notify_user(context, order.user_id, text)


async def refresh_admin_card(context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = await db.get_order(order_id)
    if order is None or order.admin_msg_id is None:
        return

    trade_label = "🟢 BUY USDT" if order.trade_type == "BUY" else "🔴 SELL USDT"
    username_txt = f"@{esc(order.username)}" if order.username else "(no username)"
    lines = [
        "📥 <b>OTC Order</b>",
        f"Order ID: <code>{order.order_id}</code>",
        f"Type: <b>{trade_label}</b>",
        f"Amount: <b>{fmt_usdt(order.usdt_amount)}</b> ≈ <b>{fmt_etb(order.etb_amount)}</b>",
        f"Payment Method: <b>{esc(order.payment_method)}</b>",
        f"Network / Route: <b>{esc(order.network)}</b>",
    ]
    if order.trade_type == "BUY":
        lines.append(f"User's Receiving Wallet/UID: <code>{esc(order.account_details)}</code>")
    else:
        lines.append(f"User's Receiving Account: <code>{esc(order.account_details)}</code>")
        lines.append(f"Account Holder Name: <b>{esc(order.account_name)}</b>")
    lines.append(f"\nUser: {username_txt} (id: <code>{order.user_id}</code>)")
    lines.append(f"Status: <b>{STATUS_LABELS.get(order.status, order.status)}</b>")
    if order.status == "REJECTED" and order.reject_reason:
        lines.append(f"Reason: <i>{esc(order.reject_reason)}</i>")

    text = "\n".join(lines)
    try:
        await context.bot.edit_message_text(
            chat_id=ADMIN_CHAT_ID,
            message_id=order.admin_msg_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_action_keyboard(order.order_id, order.status),
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("Failed to refresh admin card for %s: %s", order_id, e)


async def notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
    except Forbidden:
        logger.warning("User %s blocked the bot.", user_id)
    except TelegramError:
        logger.exception("Failed to notify user %s", user_id)


async def admin_reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order_id = awaiting_reject_reason.get(ADMIN_CHAT_ID)
    if not order_id:
        return
    reason = sanitize_input(update.message.text or "No reason provided.", max_len=300)
    order = await db.get_order(order_id)
    if order is None or order.status != "PENDING":
        awaiting_reject_reason.pop(ADMIN_CHAT_ID, None)
        return

    await db.update_status(order_id, "REJECTED", reject_reason=reason)
    awaiting_reject_reason.pop(ADMIN_CHAT_ID, None)
    await refresh_admin_card(context, order_id)
    await update.message.reply_html(f"❌ Order <code>{order_id}</code> rejected.")

    await notify_user(
        context,
        order.user_id,
        f"❌ <b>Your order {order.order_id} was rejected.</b>\nReason: {esc(reason)}\n\n"
        "You may submit a new order anytime with /start.",
    )
    raise ApplicationHandlerStop

# --------------------------------------------------------------------------- #
#  Two-way relay handlers
# --------------------------------------------------------------------------- #

@admin_only
async def admin_reply_relay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg.reply_to_message is None:
        return

    order = await db.get_order_by_admin_msg(msg.reply_to_message.message_id)
    if order is None:
        return

    prefix = f"📩 <b>Message from OTC Desk</b> (Order <code>{order.order_id}</code>):\n"
    try:
        if msg.photo:
            caption = prefix + esc(msg.caption or "")
            await context.bot.send_photo(
                chat_id=order.user_id,
                photo=msg.photo[-1].file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        elif msg.document:
            caption = prefix + esc(msg.caption or "")
            await context.bot.send_document(
                chat_id=order.user_id,
                document=msg.document.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        elif msg.text:
            await context.bot.send_message(
                chat_id=order.user_id,
                text=prefix + esc(msg.text),
                parse_mode=ParseMode.HTML,
            )
        await msg.reply_text("✅ Relayed to user.")
    except Forbidden:
        await msg.reply_text("⚠️ Could not deliver — user blocked the bot.")
    except TelegramError:
        logger.exception("Failed to relay admin reply")
    raise ApplicationHandlerStop


async def user_message_relay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private" or update.effective_chat.id == ADMIN_CHAT_ID:
        return

    user = update.effective_user
    msg = update.message
    order = await db.get_active_order_for_user(user.id)
    if order is None:
        await msg.reply_html(
            "ℹ️ You have no active order. Use /start to open the menu.",
            reply_markup=main_menu_keyboard(),
        )
        return

    username_txt = f"@{esc(user.username)}" if user.username else "(no username)"
    prefix = (
        f"📨 <b>Message from user</b> {username_txt} (id: <code>{user.id}</code>)\n"
        f"Order: <code>{order.order_id}</code>\n\n"
    )
    try:
        if msg.photo:
            caption = prefix + esc(msg.caption or "Payment receipt / screenshot")
            await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=msg.photo[-1].file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=order.admin_msg_id,
            )
        elif msg.document:
            caption = prefix + esc(msg.caption or "")
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=msg.document.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=order.admin_msg_id,
            )
        elif msg.text:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=prefix + esc(msg.text),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=order.admin_msg_id,
            )
        else:
            return
        await msg.reply_html("✅ Your message was forwarded to our OTC desk.")
    except TelegramError:
        logger.exception("Failed to relay user message for order %s", order.order_id)

# --------------------------------------------------------------------------- #
#  Admin utility commands
# --------------------------------------------------------------------------- #

@admin_only
async def set_rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        rate = await db.get_rate()
        await update.message.reply_html(f"Current rate: <b>1 USDT = {rate:,.2f} ETB</b>")
        return
    try:
        new_rate = float(args[0])
        if new_rate <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_html("⚠️ Usage: /setrate 145.5")
        return
    await db.set_setting("usdt_etb_rate", str(new_rate))
    await update.message.reply_html(f"✅ Rate updated: <b>1 USDT = {new_rate:,.2f} ETB</b>")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", update, exc_info=context.error)

# --------------------------------------------------------------------------- #
#  Keepalive HTTP Server & Application Bootstrap
# --------------------------------------------------------------------------- #

async def _keepalive_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK - Fetan USDT ETB OTC bot is awake.")


async def start_keepalive_server(application: Application) -> None:
    app = web.Application()
    app.add_routes([web.get("/", _keepalive_handler), web.get("/health", _keepalive_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", KEEPALIVE_PORT)
    await site.start()
    application.bot_data["keepalive_runner"] = runner


async def stop_keepalive_server(application: Application) -> None:
    runner: Optional[web.AppRunner] = application.bot_data.get("keepalive_runner")
    if runner is not None:
        await runner.cleanup()


async def post_init(application: Application) -> None:
    await db.connect()
    await start_keepalive_server(application)


async def post_shutdown(application: Application) -> None:
    await stop_keepalive_server(application)
    await db.close()


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    order_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(trade_type_selected, pattern=r"^trade_(BUY|SELL)$"),
        ],
        states={
            State.CHOOSING_AMOUNT_MODE: [
                CallbackQueryHandler(amount_mode_selected, pattern=r"^amtmode_(USDT|ETB)$"),
            ],
            State.TYPING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_typed),
            ],
            State.CHOOSING_PAYMENT: [
                CallbackQueryHandler(payment_method_selected, pattern=r"^pay_\d+$"),
            ],
            State.CHOOSING_NETWORK: [
                CallbackQueryHandler(network_selected, pattern=r"^net_\d+$"),
            ],
            State.TYPING_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, details_typed),
            ],
            State.TYPING_ACCOUNT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, account_name_typed),
            ],
            State.CONFIRMING: [
                CallbackQueryHandler(confirm_submit, pattern=r"^confirm_submit$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_order_callback, pattern=r"^cancel_order$"),
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
        ],
        name="otc_order_conversation",
        persistent=False,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("myorders", my_orders_command))
    application.add_handler(CommandHandler("setrate", set_rate_command))
    application.add_handler(order_conv)
    application.add_handler(
        CallbackQueryHandler(show_main_menu_callback, pattern=r"^(menu_main|back_to_menu)$")
    )
    application.add_handler(CallbackQueryHandler(my_orders_callback, pattern=r"^menu_myorders$"))
    application.add_handler(CallbackQueryHandler(show_about, pattern=r"^about_us$"))
    application.add_handler(CallbackQueryHandler(support_callback, pattern=r"^menu_support$"))

    application.add_handler(CallbackQueryHandler(admin_accept, pattern=r"^adm_accept_ORD-ET-\d+$"))
    application.add_handler(CallbackQueryHandler(admin_reject, pattern=r"^adm_reject_ORD-ET-\d+$"))
    application.add_handler(CallbackQueryHandler(admin_complete, pattern=r"^adm_complete_ORD-ET-\d+$"))

    application.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_CHAT_ID) & filters.REPLY & (filters.TEXT | filters.PHOTO | filters.Document.ALL),
            admin_reply_relay,
        ),
        group=0,
    )
    application.add_handler(
        MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.TEXT & ~filters.COMMAND, admin_reason_text),
        group=0,
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            user_message_relay,
        ),
        group=1,
    )

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    application = build_application()
    logger.info("Starting Fetan USDT ETB OTC Relay Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetan USDT ETB - Ethiopian USDT Buy/Sell OTC Relay Bot
======================================================
Single-file, production-ready Telegram bot built on python-telegram-bot v20+
using async/await syntax and Motor (async MongoDB driver) against MongoDB Atlas.

Features:
  - Fixed syntax error in Database layer
  - Corrected Rates: Customer BUY = 199 ETB, Customer SELL = 180 ETB
  - Clean submission screen without cluttering main menu options
  - Vertical 4x1 Deposit Routes: Binance UID, Bybit UID, BEP-20, Aptos
  - Accounts: Telebirr (0998947429) & CBE (1000200873) - Elilo Arja
  - Dispute handling & automated two-way relay
  - Support Handle: @FetanUSDTETB_SUPPORT
  - Automated Daily Channel Post (Dynamically pulls current rates)
"""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
import os
import random
import re
import string
from dataclasses import dataclass
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

# Channel ID for daily automated posts (e.g., @MyChannel or -100123456)
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "otc_ethiopia").strip()

if not MONGODB_URI:
    raise RuntimeError(
        "Environment variable MONGODB_URI is not set. "
        "Get it from Atlas -> Database -> Connect -> Drivers (Python)."
    )

# Configured Rates based on trade context
DEFAULT_BUY_RATE = float(os.getenv("USDT_BUY_RATE", "199.0"))   # Customer BUYS USDT at 199 ETB
DEFAULT_SELL_RATE = float(os.getenv("USDT_SELL_RATE", "180.0")) # Customer SELLS USDT at 180 ETB

ADMIN_PAYMENT_DETAILS = {
    "Telebirr": os.getenv("ADMIN_TELEBIRR", "0998947429 (Account Name: Elilo Arja)"),
    "CBE ": os.getenv("ADMIN_CBE", "1000200873673 (Account Name: Elilo Arja)"),
}

ADMIN_WALLET_ADDRESSES = {
    "Binance UID": os.getenv("ADMIN_BINANCE_UID", "YourBinanceUIDHere (Name: FetanUSDTETB)"),
    "Bybit UID": os.getenv("ADMIN_BYBIT_UID", "YourBybitUIDHere (Name: FetanUSDTETB)"),
    "BEP-20 (BNB Chain)": os.getenv("ADMIN_WALLET_BEP20", "0xYourBEP20AddressHere"),
    "Aptos (APT)": os.getenv("ADMIN_WALLET_APTOS", "0xYourAptosAddressHere"),
}

SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@FetanUSDTETB_SUPPORT")
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
#  Conversation States & Statuses
# --------------------------------------------------------------------------- #

class State(IntEnum):
    CHOOSING_AMOUNT_MODE = 1
    TYPING_AMOUNT = 2
    CHOOSING_PAYMENT = 3
    CHOOSING_NETWORK = 4
    TYPING_DETAILS = 5
    TYPING_ACCOUNT_NAME = 6
    CONFIRMING = 7


PAYMENT_METHODS = ["Telebirr", "CBE / CBE Birr"]
NETWORKS = list(ADMIN_WALLET_ADDRESSES.keys())

STATUS_LABELS = {
    "PENDING": "🕓 Pending Review",
    "ACCEPTED": "✅ Accepted - Awaiting Payment",
    "ACTION_REQUIRED": "⚠️ Action Required (Invalid/Missing Proof)",
    "REJECTED": "❌ Rejected",
    "COMPLETED": "🎉 Completed",
}

# --------------------------------------------------------------------------- #
#  Utility Helpers
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
    for i, nw in enumerate(NETWORKS):
        if "Binance" in nw or "Bybit" in nw:
            label = f"⚡ {nw} ($0.00)"
        elif "Aptos" in nw:
            label = f"🟢 {nw} (<$0.01)"
        else:
            label = f"💎 {nw}"
        rows.append([InlineKeyboardButton(label, callback_data=f"net_{i}")])
        
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
    if status in ("PENDING", "ACCEPTED", "ACTION_REQUIRED"):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Accept", callback_data=f"adm_accept_{order_id}"),
                    InlineKeyboardButton("🏁 Complete", callback_data=f"adm_complete_{order_id}"),
                ],
                [
                    InlineKeyboardButton("⚠️ Request New Proof", callback_data=f"adm_fix_{order_id}"),
                    InlineKeyboardButton("❌ Reject Order", callback_data=f"adm_reject_{order_id}"),
                ],
            ]
        )
    return InlineKeyboardMarkup([])

# --------------------------------------------------------------------------- #
#  Database Layer
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

    async def get_rates(self) -> tuple[float, float]:
        buy_val = await self.get_setting("usdt_buy_rate", str(DEFAULT_BUY_RATE))
        sell_val = await self.get_setting("usdt_sell_rate", str(DEFAULT_SELL_RATE))
        try:
            buy_rate = float(buy_val)
        except ValueError:
            buy_rate = DEFAULT_BUY_RATE
        try:
            sell_rate = float(sell_val)
        except ValueError:
            sell_rate = DEFAULT_SELL_RATE
        return buy_rate, sell_rate

    async def generate_order_id(self) -> str:
        for _ in range(25):
            candidate = f"ORD-ET-{new_order_ref_suffix()}"
            exists = await self._orders.find_one({"order_id": candidate}, {"_id": 1})
            if not exists:
                return candidate
        raise RuntimeError("Could not generate a unique order id.")

    async def create_order(self, data: dict) -> str:
        order_id = await self.generate_order_id()
        # FIX: using datetime.datetime.utcnow() to prevent AttributeError
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
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
        # FIX: using datetime.datetime.utcnow()
        now = datetime.datetime.utcnow().isoformat(timespec="seconds")
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
            {"user_id": user_id, "status": {"$in": ["PENDING", "ACCEPTED", "ACTION_REQUIRED"]}},
            sort=[("_id", -1)],
        )
        return Order.from_doc(doc) if doc else None

    async def get_user_orders(self, user_id: int, limit: int = 10) -> list[Order]:
        cursor = self._orders.find({"user_id": user_id}).sort("_id", -1).limit(limit)
        return [Order.from_doc(doc) async for doc in cursor]


db = Database(MONGODB_URI, MONGODB_DB_NAME)
awaiting_admin_input: dict[int, dict] = {}

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
        "Trade USDT against Ethiopian Birr (ETB) safely via Telebirr or CBE.\n"
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
        if message.photo:
            await message.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("safe_edit failed, sending new message instead: %s", e)
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

    buy_rate, sell_rate = await db.get_rates()
    trade_type = context.user_data.get("trade_type", "BUY")
    rate = buy_rate if trade_type == "BUY" else sell_rate

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
        "💡 <i>Tip: Binance and Bybit UIDs have zero transfer fees!</i>\n"
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
            "(Telebirr number / CBE account number).\n"
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
    except Exception as e:
        logger.exception(f"Failed to create order in database: {e}")
        await safe_edit(
            query.message,
            "❌ Something went wrong while saving your order. Please try again.",
            main_menu_keyboard(),
        )
        return ConversationHandler.END

    back_to_menu_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Menu (ተመለስ)", callback_data="back_to_menu")]]
    )

    await safe_edit(
        query.message,
        f"🎉 <b>Order Submitted!</b>\n"
        f"Order ID: <code>{order_id}</code>\n\n"
        "Our team will review it shortly. You'll be notified here once it's accepted.",
        back_to_menu_keyboard,
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
        "• Fast payouts via Telebirr & CBE\n"
        "• Binance/Bybit UIDs, BEP-20 & Aptos low-fee networks\n"
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
#  Admin Actions: Accept / Reject / Fix (Request New Proof) / Complete
# --------------------------------------------------------------------------- #

@admin_only
async def admin_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.split("adm_accept_", 1)[1]
    order = await db.get_order(order_id)
    if order is None:
        await query.answer("Order not found.", show_alert=True)
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
async def admin_request_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.split("adm_fix_", 1)[1]
    order = await db.get_order(order_id)
    if order is None:
        await query.answer("Order not found.", show_alert=True)
        return

    awaiting_admin_input[ADMIN_CHAT_ID] = {"action": "fix", "order_id": order_id}
    await query.answer()
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"⚠️ <b>Request New Proof for {order_id}</b>\n"
             "Please type the issue (e.g., <i>Unreadable receipt, wrong amount sent, wrong account holder name</i>):",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.split("adm_reject_", 1)[1]
    order = await db.get_order(order_id)
    if order is None:
        await query.answer("Order not found.", show_alert=True)
        return

    awaiting_admin_input[ADMIN_CHAT_ID] = {"action": "reject", "order_id": order_id}
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
    if order is None:
        await query.answer("Order not found.", show_alert=True)
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
        "📥 <b>OTC Order Desk</b>",
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
    if order.reject_reason:
        lines.append(f"Note / Reason: <i>{esc(order.reject_reason)}</i>")

    text = "\n".join(lines)
    try:
        await context.bot.edit_message_caption(
            chat_id=ADMIN_CHAT_ID,
            message_id=order.admin_msg_id,
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_action_keyboard(order.order_id, order.status),
        )
    except BadRequest:
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


async def admin_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    task = awaiting_admin_input.get(ADMIN_CHAT_ID)
    if not task:
        return

    action = task.get("action")
    order_id = task.get("order_id")
    order = await db.get_order(order_id)
    if not order:
        awaiting_admin_input.pop(ADMIN_CHAT_ID, None)
        return

    reason = sanitize_input(update.message.text or "No specific details provided.", max_len=300)

    if action == "reject":
        await db.update_status(order_id, "REJECTED", reject_reason=reason)
        awaiting_admin_input.pop(ADMIN_CHAT_ID, None)
        await refresh_admin_card(context, order_id)
        await update.message.reply_html(f"❌ Order <code>{order_id}</code> rejected.")
        await notify_user(
            context,
            order.user_id,
            f"❌ <b>Your order {order.order_id} was rejected.</b>\nReason: {esc(reason)}\n\n"
            "You may submit a new order anytime with /start.",
        )

    elif action == "fix":
        await db.update_status(order_id, "ACTION_REQUIRED", reject_reason=reason)
        awaiting_admin_input.pop(ADMIN_CHAT_ID, None)
        await refresh_admin_card(context, order_id)
        await update.message.reply_html(f"⚠️ Action required notice sent to user for <code>{order_id}</code>.")
        await notify_user(
            context,
            order.user_id,
            f"⚠️ <b>Action Required for Order {order.order_id}</b>\n"
            f"Issue: <i>{esc(reason)}</i>\n\n"
            "Please send a new screenshot or reply with the correct transfer details directly in this chat.",
        )

    raise ApplicationHandlerStop

# --------------------------------------------------------------------------- #
#  Two-Way Relay Handlers
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

    if context.user_data and ("trade_type" in context.user_data or "amount_mode" in context.user_data):
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
    trade_label = "🟢 BUY USDT" if order.trade_type == "BUY" else "🔴 SELL USDT"

    caption = (
        f"📸 <b>NEW PAYMENT PROOF RECEIVED</b>\n"
        f"Order ID: <code>{order.order_id}</code>\n"
        f"Type: <b>{trade_label}</b> | Amount: <b>{fmt_usdt(order.usdt_amount)}</b>\n"
        f"User: {username_txt} (id: <code>{user.id}</code>)\n"
        f"Status: <b>{STATUS_LABELS.get(order.status, order.status)}</b>\n\n"
        f"👇 <i>Use the action buttons below to process this trade.</i>"
    )

    try:
        if msg.photo:
            admin_msg = await context.bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=msg.photo[-1].file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_action_keyboard(order.order_id, order.status),
            )
            await db.set_admin_msg_id(order.order_id, admin_msg.message_id)

        elif msg.document:
            admin_msg = await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=msg.document.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_action_keyboard(order.order_id, order.status),
            )
            await db.set_admin_msg_id(order.order_id, admin_msg.message_id)

        elif msg.text:
            text_payload = (
                f"📨 <b>New User Reply</b> from {username_txt} (id: <code>{user.id}</code>)\n"
                f"Order: <code>{order.order_id}</code>\n\n"
                f"{esc(msg.text)}"
            )
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=text_payload,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=order.admin_msg_id,
            )

        await msg.reply_html("✅ Your message/receipt was forwarded to our OTC desk.")
    except TelegramError:
        logger.exception("Failed to relay user message for order %s", order.order_id)

# --------------------------------------------------------------------------- #
#  Admin Utility Commands
# --------------------------------------------------------------------------- #

@admin_only
async def set_rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or len(args) < 2:
        buy_rate, sell_rate = await db.get_rates()
        await update.message.reply_html(
            f"Current Rates:\n"
            f"• 🟢 Buying Rate (Customer Buys): <b>1 USDT = {buy_rate:,.2f} ETB</b>\n"
            f"• 🔴 Selling Rate (Customer Sells): <b>1 USDT = {sell_rate:,.2f} ETB</b>\n\n"
            f"Usage: <code>/setrate 199 180</code> (Buy_Rate Sell_Rate)"
        )
        return
    try:
        new_buy = float(args[0])
        new_sell = float(args[1])
        if new_buy <= 0 or new_sell <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_html("⚠️ Usage: <code>/setrate 199 180</code>")
        return
    await db.set_setting("usdt_buy_rate", str(new_buy))
    await db.set_setting("usdt_sell_rate", str(new_sell))
    await update.message.reply_html(
        f"✅ Rates updated:\n"
        f"• Buying Rate (Customer Buys): <b>1 USDT = {new_buy:,.2f} ETB</b>\n"
        f"• Selling Rate (Customer Sells): <b>1 USDT = {new_sell:,.2f} ETB</b>"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", update, exc_info=context.error)

# --------------------------------------------------------------------------- #
#  Automated Daily Channel Post Task
# --------------------------------------------------------------------------- #

async def post_daily_promo(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled task to post the current exchange rates to a Telegram channel."""
    if not CHANNEL_ID:
        return

    buy_rate, sell_rate = await db.get_rates()
    bot_username = context.bot.username or "FetanUSDTETB_bot"

    text = f"""🎉 <b>Welcome to {esc(BRAND_NAME)}! | እንኳን ወደ {esc(BRAND_NAME)} በደህና መጡ!</b> 🎉

Ethiopia’s fastest, safest, and most reliable automated OTC desk for buying and selling USDT is officially live! 
አስተማማኝ እና ፈጣን የሆነው የUSDT መገበያያ ቦት አገልግሎት መስጠት ጀምሯል። 

💱 <b>Current Exchange Rates / የዕለቱ ተመን:</b>
🟢 Buy USDT (USDT ለመግዛት): 1 USDT = <b>{buy_rate:,.2f} ETB</b>
🔴 Sell USDT (USDT ለመሸጥ): 1 USDT = <b>{sell_rate:,.2f} ETB</b>

⚡️ <b>Why Choose Us? / ለምን እኛን ይመርጣሉ?</b>
• <b>Local Payments:</b> Fast fiat transfers via Telebirr & CBE. 
• <b>Zero/Low Fees:</b> Enjoy ZERO crypto fees using Binance UID & Bybit UID, or ultra-low fees on BEP-20 and Aptos networks!
• <b>Secure & Automated:</b> Admin-verified trades ensure your funds are 100% safe. 

🚀 <b>How to start? / እንዴት መጀመር ይቻላል?</b>
Click the link below to open our bot and start trading instantly!

👉 <b>Start Trading Now (ወደ ቦት ለመግባት):</b> @{bot_username}

💬 <b>Need Help? (ለጥያቄ እና ድጋፍ):</b> {esc(SUPPORT_CONTACT)}

<i>(⚠️ Security Notice: Our admins will NEVER message you first. Always use the official bot and support links.)</i>"""

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        logger.info("Daily promo posted to channel %s", CHANNEL_ID)
    except TelegramError as e:
        logger.error("Failed to post daily promo to channel: %s", e)


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

    # Schedule the daily channel post at 7:00 AM EAT (which is 04:00 UTC)
    if CHANNEL_ID:
        # Note: python-telegram-bot job_queue runs on UTC time by default
        run_time = datetime.time(hour=4, minute=00, tzinfo=datetime.timezone.utc)
        application.job_queue.run_daily(post_daily_promo, run_time)
        logger.info(f"Daily promo task scheduled for channel {CHANNEL_ID} at 7:00 AM EAT.")

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

    # Admin Handlers
    application.add_handler(CallbackQueryHandler(admin_accept, pattern=r"^adm_accept_ORD-ET-\d+$"))
    application.add_handler(CallbackQueryHandler(admin_request_fix, pattern=r"^adm_fix_ORD-ET-\d+$"))
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
        MessageHandler(filters.Chat(ADMIN_CHAT_ID) & filters.TEXT & ~filters.COMMAND, admin_input_text),
        group=0,
    )

    # User Relay
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

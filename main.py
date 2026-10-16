#!/usr/bin/env python3
"""NextGen Telegram voting giveaway bot.

Install once: python -m pip install 'aiogram>=3.25,<4' pymongo dnspython aiohttp
Set BOT_TOKEN below, then run: python bot.py
Persistent giveaway data and conversation state are stored in MongoDB.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from bson import ObjectId
from pymongo import ASCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError

# Retain the model name for type hints; buttons are constructed through ui_button().
InlineKeyboardButton = TelegramInlineKeyboardButton

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Configure BOT_TOKEN and a freshly rotated FAMGATEWAY_API_KEY before running.
# Keep this file private; the database URI and API key are sensitive credentials.
# Rotate any credential that has been shared, then update it locally in this file.
# ---------------------------------------------------------------------------
BOT_TOKEN = "8862068749:AAEK6-dcZ-G7wHk9IEZWjpWg1IP03kaeKCc"
MONGODB_URI = 'mongodb+srv://anjashapw_db_user:M6zLDw1kZpu0gCl8@clustergivaway.j6qctkx.mongodb.net'
MONGODB_DATABASE = "nextgen_giveaway_bot"
ROOT_ADMIN_IDS = {8954471152}
ADMIN_IDS = set(ROOT_ADMIN_IDS)
FAMGATEWAY_API_KEY = "fam_7f7d5681939723bb5203aab0f38a5648cd519ec2"
FAMGATEWAY_EXPECTED_UPI_ID = "Anjasha@fam"
FAMGATEWAY_CREATE_ORDER_URL = "https://famgateway.in/api/create-order"
FAMGATEWAY_VERIFY_ORDER_URL = "https://famgateway.in/api/verify-order.php"
CHECK_INTERVAL_SECONDS = 60
PAYMENT_SCAN_INTERVAL_SECONDS = 5
PREMIUM_CHECK_INTERVAL_SECONDS = 30
PAYMENT_ORDER_EXPIRY_MINUTES = 5
PAYMENT_EXPIRY_GRACE_SECONDS = 120
MAX_PAID_VOTES_PER_ORDER = 10000

DEFAULT_SETTINGS: dict[str, Any] = {
    "maintenance_mode": False,
    "force_join_enabled": True,
    "anti_leave_default": True,
    "votes_per_account_default": 1,
    "admin_ids": [],
    "premium_plans": {
        "day": {"label": "1 Day", "days": 1, "price": 5},
        "week": {"label": "7 Days", "days": 7, "price": 20},
        "month": {"label": "30 Days", "days": 30, "price": 50},
    },
    "paid_vote_pack_price": 1,
    "paid_vote_pack_votes": 2,
    "paid_vote_min_votes": 20,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nextgen_giveaway_bot")

mongo: AsyncMongoClient | None = None
db: Any = None
bot: Bot | None = None
bot_username = ""


class MongoFSMStorage(BaseStorage):
    """Small aiogram FSM adapter backed by the same MongoDB database."""

    def __init__(self) -> None:
        self.key_builder = DefaultKeyBuilder(prefix="fsm", with_bot_id=True, with_destiny=True)

    def _key(self, key: StorageKey) -> str:
        return self.key_builder.build(key)

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        value = state.state if isinstance(state, State) else state
        await db.fsm_state.update_one(
            {"_id": self._key(key)},
            {"$set": {"state": value, "updated_at": now_utc()}},
            upsert=True,
        )

    async def get_state(self, key: StorageKey) -> str | None:
        record = await db.fsm_state.find_one({"_id": self._key(key)}, {"state": 1})
        return record.get("state") if record else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await db.fsm_state.update_one(
            {"_id": self._key(key)},
            {"$set": {"data": dict(data), "updated_at": now_utc()}},
            upsert=True,
        )

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        record = await db.fsm_state.find_one({"_id": self._key(key)}, {"data": 1})
        return dict(record.get("data") or {}) if record else {}

    async def close(self) -> None:
        return None


router = Router()
dp = Dispatcher(storage=MongoFSMStorage())
dp.include_router(router)

# Locks make vote/end operations serial within the single polling worker.
_giveaway_locks: dict[str, asyncio.Lock] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


_SMALL_CAPS = dict(
    zip(
        "abcdefghijklmnopqrstuvwxyz",
        "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
    )
)


def smallcaps_title(value: str) -> str:
    """Title case with a subtle small-caps treatment for headings."""
    pieces = re.split(r"(\s+)", value.strip())
    output: list[str] = []
    for piece in pieces:
        if not piece or piece.isspace():
            output.append(piece)
            continue
        first_index = next((i for i, char in enumerate(piece) if char.isalpha()), None)
        if first_index is None:
            output.append(piece)
            continue
        first = piece[:first_index] + piece[first_index].upper()
        rest = "".join(_SMALL_CAPS.get(ch.lower(), ch) for ch in piece[first_index + 1 :])
        output.append(first + rest)
    return "".join(output)


def heading(value: str) -> str:
    return f"<b>{esc(smallcaps_title(value))}</b>"


def display_name(user: types.User) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "User"


def clean_button_label(value: Any, limit: int = 60) -> str:
    """Keep dynamic channel titles from adding emoji to otherwise clean buttons."""
    text = str(value or "")
    cleaned = "".join(
        char for char in text
        if not (
            0x1F000 <= ord(char) <= 0x1FAFF
            or 0x2600 <= ord(char) <= 0x27BF
            or ord(char) in {0x200D, 0xFE0E, 0xFE0F}
        )
    ).strip()
    return (cleaned or "Channel")[:limit]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_root_admin(user_id: int) -> bool:
    return user_id in ROOT_ADMIN_IDS


def oid(value: str | ObjectId | None) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    if value and ObjectId.is_valid(str(value)):
        return ObjectId(str(value))
    return None


def gw_lock(giveaway_id: ObjectId | str) -> asyncio.Lock:
    key = str(giveaway_id)
    if key not in _giveaway_locks:
        _giveaway_locks[key] = asyncio.Lock()
    return _giveaway_locks[key]


def ui_button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> TelegramInlineKeyboardButton:
    """Use Telegram's native button colors with sensible action defaults."""
    if style is None:
        label = text.casefold()
        if "unban" in label or any(term in label for term in ("start giveaway", "participate", "join ", "add channel")):
            style = "success"
        elif any(term in label for term in ("delete", "end giveaway", "remove", "cancel", "ban")):
            style = "danger"
        else:
            style = "primary"
    return TelegramInlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style=style,
    )


def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            ui_button(text="Giveaways", callback_data="menu:giveaways"),
            ui_button(text="My Channels", callback_data="menu:channels"),
        ],
        [
            ui_button(text="Premium Plans", callback_data="menu:premium"),
            ui_button(text="Paid Vote", callback_data="menu:paid-vote"),
        ],
        [
            ui_button(text="My Profile", callback_data="menu:profile"),
            ui_button(text="Support", url="https://t.me/TGMandi_Support_bot"),
        ],
    ]
    if is_admin(user_id):
        rows.append([ui_button(text="Admin Panel", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def simple_keyboard(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ui_button(text=label, callback_data=data)] for label, data in buttons]
    )


async def safe_answer(callback: CallbackQuery, text: str = "", alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=alert)
    except Exception:
        pass


async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Message edit rejected by Telegram: %s", exc)
    except Exception as exc:
        logger.warning("Could not edit bot message: %s", exc)


async def save_user(user: types.User) -> None:
    await db.users.update_one(
        {"_id": user.id},
        {
            "$set": {
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "username": user.username or "",
                "updated_at": now_utc(),
            },
            "$setOnInsert": {"created_at": now_utc(), "is_banned": False},
        },
        upsert=True,
    )


async def global_settings() -> dict[str, Any]:
    doc = await db.settings.find_one({"_id": "global"})
    if not doc:
        await db.settings.update_one(
            {"_id": "global"}, {"$setOnInsert": DEFAULT_SETTINGS}, upsert=True
        )
        doc = await db.settings.find_one({"_id": "global"})
    settings = {**DEFAULT_SETTINGS, **(doc or {})}
    stored_plans = (doc or {}).get("premium_plans") or {}
    settings["premium_plans"] = {
        slug: {**plan, **(stored_plans.get(slug) or {})}
        for slug, plan in DEFAULT_SETTINGS["premium_plans"].items()
    }
    return settings


async def has_active_premium(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    user = await db.users.find_one({"_id": user_id}, {"premium_until": 1}) or {}
    expiry = as_utc(user.get("premium_until"))
    return bool(expiry and expiry > now_utc())


async def premium_status_text(user_id: int) -> str:
    if is_admin(user_id):
        return "Unlimited (administrator)"
    user = await db.users.find_one({"_id": user_id}, {"premium_until": 1}) or {}
    expiry = as_utc(user.get("premium_until"))
    if expiry and expiry > now_utc():
        return f"Active until {date_label(expiry)}"
    return "Inactive"


class PaymentProviderError(RuntimeError):
    """Safe, user-facing payment-provider error without credential details."""


_premium_locks: dict[int, asyncio.Lock] = {}


def famgateway_ready() -> bool:
    key = (FAMGATEWAY_API_KEY or "").strip()
    return bool(key and key != "PASTE_NEW_FAMGATEWAY_API_KEY_HERE")


def _payment_payload_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(payload.get("order"), dict):
        return payload["order"]
    return payload


def payment_status_from_payload(payload: Any) -> str:
    data = _payment_payload_data(payload)
    nested = [
        data.get("payment_status"),
        data.get("order_status"),
        data.get("transaction_status"),
        data.get("status"),
    ]
    candidates = [value for value in nested if value is not None and str(value).strip()]
    if not candidates and isinstance(payload, dict) and payload.get("status") is not None and str(payload["status"]).strip():
        candidates = [payload["status"]]
    for candidate in candidates:
        value = str(candidate).strip().casefold()
        if value in {"success", "successful", "paid", "completed", "complete", "approved"}:
            return "success"
        if value in {"expired", "cancelled", "canceled", "failed_expired"}:
            return "expired"
        if value in {"pending", "processing", "created", "initiated", "waiting"}:
            return "pending"
    return "pending"


async def famgateway_create_order(amount_inr: int, customer_name: str) -> dict[str, str]:
    if not famgateway_ready():
        raise PaymentProviderError(
            "Payments are not configured yet. Add a freshly rotated FamGateway API key in bot.py."
        )
    if amount_inr < 1 or amount_inr > 100000:
        raise PaymentProviderError("The payment amount is outside the supported range.")
    headers = {"X-Api-Key": FAMGATEWAY_API_KEY.strip(), "Accept": "application/json"}
    payload = {"amount": int(amount_inr), "customer_name": (customer_name or "Telegram User")[:80]}
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(FAMGATEWAY_CREATE_ORDER_URL, json=payload, headers=headers) as response:
                body = await response.json(content_type=None)
                if response.status < 200 or response.status >= 300 or not isinstance(body, dict):
                    raise PaymentProviderError("FamGateway could not create a payment order. Check its API key and merchant setup.")
    except PaymentProviderError:
        raise
    except Exception as exc:
        logger.warning("FamGateway order creation failed (%s).", type(exc).__name__)
        raise PaymentProviderError("Could not reach FamGateway. Please try again shortly.") from None

    provider_status = str(body.get("status", "")).strip().casefold()
    if provider_status in {"error", "failed", "failure"} or body.get("success") is False:
        raise PaymentProviderError("FamGateway rejected the order. Check the API key and merchant dashboard.")
    data = _payment_payload_data(body)
    order_id = str(data.get("order_id") or data.get("id") or "").strip()
    if not order_id:
        raise PaymentProviderError("FamGateway returned an invalid order. No payment was requested.")
    order = {
        "order_id": order_id,
        "qr_url": str(data.get("qr_url") or "").strip(),
        "checkout_url": str(data.get("checkout_url") or "").strip(),
        "upi_intent": str(data.get("upi_intent") or "").strip(),
        "upi_id": str(data.get("upi_id") or "").strip(),
    }
    usable_intent = order["upi_intent"].startswith(("https://", "http://"))
    usable_checkout = order["checkout_url"].startswith(("https://", "http://"))
    if not (order["qr_url"] or usable_checkout or usable_intent):
        raise PaymentProviderError("FamGateway did not return a QR or web payment link. No payment was requested.")
    actual_upi = order["upi_id"].replace(" ", "").casefold()
    expected_upi = FAMGATEWAY_EXPECTED_UPI_ID.replace(" ", "").casefold()
    if actual_upi and expected_upi and actual_upi != expected_upi:
        logger.error("FamGateway returned a receiver different from the configured UPI ID; order not offered to user.")
        raise PaymentProviderError("The FamGateway receiver does not match the configured UPI ID. Ask support to check merchant setup.")
    return order


async def famgateway_verify_order(order_id: str) -> dict[str, Any]:
    if not famgateway_ready():
        raise PaymentProviderError("FamGateway API key is not configured.")
    headers = {"X-Api-Key": FAMGATEWAY_API_KEY.strip(), "Accept": "application/json"}
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                FAMGATEWAY_VERIFY_ORDER_URL,
                params={"order_id": order_id},
                headers=headers,
            ) as response:
                body = await response.json(content_type=None)
                if response.status < 200 or response.status >= 300 or not isinstance(body, dict):
                    raise PaymentProviderError("FamGateway verification is temporarily unavailable.")
                return body
    except PaymentProviderError:
        raise
    except Exception as exc:
        logger.warning("FamGateway verification failed for an order (%s).", type(exc).__name__)
        raise PaymentProviderError("FamGateway verification is temporarily unavailable.") from None


async def send_payment_order(
    *,
    user_id: int,
    amount_inr: int,
    kind: str,
    description: str,
    metadata: dict[str, Any],
) -> str:
    assert bot is not None
    user = await db.users.find_one({"_id": user_id}) or {}
    order = await famgateway_create_order(amount_inr, user.get("first_name", "Telegram User"))
    created_at = now_utc()
    payment_record = {
        "provider": "famgateway",
        "provider_order_id": order["order_id"],
        "user_id": int(user_id),
        "kind": kind,
        "amount_inr": int(amount_inr),
        "status": "pending",
        "metadata": dict(metadata),
        "created_at": created_at,
        "expires_at": created_at + timedelta(minutes=PAYMENT_ORDER_EXPIRY_MINUTES),
    }
    try:
        await db.payments.insert_one(payment_record)
    except DuplicateKeyError:
        raise PaymentProviderError("FamGateway returned a duplicate order ID. Please retry in a moment.") from None

    caption = (
        f"{heading('Payment Pending')}\n\n"
        f"<b>For:</b> {esc(description)}\n"
        f"<b>Amount:</b> ₹{int(amount_inr)}\n"
        f"<b>Order ID:</b> <code>{esc(order['order_id'])}</code>\n"
        "Payment is checked automatically. Do not pay twice. The QR/order expires in about five minutes."
    )
    if order.get("upi_id"):
        caption += f"\n<b>Receiver:</b> <code>{esc(order['upi_id'])}</code>"
    link_rows: list[list[InlineKeyboardButton]] = []
    if order.get("checkout_url", "").startswith(("https://", "http://")):
        link_rows.append([ui_button(text="Open Payment Page", url=order["checkout_url"], style="success")])
    if order.get("upi_intent", "").startswith(("https://", "http://")):
        link_rows.append([ui_button(text="Open UPI App", url=order["upi_intent"], style="primary")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=link_rows) if link_rows else None
    if order.get("qr_url"):
        try:
            await bot.send_photo(user_id, photo=order["qr_url"], caption=caption, reply_markup=keyboard)
            return order["order_id"]
        except Exception as exc:
            logger.info("Could not send FamGateway QR as a photo (%s); falling back to a link.", type(exc).__name__)
            caption += f"\n<a href=\"{esc(order['qr_url'])}\">Open payment QR</a>"
    try:
        await bot.send_message(user_id, caption, reply_markup=keyboard, disable_web_page_preview=False)
    except Exception as exc:
        logger.info("Could not deliver FamGateway payment instructions (%s).", type(exc).__name__)
        raise PaymentProviderError("The order was created, but payment instructions could not be delivered. Please open the bot and try again.") from None
    return order["order_id"]


async def extend_premium(
    user_id: int,
    days: int,
    *,
    granted_by: int | None = None,
    payment_order_id: str | None = None,
) -> datetime:
    lock = _premium_locks.setdefault(int(user_id), asyncio.Lock())
    async with lock:
        user = await db.users.find_one({"_id": int(user_id)}) or {}
        current = as_utc(user.get("premium_until"))
        if payment_order_id and payment_order_id in (user.get("premium_applied_orders") or []):
            return current or now_utc()
        now = now_utc()
        expiry = max(current, now) + timedelta(days=int(days)) if current else now + timedelta(days=int(days))
        fields: dict[str, Any] = {
            "premium_until": expiry,
            "premium_activated_at": now,
            "premium_updated_at": now,
        }
        if granted_by is not None:
            fields["premium_granted_by"] = int(granted_by)
        update: dict[str, Any] = {
            "$set": fields,
            "$setOnInsert": {"created_at": now, "is_banned": False},
        }
        if payment_order_id:
            update["$addToSet"] = {"premium_applied_orders": payment_order_id}
        await db.users.update_one({"_id": int(user_id)}, update, upsert=True)
        return expiry


async def pause_giveaway_for_expired_plan(giveaway: dict[str, Any]) -> bool:
    giveaway_id = giveaway["_id"]
    changed = await db.giveaways.update_one(
        {"_id": giveaway_id, "status": "active"},
        {"$set": {"status": "paused", "paused_reason": "premium_expired", "paused_at": now_utc()}},
    )
    if not changed.modified_count:
        return False
    paused = {**giveaway, "status": "paused", "paused_reason": "premium_expired"}
    participants = await db.participants.find({"giveaway_id": giveaway_id}).to_list(length=10000)
    for participant in participants:
        await update_entry_button(paused, participant)
    try:
        await bot.send_message(
            giveaway["channel_id"],
            f"{heading('Giveaway Paused')}\n\nVoting and new entries are temporarily paused while the owner renews Premium.",
        )
    except Exception:
        logger.info("Could not announce premium-expiry pause for giveaway %s.", giveaway_id)
    try:
        await bot.send_message(
            int(giveaway["creator_id"]),
            f"{heading('Giveaway Paused')}\n\nYour Premium plan expired, so giveaway #{str(giveaway_id)[-6:]} has been paused. Renew Premium to resume it.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Renew Premium", callback_data="menu:premium", style="success")]
            ]),
        )
    except Exception:
        logger.info("Could not DM owner %s about a paused giveaway.", giveaway.get("creator_id"))
    return True


async def resume_owner_giveaways(user_id: int) -> int:
    resumed = 0
    cursor = db.giveaways.find({
        "creator_id": int(user_id),
        "status": "paused",
        "paused_reason": "premium_expired",
    })
    async for giveaway in cursor:
        changed = await db.giveaways.update_one(
            {"_id": giveaway["_id"], "status": "paused", "paused_reason": "premium_expired"},
            {"$set": {"status": "active", "resumed_at": now_utc()}, "$unset": {"paused_reason": "", "paused_at": ""}},
        )
        if not changed.modified_count:
            continue
        active = {**giveaway, "status": "active"}
        participants = await db.participants.find({"giveaway_id": giveaway["_id"]}).to_list(length=10000)
        for participant in participants:
            await update_entry_button(active, participant)
        try:
            await bot.send_message(
                giveaway["channel_id"],
                f"{heading('Giveaway Resumed')}\n\nEntries and voting are open again. Use the original giveaway post to participate or vote.",
            )
        except Exception:
            logger.info("Could not announce resume for giveaway %s.", giveaway["_id"])
        try:
            await bot.send_message(
                int(user_id),
                f"Giveaway #{str(giveaway['_id'])[-6:]} has resumed in <b>{esc(giveaway.get('channel_title', 'your channel'))}</b>.",
            )
        except Exception:
            pass
        resumed += 1
    return resumed


async def reconcile_premium_giveaways() -> None:
    active = await db.giveaways.find({"status": "active"}).to_list(length=10000)
    for giveaway in active:
        owner_id = int(giveaway.get("creator_id", 0))
        if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
            await pause_giveaway_for_expired_plan(giveaway)
    paused = await db.giveaways.find({"status": "paused", "paused_reason": "premium_expired"}).to_list(length=10000)
    owners: set[int] = set()
    for giveaway in paused:
        owner_id = int(giveaway.get("creator_id", 0))
        if owner_id:
            owners.add(owner_id)
    for owner_id in owners:
        if await has_active_premium(owner_id):
            await resume_owner_giveaways(owner_id)


async def premium_expiry_checker() -> None:
    while True:
        try:
            await reconcile_premium_giveaways()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Premium expiry check failed (%s).", type(exc).__name__)
        await asyncio.sleep(PREMIUM_CHECK_INTERVAL_SECONDS)


async def notify_payment_review(payment: dict[str, Any], reason: str) -> None:
    text = (
        f"{heading('Payment Needs Review')}\n\n"
        f"<b>Order:</b> <code>{esc(payment.get('provider_order_id'))}</code>\n"
        f"<b>User:</b> <code>{int(payment.get('user_id', 0))}</code>\n"
        f"<b>Reason:</b> {esc(reason)}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def settle_verified_payment(payment: dict[str, Any], verification: dict[str, Any]) -> None:
    payment_id = payment["_id"]
    order_id = str(payment.get("provider_order_id", ""))
    current = await db.payments.find_one({"_id": payment_id}) or payment
    if current.get("status") == "activated":
        return
    claim = await db.payments.update_one(
        {"_id": payment_id, "status": {"$in": ["pending", "processing", "expired"]}},
        {"$set": {"status": "processing", "verified_at": now_utc()}},
    )
    if not claim.modified_count:
        latest = await db.payments.find_one({"_id": payment_id}) or {}
        if latest.get("status") == "activated":
            return
        if latest.get("status") == "manual_review":
            return
    user_id = int(payment.get("user_id", 0))
    kind = payment.get("kind")
    metadata = payment.get("metadata") or {}

    if kind == "premium":
        days = int(metadata.get("plan_days", 0))
        if not user_id or days < 1 or days > 3650:
            await db.payments.update_one({"_id": payment_id}, {"$set": {"status": "manual_review", "review_reason": "invalid premium order metadata"}})
            await notify_payment_review(payment, "Invalid premium plan data; confirm and grant manually.")
            return
        expiry = await extend_premium(user_id, days, payment_order_id=order_id)
        await db.payments.update_one(
            {"_id": payment_id},
            {"$set": {"status": "activated", "activated_at": now_utc(), "premium_until": expiry}},
        )
        await resume_owner_giveaways(user_id)
        try:
            await bot.send_message(
                user_id,
                f"{heading('Premium Activated')}\n\nYour <b>{days}-day</b> Premium plan is active until <b>{date_label(expiry)}</b>. Any giveaways paused for expiry have been resumed.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [ui_button(text="My Giveaways", callback_data="menu:giveaways")],
                    [ui_button(text="Main Menu", callback_data="menu:home")],
                ]),
            )
        except Exception:
            pass
        return

    if kind == "paid_votes":
        giveaway_id = oid(metadata.get("giveaway_id"))
        participant_id = oid(metadata.get("participant_id"))
        vote_count = int(metadata.get("votes", 0))
        giveaway = await db.giveaways.find_one({"_id": giveaway_id}) if giveaway_id else None
        participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id}) if participant_id and giveaway_id else None
        if not giveaway or not participant or giveaway.get("status") not in {"active", "paused"}:
            reason = "The target giveaway entry is no longer active; payment is confirmed and needs admin review."
            await db.payments.update_one({"_id": payment_id}, {"$set": {"status": "manual_review", "review_reason": reason}})
            await notify_payment_review(payment, reason)
            try:
                await bot.send_message(user_id, f"{heading('Payment Received')}\n\nYour payment is confirmed, but the giveaway has closed. Support has been notified to resolve this.")
            except Exception:
                pass
            return
        if vote_count < 1 or vote_count > MAX_PAID_VOTES_PER_ORDER:
            reason = "Invalid paid vote quantity in order metadata."
            await db.payments.update_one({"_id": payment_id}, {"$set": {"status": "manual_review", "review_reason": reason}})
            await notify_payment_review(payment, reason)
            return
        result = await db.participants.update_one(
            {"_id": participant_id, "paid_payment_ids": {"$ne": order_id}},
            {"$inc": {"vote_count": vote_count}, "$addToSet": {"paid_payment_ids": order_id}},
        )
        if result.modified_count:
            participant = await db.participants.find_one({"_id": participant_id}) or participant
            await update_entry_button(giveaway, participant)
        else:
            participant = await db.participants.find_one({"_id": participant_id}) or participant
            if order_id not in (participant.get("paid_payment_ids") or []):
                reason = "Could not apply the purchased votes to the target entry."
                await db.payments.update_one({"_id": payment_id}, {"$set": {"status": "manual_review", "review_reason": reason}})
                await notify_payment_review(payment, reason)
                return
        await db.payments.update_one(
            {"_id": payment_id},
            {"$set": {"status": "activated", "activated_at": now_utc()}},
        )
        paused_note = " The giveaway is paused; the count will be visible when it resumes." if giveaway.get("status") == "paused" else ""
        try:
            await bot.send_message(
                user_id,
                f"{heading('Paid Votes Added')}\n\n<b>{vote_count}</b> votes were added to <b>{esc(participant.get('candidate_name', 'the entry'))}</b>.{paused_note}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [ui_button(text="Main Menu", callback_data="menu:home")]
                ]),
            )
        except Exception:
            pass
        return

    reason = "Unknown payment type."
    await db.payments.update_one({"_id": payment_id}, {"$set": {"status": "manual_review", "review_reason": reason}})
    await notify_payment_review(payment, reason)


async def expire_payment(payment: dict[str, Any]) -> None:
    result = await db.payments.update_one(
        {"_id": payment["_id"], "status": {"$in": ["pending", "processing"]}},
        {"$set": {"status": "expired", "expired_at": now_utc()}},
    )
    if result.modified_count:
        try:
            retry_label, retry_callback = ("Paid Vote", "menu:paid-vote") if payment.get("kind") == "paid_votes" else ("Premium Plans", "menu:premium")
            await bot.send_message(
                int(payment.get("user_id", 0)),
                f"FamGateway order <code>{esc(payment.get('provider_order_id'))}</code> expired without confirmation. Start again if you still want to purchase.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [ui_button(text=retry_label, callback_data=retry_callback)],
                    [ui_button(text="Main Menu", callback_data="menu:home")],
                ]),
            )
        except Exception:
            pass


async def _check_one_payment(payment: dict[str, Any], semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        try:
            response = await famgateway_verify_order(str(payment.get("provider_order_id", "")))
            status = payment_status_from_payload(response)
            if status == "success":
                await settle_verified_payment(payment, response)
            elif status == "expired":
                await expire_payment(payment)
            else:
                expires = as_utc(payment.get("expires_at"))
                if expires and now_utc() > expires + timedelta(seconds=PAYMENT_EXPIRY_GRACE_SECONDS):
                    await expire_payment(payment)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Payment check failed for an order (%s).", type(exc).__name__)


async def payment_monitor() -> None:
    while True:
        try:
            if famgateway_ready():
                pending = await db.payments.find({"status": {"$in": ["pending", "processing"]}}).sort("created_at", 1).limit(40).to_list(length=40)
                semaphore = asyncio.Semaphore(10)
                await asyncio.gather(*(_check_one_payment(payment, semaphore) for payment in pending))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Payment monitor encountered an error (%s).", type(exc).__name__)
        await asyncio.sleep(PAYMENT_SCAN_INTERVAL_SECONDS)


class AccessControlMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not is_admin(user.id):
            settings = await global_settings()
            if settings.get("maintenance_mode"):
                if isinstance(event, CallbackQuery):
                    await safe_answer(event, "The bot is temporarily under maintenance.", alert=True)
                elif isinstance(event, Message):
                    await event.answer("The bot is temporarily under maintenance. Please try again later.")
                return
            record = await db.users.find_one({"_id": user.id}, {"is_banned": 1})
            if record and record.get("is_banned"):
                if isinstance(event, CallbackQuery):
                    await safe_answer(event, "Access is restricted for this account.", alert=True)
                elif isinstance(event, Message):
                    await event.answer("Access is restricted for this account.")
                return
        return await handler(event, data)


async def check_bot_admin(channel_id: int, *, require_post: bool) -> tuple[bool, str]:
    assert bot is not None
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)
        if member.status == ChatMemberStatus.CREATOR:
            return True, "OK"
        if member.status != ChatMemberStatus.ADMINISTRATOR:
            return False, "Add the bot as a channel administrator first."
        if require_post and not getattr(member, "can_post_messages", False):
            return False, "The bot needs permission to post messages."
        if require_post and not getattr(member, "can_delete_messages", False):
            return False, "The bot needs permission to delete its entry posts."
        return True, "OK"
    except Exception:
        return False, "The bot cannot access this channel. Check the channel ID and bot admin status."


def channel_url(channel_id: int, username: str = "", invite_link: str = "") -> str:
    if username:
        return f"https://t.me/{username.lstrip('@')}"
    if invite_link:
        return invite_link
    raw = str(channel_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}"
    return ""


def post_url(channel_id: int, username: str, message_id: int) -> str:
    if username:
        return f"https://t.me/{username.lstrip('@')}/{message_id}"
    raw = str(channel_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return ""


def add_channel_url() -> str:
    # Telegram opens the native channel picker with the required rights preselected.
    permissions = "post_messages+edit_messages+delete_messages+invite_users"
    return f"https://t.me/{bot_username}?startchannel&admin={permissions}"


async def obtain_invite_link(chat: types.Chat) -> str:
    if chat.username:
        return f"https://t.me/{chat.username}"
    try:
        invite = await bot.create_chat_invite_link(chat.id)
        return invite.invite_link
    except Exception:
        return ""


async def resolve_channel(text: str) -> types.Chat:
    assert bot is not None
    value = text.strip()
    if "t.me/" in value:
        value = value.split("t.me/", 1)[1].split("/", 1)[0]
        if not value.startswith("+") and not value.startswith("joinchat"):
            value = "@" + value.lstrip("@")
    chat = await bot.get_chat(value if value.startswith("@") else int(value))
    if str(chat.type) not in {"channel", "ChatType.CHANNEL"}:
        raise ValueError("Please provide a Telegram channel, not a group or private chat.")
    return chat


def participant_keyboard(giveaway: dict[str, Any], participant: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ui_button(
                    text=f"{giveaway.get('vote_emoji', '👍')} {int(participant.get('vote_count', 0))}",
                    callback_data=f"v:{participant['_id']}",
                    style="success",
                )
            ]
        ]
    )


def date_label(value: Any) -> str:
    if not value:
        return "—"
    normalized = as_utc(value)
    if normalized:
        return normalized.strftime("%Y-%m-%d %H:%M UTC")
    return esc(value)


async def notify_admins_giveaway_started(giveaway: dict[str, Any], member_count: int) -> None:
    channel_link = channel_url(
        giveaway["channel_id"],
        giveaway.get("channel_username", ""),
        giveaway.get("channel_invite_link", ""),
    )
    channel_html = f'<a href="{esc(channel_link)}">Open Channel</a>' if channel_link else "Unavailable"
    participation_link = f"https://t.me/{bot_username}?start=join-{giveaway['_id']}"
    paid_vote_link = f"https://t.me/{bot_username}?start=paidvote-{giveaway['_id']}"
    target = int(giveaway.get("target_members", 0) or 0)
    target_text = f"{target} members" if target else "Manual End"
    text = (
        f"{heading('New Giveaway Started')}\n\n"
        f"<b>Giveaway ID:</b> <code>{esc(giveaway['_id'])}</code>\n"
        f"<b>Owner ID:</b> <code>{int(giveaway['creator_id'])}</code>\n"
        f"<b>Channel:</b> {esc(giveaway.get('channel_title', 'Channel'))}\n"
        f"<b>Channel Link:</b> {channel_html}\n"
        f"<b>Started At:</b> {int(member_count)} members\n"
        f"<b>Auto-End At:</b> {esc(target_text)}\n"
        f"<b>Winner Places:</b> {int(giveaway.get('winner_places', 1))}\n"
        f"<b>Vote Cap:</b> {int(giveaway.get('votes_per_account', 1))} per Telegram account\n"
        f'<b>Participation Link:</b> <a href="{esc(participation_link)}">Open Giveaway</a>\n'
        f'<b>Paid Vote Link:</b> <a href="{esc(paid_vote_link)}">Open Paid Vote</a>'
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.debug("Could not DM admin %s about giveaway start.", admin_id)


async def notify_owner_new_participant(
    giveaway: dict[str, Any],
    participant: dict[str, Any],
    participant_user: types.User,
    entry_link: str,
    channel_link: str,
) -> None:
    if int(giveaway.get("creator_id", 0)) == participant_user.id:
        return
    entry_html = f'<a href="{esc(entry_link)}">Open Entry Post</a>' if entry_link else "Unavailable"
    channel_html = f'<a href="{esc(channel_link)}">Open Channel</a>' if channel_link else "Unavailable"
    username = "@" + participant_user.username if participant_user.username else "Not Set"
    text = (
        f"{heading('New Participant Entry')}\n\n"
        f"<b>Giveaway ID:</b> <code>{esc(giveaway['_id'])}</code>\n"
        f"<b>Participant Name:</b> {esc(participant.get('candidate_name'))}\n"
        f"<b>Telegram Name:</b> {esc(display_name(participant_user))}\n"
        f"<b>Telegram Username:</b> {esc(username)}\n"
        f"<b>Telegram ID:</b> <code>{int(participant_user.id)}</code>\n"
        f"<b>Entry Post:</b> {entry_html}\n"
        f"<b>Channel:</b> {channel_html}\n"
        f"<b>Submitted At:</b> {date_label(participant.get('created_at'))}"
    )
    try:
        await bot.send_message(int(giveaway["creator_id"]), text)
    except Exception:
        logger.debug("Could not DM giveaway owner %s about a new entry.", giveaway.get("creator_id"))


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------
class ChannelInputState(StatesGroup):
    waiting_channel = State()


class CandidateNameState(StatesGroup):
    waiting_name = State()


class TargetMembersState(StatesGroup):
    waiting_target = State()


class VoteEmojiState(StatesGroup):
    waiting_emoji = State()


class AdminUserSearchState(StatesGroup):
    waiting_user_id = State()


class BroadcastState(StatesGroup):
    waiting_users_message = State()
    waiting_channels_message = State()


class PaidVoteState(StatesGroup):
    waiting_post_link = State()
    confirming_participant = State()
    waiting_quantity = State()


class MonetizationState(StatesGroup):
    waiting_value = State()


class AdminGrantPremiumState(StatesGroup):
    waiting_days = State()


class AdminVoteGrantState(StatesGroup):
    waiting_post_link = State()
    waiting_quantity = State()


class AdminAddAdminState(StatesGroup):
    waiting_user_id = State()


# ---------------------------------------------------------------------------
# Start, menus, and profile
# ---------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await save_user(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""

    if payload.startswith("join-"):
        giveaway_id = oid(payload[len("join-") :])
        if not giveaway_id:
            await message.answer("That giveaway link is invalid.")
            return
        await handle_join_giveaway(message, state, giveaway_id)
        return

    if payload.startswith("verify-"):
        values = payload.split("-")
        if len(values) == 3:
            giveaway_id, participant_id = oid(values[1]), oid(values[2])
            if giveaway_id and participant_id:
                await verification_flow(message, message.from_user, giveaway_id, participant_id)
                return
        await message.answer("That verification link is invalid or expired.")
        return

    if payload.startswith("paidvote-"):
        giveaway_id = oid(payload[len("paidvote-") :])
        if not giveaway_id:
            await message.answer("That paid-vote link is invalid.")
            return
        await begin_paid_vote_flow(message, state, giveaway_id)
        return

    name = esc(display_name(message.from_user))
    await message.answer(
        f"{heading('NextGen Giveaway Bot')}\n\n"
        f"Welcome, <b>{name}</b>.\n"
        "Create a channel giveaway, publish participant entries, and manage votes from one place.",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@router.callback_query(F.data == "menu:home")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await safe_edit(
        callback.message,
        f"{heading('NextGen Giveaway Bot')}\n\n"
        f"Welcome, <b>{esc(display_name(callback.from_user))}</b>.\nChoose an option below.",
        main_menu_keyboard(callback.from_user.id),
    )


@router.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    uid = callback.from_user.id
    user = await db.users.find_one({"_id": uid}) or {}
    giveaways_created = await db.giveaways.count_documents({"creator_id": uid})
    entries = await db.participants.count_documents({"user_id": uid})
    full_name = " ".join(x for x in (user.get("first_name", ""), user.get("last_name", "")) if x) or "User"
    premium_status = await premium_status_text(uid)
    text = (
        f"{heading('Your Profile')}\n\n"
        f"<b>Name:</b> {esc(full_name)}\n"
        f"<b>Telegram ID:</b> <code>{uid}</code>\n"
        f"<b>Giveaways Created:</b> {giveaways_created}\n"
        f"<b>Premium:</b> {esc(premium_status)}\n"
        f"<b>Entries Submitted:</b> {entries}"
    )
    await safe_edit(callback.message, text, simple_keyboard(("Back", "menu:home")))


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    if is_admin(callback.from_user.id):
        await safe_edit(callback.message, "Action cancelled.", simple_keyboard(("Admin Panel", "admin:home")))
    else:
        await safe_edit(callback.message, "Action cancelled.", simple_keyboard(("Main Menu", "menu:home")))


def _plan_duration_label(days: int) -> str:
    return f"{days} Day" if days == 1 else f"{days} Days"


@router.callback_query(F.data == "menu:premium")
async def cb_premium_menu(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    settings = await global_settings()
    status = await premium_status_text(callback.from_user.id)
    rows: list[list[InlineKeyboardButton]] = []
    if not is_admin(callback.from_user.id):
        for slug in ("day", "week", "month"):
            plan = settings["premium_plans"][slug]
            days = max(1, int(plan.get("days", 1)))
            price = max(1, int(plan.get("price", 1)))
            rows.append([ui_button(
                text=f"{_plan_duration_label(days)} — ₹{price}",
                callback_data=f"premium:buy:{slug}",
                style="success",
            )])
    rows.extend([
        [ui_button(text="Support", url="https://t.me/TGMandi_Support_bot")],
        [ui_button(text="Back", callback_data="menu:home")],
    ])
    if is_admin(callback.from_user.id):
        intro = "Administrators have unlimited Premium access."
    else:
        intro = "Premium is activated only after FamGateway confirms payment. An active plan is required to run giveaways."
    text = (
        f"{heading('Premium Plans')}\n\n"
        f"<b>Your plan:</b> {esc(status)}\n"
        f"{intro}\n"
        "If a plan expires, your running giveaways pause and resume after renewal."
    )
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("premium:buy:"))
async def cb_buy_premium(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    if is_admin(callback.from_user.id):
        await safe_edit(callback.message, "Administrators already have unlimited Premium access.", simple_keyboard(("Back", "menu:premium")))
        return
    slug = callback.data.rsplit(":", 1)[1]
    settings = await global_settings()
    plan = settings.get("premium_plans", {}).get(slug)
    if not plan or slug not in {"day", "week", "month"}:
        await safe_edit(callback.message, "That Premium plan is unavailable.", simple_keyboard(("Premium Plans", "menu:premium")))
        return
    days = int(plan.get("days", 0))
    price = int(plan.get("price", 0))
    if days < 1 or days > 3650 or price < 1 or price > 100000:
        await safe_edit(
            callback.message,
            "This plan is not configured correctly. Please contact support.",
            InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Support", url="https://t.me/TGMandi_Support_bot")],
                [ui_button(text="Premium Plans", callback_data="menu:premium")],
            ]),
        )
        return
    try:
        order_id = await send_payment_order(
            user_id=callback.from_user.id,
            amount_inr=price,
            kind="premium",
            description=f"{_plan_duration_label(days)} Premium plan",
            metadata={"plan_slug": slug, "plan_days": days},
        )
    except PaymentProviderError as exc:
        await safe_edit(
            callback.message,
            esc(exc),
            InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Premium Plans", callback_data="menu:premium")],
                [ui_button(text="Support", url="https://t.me/TGMandi_Support_bot")],
                [ui_button(text="Main Menu", callback_data="menu:home")],
            ]),
        )
        return
    await safe_edit(
        callback.message,
        f"{heading('Premium Payment Created')}\n\n"
        f"Order <code>{esc(order_id)}</code> is waiting for FamGateway confirmation. The plan starts only after confirmation.",
        simple_keyboard(("Premium Plans", "menu:premium"), ("Main Menu", "menu:home")),
    )


async def begin_paid_vote_flow(
    target: Message | CallbackQuery,
    state: FSMContext,
    expected_giveaway_id: ObjectId | None = None,
) -> None:
    await state.clear()
    await state.set_state(PaidVoteState.waiting_post_link)
    await state.update_data(expected_giveaway_id=str(expected_giveaway_id) if expected_giveaway_id else None)
    text = (
        f"{heading('Paid Vote')}\n\n"
        "Send the Telegram link to the participant's giveaway entry post. I will show the participant name for confirmation before asking the vote quantity."
    )
    keyboard = simple_keyboard(("Cancel", "paidvote:cancel"))
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:paid-vote")
async def cb_begin_paid_vote(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await begin_paid_vote_flow(callback, state)


async def resolve_entry_post_link(link: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    raw = (link or "").strip()
    if not raw:
        return None, None, "Send a Telegram giveaway entry post link."
    parse_value = raw if "://" in raw else "https://" + raw
    try:
        parsed = urlparse(parse_value)
        host = (parsed.netloc or "").lower().split(":", 1)[0]
        if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            return None, None, "Use a Telegram post link such as https://t.me/channel/123 or https://t.me/c/123/456."
        segments = [part for part in parsed.path.split("/") if part]
        if segments and segments[0].casefold() == "s":
            segments = segments[1:]
        if len(segments) < 2:
            return None, None, "The link must point directly to a participant entry post."
        if segments[0].casefold() == "c":
            if len(segments) < 3 or not segments[1].isdigit():
                return None, None, "That private-channel post link is invalid."
            channel_id = int(f"-100{segments[1]}")
            message_id = int(segments[2])
        else:
            username = segments[0].lstrip("@")
            if username.startswith("+") or username.casefold() == "joinchat" or not segments[1].isdigit():
                return None, None, "That link is not a public channel post link. Use the direct entry post link."
            message_id = int(segments[1])
            chat = await bot.get_chat("@" + username)
            if str(chat.type) not in {"channel", "ChatType.CHANNEL"}:
                return None, None, "The linked post is not in a Telegram channel."
            channel_id = int(chat.id)
    except (ValueError, IndexError):
        return None, None, "That Telegram post link is invalid. Check the link and send it again."
    except Exception:
        return None, None, "I could not open that channel. Check the post link and try again."

    giveaways = await db.giveaways.find({"channel_id": channel_id}).sort("created_at", -1).limit(100).to_list(length=100)
    for giveaway in giveaways:
        participant = await db.participants.find_one({"giveaway_id": giveaway["_id"], "message_id": message_id})
        if participant:
            if giveaway.get("status") == "active":
                owner_id = int(giveaway.get("creator_id", 0))
                if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
                    await pause_giveaway_for_expired_plan(giveaway)
                    return giveaway, participant, "This giveaway is paused because its owner's Premium plan expired."
                return giveaway, participant, ""
            if giveaway.get("status") == "paused":
                return giveaway, participant, "This giveaway is paused, so paid votes are temporarily unavailable."
            return giveaway, participant, "This giveaway has ended; paid votes are unavailable."
    return None, None, "No participant entry was found at that link. Send the exact channel post link."


@router.message(PaidVoteState.waiting_post_link)
async def handle_paid_vote_post_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    giveaway, participant, error = await resolve_entry_post_link(message.text or "")
    if error or not giveaway or not participant:
        await message.answer(error or "Entry not found. Try the direct post link again.")
        return
    expected = oid(data.get("expected_giveaway_id"))
    if expected and giveaway["_id"] != expected:
        await message.answer("That post belongs to a different giveaway. Send an entry link from the giveaway you opened.")
        return
    await state.update_data(
        giveaway_id=str(giveaway["_id"]),
        participant_id=str(participant["_id"]),
        candidate_name=str(participant.get("candidate_name", "Entry")),
        post_link=(message.text or "").strip(),
    )
    await state.set_state(PaidVoteState.confirming_participant)
    text = (
        f"{heading('Confirm Participant')}\n\n"
        f"<b>Participant:</b> {esc(participant.get('candidate_name', 'Entry'))}\n"
        f"<b>Giveaway:</b> {esc(giveaway.get('channel_title', 'Channel'))}\n\n"
        "Is this the entry you want to support with paid votes?"
    )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Confirm Entry", callback_data="paidvote:confirm", style="success")],
        [ui_button(text="Change Link", callback_data="paidvote:change", style="primary")],
        [ui_button(text="Cancel", callback_data="paidvote:cancel", style="danger")],
    ]))


@router.callback_query(F.data == "paidvote:confirm")
async def cb_paid_vote_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    data = await state.get_data()
    giveaway_id, participant_id = oid(data.get("giveaway_id")), oid(data.get("participant_id"))
    if not giveaway_id or not participant_id:
        await state.clear()
        await safe_edit(callback.message, "That paid-vote session expired. Start again from the giveaway post.", simple_keyboard(("Main Menu", "menu:home")))
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id})
    if not giveaway or not participant or giveaway.get("status") != "active":
        await state.clear()
        await safe_edit(callback.message, "This giveaway is no longer accepting paid votes.", simple_keyboard(("Main Menu", "menu:home")))
        return
    owner_id = int(giveaway.get("creator_id", 0))
    if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
        await pause_giveaway_for_expired_plan(giveaway)
        await state.clear()
        await safe_edit(callback.message, "This giveaway has been paused because its owner's Premium plan expired.", simple_keyboard(("Main Menu", "menu:home")))
        return
    settings = await global_settings()
    pack_price = max(1, int(settings.get("paid_vote_pack_price", 1)))
    pack_votes = max(1, int(settings.get("paid_vote_pack_votes", 2)))
    minimum = max(1, int(settings.get("paid_vote_min_votes", 20)))
    await state.set_state(PaidVoteState.waiting_quantity)
    await safe_edit(
        callback.message,
        f"{heading('Choose Vote Quantity')}\n\n"
        f"<b>Entry:</b> {esc(participant.get('candidate_name', 'Entry'))}\n"
        f"<b>Rate:</b> ₹{pack_price} for {pack_votes} votes\n"
        f"<b>Minimum:</b> {minimum} votes\n\n"
        f"Send the number of votes to purchase (maximum {MAX_PAID_VOTES_PER_ORDER}). The quantity must be a multiple of {pack_votes}.",
        simple_keyboard(("Cancel", "paidvote:cancel")),
    )


@router.callback_query(F.data == "paidvote:change")
async def cb_paid_vote_change_link(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    data = await state.get_data()
    expected = oid(data.get("expected_giveaway_id"))
    await state.set_state(PaidVoteState.waiting_post_link)
    await state.update_data(expected_giveaway_id=str(expected) if expected else None)
    await safe_edit(callback.message, "Send the direct participant entry post link.", simple_keyboard(("Cancel", "paidvote:cancel")))


@router.message(PaidVoteState.waiting_quantity)
async def handle_paid_vote_quantity(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Send the vote quantity as a whole number.")
        return
    votes = int(raw)
    if votes < 1 or votes > MAX_PAID_VOTES_PER_ORDER:
        await message.answer(f"Choose a quantity from 1 to {MAX_PAID_VOTES_PER_ORDER} votes.")
        return
    settings = await global_settings()
    minimum = max(1, int(settings.get("paid_vote_min_votes", 20)))
    pack_votes = max(1, int(settings.get("paid_vote_pack_votes", 2)))
    pack_price = max(1, int(settings.get("paid_vote_pack_price", 1)))
    if votes < minimum:
        await message.answer(f"The minimum purchase is {minimum} votes.")
        return
    if votes % pack_votes:
        await message.answer(f"Choose a multiple of {pack_votes} votes so the price can be calculated exactly.")
        return
    amount = (votes // pack_votes) * pack_price
    if amount < 1 or amount > 100000:
        await message.answer("That quantity is outside the supported payment range. Choose a smaller amount.")
        return
    data = await state.get_data()
    giveaway_id, participant_id = oid(data.get("giveaway_id")), oid(data.get("participant_id"))
    giveaway = await db.giveaways.find_one({"_id": giveaway_id}) if giveaway_id else None
    participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id}) if giveaway_id and participant_id else None
    if not giveaway or not participant or giveaway.get("status") != "active":
        await state.clear()
        await message.answer("This giveaway is no longer accepting paid votes.")
        return
    try:
        async with gw_lock(giveaway_id):
            fresh_giveaway = await db.giveaways.find_one({"_id": giveaway_id})
            fresh_participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id})
            if not fresh_giveaway or not fresh_participant or fresh_giveaway.get("status") != "active":
                await state.clear()
                await message.answer("This giveaway is no longer accepting paid votes.")
                return
            owner_id = int(fresh_giveaway.get("creator_id", 0))
            if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
                await pause_giveaway_for_expired_plan(fresh_giveaway)
                await state.clear()
                await message.answer("This giveaway has been paused because its owner's Premium plan expired.")
                return
            order_id = await send_payment_order(
                user_id=message.from_user.id,
                amount_inr=amount,
                kind="paid_votes",
                description=f"{votes} paid votes for {fresh_participant.get('candidate_name', 'the entry')}",
                metadata={
                    "giveaway_id": giveaway_id,
                    "participant_id": participant_id,
                    "votes": votes,
                },
            )
    except PaymentProviderError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(
        f"FamGateway order <code>{esc(order_id)}</code> was created for <b>{votes}</b> votes at <b>₹{amount}</b>. Votes are added automatically after confirmation.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="Main Menu", callback_data="menu:home")]
        ]),
    )


@router.callback_query(F.data == "paidvote:cancel")
async def cb_paid_vote_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await safe_edit(callback.message, "Paid-vote purchase cancelled.", simple_keyboard(("Main Menu", "menu:home")))


# ---------------------------------------------------------------------------
# User channel management
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:channels")
async def cb_channels_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    channels = await db.owned_channels.find({"owner_id": callback.from_user.id}).sort("created_at", -1).limit(30).to_list(length=30)
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        title = str(channel.get("title") or channel["_id"])
        rows.append([ui_button(text=clean_button_label(title), callback_data=f"channel:view:{channel['_id']}")])
    rows.append([ui_button(text="Add Bot To Channel", url=add_channel_url(), style="success")])
    rows.append([ui_button(text="Connect Existing Channel", callback_data="channel:add")])
    rows.append([ui_button(text="Back", callback_data="menu:home")])
    count_text = f"You have connected <b>{len(channels)}</b> channel(s)." if channels else "No channels are connected yet."
    await safe_edit(
        callback.message,
        f"{heading('My Channels')}\n\n{count_text}\n\nThe bot must be an administrator with posting and deletion permissions.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "channel:add")
async def cb_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(ChannelInputState.waiting_channel)
    await state.update_data(channel_mode="owner")
    await safe_edit(
        callback.message,
        f"{heading('Connect A Channel')}\n\n"
        "Send a public @username or numeric channel ID.\n"
        "Add the bot as an administrator first, with permission to post and delete its own entry posts.",
        simple_keyboard(("Cancel", "cancel")),
    )


@router.message(ChannelInputState.waiting_channel)
async def handle_channel_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    mode = data.get("channel_mode", "owner")
    try:
        chat = await resolve_channel(message.text or "")
    except (ValueError, TypeError):
        await message.answer("Send a valid public @channelname or numeric channel ID.")
        return
    except Exception:
        await message.answer("I could not find that channel. Check the ID and make sure the bot is already an administrator.")
        return

    if mode == "owner":
        ok, reason = await check_bot_admin(chat.id, require_post=True)
        if not ok:
            await message.answer(esc(reason))
            return
        link = await obtain_invite_link(chat)
        if not chat.username and not link:
            await message.answer("For a private channel, grant the bot invite-link permission or use a public channel.")
            return
        existing = await db.owned_channels.find_one({"_id": chat.id})
        if existing and existing.get("owner_id") != message.from_user.id:
            await state.clear()
            await message.answer("This channel is already managed by another account.")
            return
        await db.owned_channels.update_one(
            {"_id": chat.id},
            {
                "$set": {
                    "owner_id": message.from_user.id,
                    "title": chat.title or "Channel",
                    "username": chat.username or "",
                    "invite_link": link,
                    "updated_at": now_utc(),
                },
                "$setOnInsert": {"created_at": now_utc()},
            },
            upsert=True,
        )
        await state.clear()
        await message.answer(
            f"{heading('Channel Connected')}\n\n<b>{esc(chat.title or 'Channel')}</b> is ready for giveaways.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="My Channels", callback_data="menu:channels")],
                [ui_button(text="New Giveaway", callback_data="menu:giveaways")],
            ]),
        )
        return

    if mode == "force":
        ok, reason = await check_bot_admin(chat.id, require_post=False)
        if not ok:
            await message.answer(esc(reason) + " The bot must be an administrator to verify membership and detect leave events.")
            return
        link = await obtain_invite_link(chat)
        if not chat.username and not link:
            await message.answer("For a private force-join channel, grant the bot permission to create invite links.")
            return
        await db.force_channels.update_one(
            {"_id": chat.id},
            {
                "$set": {
                    "title": chat.title or "Channel",
                    "username": chat.username or "",
                    "invite_link": link,
                    "updated_at": now_utc(),
                },
                "$setOnInsert": {"created_at": now_utc()},
            },
            upsert=True,
        )
        await state.clear()
        await message.answer(
            f"{heading('Force Channel Added')}\n\n<b>{esc(chat.title or 'Channel')}</b> is now in the membership-check list.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Force Channels", callback_data="admin:force")],
                [ui_button(text="Admin Panel", callback_data="admin:home")],
            ]),
        )
        return

    await state.clear()
    await message.answer("Channel setup was cancelled. Use /admin or /start to continue.")


@router.callback_query(F.data.startswith("channel:view:"))
async def cb_channel_view(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    try:
        channel_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    channel = await db.owned_channels.find_one({"_id": channel_id, "owner_id": callback.from_user.id})
    if not channel:
        await safe_edit(callback.message, "Channel not found.", simple_keyboard(("Back", "menu:channels")))
        return
    try:
        member_count: Any = await bot.get_chat_member_count(channel_id)
    except Exception:
        member_count = "Unavailable"
    uname = "@" + channel["username"] if channel.get("username") else "Private"
    text = (
        f"{heading('Channel Details')}\n\n"
        f"<b>Title:</b> {esc(channel.get('title'))}\n"
        f"<b>Channel ID:</b> <code>{channel_id}</code>\n"
        f"<b>Username:</b> {esc(uname)}\n"
        f"<b>Members:</b> {esc(member_count)}\n"
        f"<b>Connected:</b> {date_label(channel.get('created_at'))}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Remove Channel", callback_data=f"channel:remove:{channel_id}")],
        [ui_button(text="Back", callback_data="menu:channels")],
    ])
    await safe_edit(callback.message, text, keyboard)


@router.callback_query(F.data.startswith("channel:remove:"))
async def cb_channel_remove(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    try:
        channel_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    channel = await db.owned_channels.find_one({"_id": channel_id, "owner_id": callback.from_user.id})
    if not channel:
        await safe_edit(callback.message, "Channel not found.", simple_keyboard(("Back", "menu:channels")))
        return
    active = await db.giveaways.count_documents({
        "channel_id": channel_id,
        "status": {"$in": ["draft", "starting", "active", "paused", "ending"]},
    })
    if active:
        await safe_edit(
            callback.message,
            "This channel has a draft or running giveaway. End or delete it before disconnecting the channel.",
            simple_keyboard(("Back", "channel:view:" + str(channel_id))),
        )
        return
    await db.owned_channels.delete_one({"_id": channel_id, "owner_id": callback.from_user.id})
    await safe_edit(callback.message, "Channel disconnected.", simple_keyboard(("My Channels", "menu:channels")))


# ---------------------------------------------------------------------------
# Giveaways: create, list, settings, start, end, delete
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "menu:giveaways")
async def cb_giveaway_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="New Giveaway", callback_data="gw:new")],
        [ui_button(text="My Giveaways", callback_data="gw:list")],
        [ui_button(text="Back", callback_data="menu:home")],
    ])
    await safe_edit(callback.message, f"{heading('Giveaway Manager')}\n\nCreate a giveaway or open one you already manage.", keyboard)


@router.callback_query(F.data == "gw:new")
async def cb_new_giveaway(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    channels = await db.owned_channels.find({"owner_id": callback.from_user.id}).sort("created_at", -1).limit(30).to_list(length=30)
    if not channels:
        await safe_edit(
            callback.message,
            "Connect a channel before creating a giveaway.",
            InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Add Bot To Channel", url=add_channel_url(), style="success")],
                [ui_button(text="Connect Existing Channel", callback_data="channel:add")],
                [ui_button(text="Back", callback_data="menu:giveaways")],
            ]),
        )
        return
    rows = [[ui_button(text=clean_button_label(ch.get("title") or ch["_id"]), callback_data=f"gw:new-channel:{ch['_id']}")] for ch in channels]
    rows.extend([
        [ui_button(text="Add Another Channel", callback_data="channel:add")],
        [ui_button(text="Back", callback_data="menu:giveaways")],
    ])
    await safe_edit(callback.message, f"{heading('Choose A Channel')}\n\nSelect where the giveaway should be posted.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("gw:new-channel:"))
async def cb_create_giveaway(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    try:
        channel_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    channel = await db.owned_channels.find_one({"_id": channel_id, "owner_id": callback.from_user.id})
    if not channel:
        await safe_edit(callback.message, "Channel not found.", simple_keyboard(("Back", "gw:new")))
        return
    existing = await db.giveaways.find_one({
        "creator_id": callback.from_user.id,
        "channel_id": channel_id,
        "status": {"$in": ["draft", "starting", "active", "paused", "ending"]},
    })
    if existing:
        await safe_edit(
            callback.message,
            "There is already a draft or running giveaway on this channel. Open it to continue.",
            simple_keyboard(("Open Existing", f"gw:manage:{existing['_id']}"), ("Back", "gw:new")),
        )
        return
    settings = await global_settings()
    record = {
        "creator_id": callback.from_user.id,
        "channel_id": channel_id,
        "channel_title": channel.get("title", "Channel"),
        "channel_username": channel.get("username", ""),
        "channel_invite_link": channel.get("invite_link", ""),
        "status": "draft",
        "vote_emoji": "👍",
        "target_members": 0,
        "start_member_count": None,
        "votes_per_account": int(settings.get("votes_per_account_default", 1)),
        "winner_places": 1,
        "anti_leave": bool(settings.get("anti_leave_default", True)),
        "created_at": now_utc(),
        "started_at": None,
        "ended_at": None,
        "start_post_id": None,
        "result_post_id": None,
        "result_announced": False,
        "winners": [],
    }
    result = await db.giveaways.insert_one(record)
    await show_giveaway_manage(callback.message, result.inserted_id, callback.from_user.id)


@router.callback_query(F.data == "gw:list")
async def cb_my_giveaways(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    cursor = db.giveaways.find({"creator_id": callback.from_user.id}).sort("created_at", -1).limit(20)
    giveaways = await cursor.to_list(length=20)
    if not giveaways:
        await safe_edit(
            callback.message,
            "You have not created a giveaway yet.",
            simple_keyboard(("New Giveaway", "gw:new"), ("Back", "menu:giveaways")),
        )
        return
    rows: list[list[InlineKeyboardButton]] = []
    for item in giveaways:
        label = f"#{str(item['_id'])[-6:]} | {item.get('channel_title', 'Channel')} | {item.get('status', 'draft').title()}"
        rows.append([ui_button(text=clean_button_label(label, 64), callback_data=f"gw:manage:{item['_id']}")])
    rows.append([ui_button(text="Back", callback_data="menu:giveaways")])
    await safe_edit(callback.message, f"{heading('My Giveaways')}\n\nSelect a giveaway to manage.", InlineKeyboardMarkup(inline_keyboard=rows))


async def show_giveaway_manage(message: Message, giveaway_id: ObjectId, viewer_id: int) -> None:
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != viewer_id and not is_admin(viewer_id)):
        await safe_edit(message, "Giveaway not found or access denied.", simple_keyboard(("Back", "gw:list")))
        return
    participants = await db.participants.find({"giveaway_id": giveaway_id}, {"vote_count": 1}).to_list(length=10000)
    participant_count = len(participants)
    total_votes = sum(int(item.get("vote_count", 0)) for item in participants)
    status = giveaway.get("status", "draft").title()
    target = giveaway.get("target_members") or 0
    target_text = f"{target} members" if target else "Not Set"
    start_count = giveaway.get("start_member_count")
    started_text = f"{start_count} members" if start_count is not None else "Not Started"
    text = (
        f"{heading('Giveaway ' + str(giveaway_id)[-6:])}\n\n"
        f"<b>Channel:</b> {esc(giveaway.get('channel_title', 'Channel'))}\n"
        f"<b>Status:</b> {esc(status)}\n"
        f"<b>Started At:</b> {esc(started_text)}\n"
        f"<b>Auto-End At:</b> {esc(target_text)}\n"
        "<b>Vote Button:</b> Configured\n"
        f"<b>Votes Per Telegram Account:</b> {int(giveaway.get('votes_per_account', 1))}\n"
        f"<b>Winner Places:</b> {int(giveaway.get('winner_places', 1))}\n"
        f"<b>Anti-Leave:</b> {'On' if giveaway.get('anti_leave', True) else 'Off'}\n"
        f"<b>Participants:</b> {participant_count}\n"
        f"<b>Total Votes:</b> {total_votes}"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if giveaway.get("status") == "draft":
        rows.append([ui_button(text="Start Giveaway", callback_data=f"gw:start:{giveaway_id}")])
    if giveaway.get("status") in {"draft", "active", "paused"}:
        rows.append([ui_button(text="Giveaway Settings", callback_data=f"gw:settings:{giveaway_id}")])
    if giveaway.get("status") == "active":
        link = f"https://t.me/{bot_username}?start=join-{giveaway_id}"
        paid_vote_link = f"https://t.me/{bot_username}?start=paidvote-{giveaway_id}"
        text += f"\n\n<b>Participation Link:</b>\n<code>{esc(link)}</code>\n<b>Paid Vote Link:</b>\n<code>{esc(paid_vote_link)}</code>"
        rows.append([ui_button(text="Participate", url=link, style="success"), ui_button(text="Paid Vote", url=paid_vote_link)])
    if giveaway.get("status") in {"draft", "ended"}:
        rows.append([ui_button(text="Delete Giveaway", callback_data=f"gw:delete:{giveaway_id}")])
    rows.append([ui_button(text="Back", callback_data="gw:list")])
    await safe_edit(message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("gw:manage:"))
async def cb_manage_giveaway(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if giveaway_id:
        await show_giveaway_manage(callback.message, giveaway_id, callback.from_user.id)


@router.callback_query(F.data.startswith("gw:settings:"))
async def cb_giveaway_settings(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        await safe_edit(callback.message, "Giveaway not found or access denied.", simple_keyboard(("Back", "gw:list")))
        return
    rows = [
        [ui_button(text="Set Auto-End Member Count", callback_data=f"gw:target:{giveaway_id}")],
        [ui_button(text="Change Vote Emoji", callback_data=f"gw:emoji:{giveaway_id}")],
        [ui_button(text=f"Votes Per Account: {int(giveaway.get('votes_per_account', 1))}", callback_data=f"gw:votes:{giveaway_id}")],
        [ui_button(text=f"Winner Places: {int(giveaway.get('winner_places', 1))}", callback_data=f"gw:winners:{giveaway_id}")],
        [ui_button(text=f"Anti-Leave: {'On' if giveaway.get('anti_leave', True) else 'Off'}", callback_data=f"gw:anti:{giveaway_id}")],
    ]
    if giveaway.get("status") in {"active", "paused"}:
        rows.append([ui_button(text="End Giveaway", callback_data=f"gw:end:{giveaway_id}")])
    rows.append([ui_button(text="Back", callback_data=f"gw:manage:{giveaway_id}")])
    text = (
        f"{heading('Giveaway Settings')}\n\n"
        "Choose what to change. Vote limits apply per Telegram account, not per physical device."
    )
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("gw:target:"))
async def cb_set_target(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    await state.set_state(TargetMembersState.waiting_target)
    await state.update_data(giveaway_id=str(giveaway_id))
    await safe_edit(
        callback.message,
        f"{heading('Set Auto-End Target')}\n\n"
        "Send the channel member count at which this giveaway should end.\n"
        "Send 0 to disable automatic ending.",
        simple_keyboard(("Cancel", "cancel")),
    )


@router.message(TargetMembersState.waiting_target)
async def handle_target_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    giveaway_id = oid(data.get("giveaway_id"))
    try:
        target = int((message.text or "").strip())
        if target < 0 or target > 2_000_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Send a whole number from 0 to 2,000,000,000.")
        return
    if not giveaway_id:
        await state.clear()
        await message.answer("Giveaway not found. Use /start to return to the menu.")
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != message.from_user.id and not is_admin(message.from_user.id)):
        await state.clear()
        await message.answer("Giveaway not found or access denied.")
        return
    await db.giveaways.update_one({"_id": giveaway_id}, {"$set": {"target_members": target, "updated_at": now_utc()}})
    await state.clear()
    response = f"Auto-end target set to <b>{target}</b> members." if target else "Automatic ending is disabled."
    await message.answer(response, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Back To Settings", callback_data=f"gw:settings:{giveaway_id}")]
    ]))


@router.callback_query(F.data.startswith("gw:emoji:"))
async def cb_set_emoji(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    await state.set_state(VoteEmojiState.waiting_emoji)
    await state.update_data(giveaway_id=str(giveaway_id))
    await safe_edit(
        callback.message,
        f"{heading('Change Vote Emoji')}\n\nSend one emoji to show on the vote-count button. It will appear only on that button.",
        simple_keyboard(("Cancel", "cancel")),
    )


@router.message(VoteEmojiState.waiting_emoji)
async def handle_emoji_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    giveaway_id = oid(data.get("giveaway_id"))
    emoji = (message.text or "").strip()
    valid = bool(emoji) and len(emoji) <= 12 and not any(ch.isspace() for ch in emoji) and any(ord(ch) > 0x2000 for ch in emoji)
    if not valid:
        await message.answer("Send one emoji only. It will be used on the vote-count button.")
        return
    if not giveaway_id:
        await state.clear()
        await message.answer("Giveaway not found. Use /start to return to the menu.")
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != message.from_user.id and not is_admin(message.from_user.id)):
        await state.clear()
        await message.answer("Giveaway not found or access denied.")
        return
    await db.giveaways.update_one({"_id": giveaway_id}, {"$set": {"vote_emoji": emoji, "updated_at": now_utc()}})
    await state.clear()
    await message.answer("Vote emoji updated.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Back To Settings", callback_data=f"gw:settings:{giveaway_id}")]
    ]))


@router.callback_query(F.data.startswith("gw:votes:"))
async def cb_set_vote_limit(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    values = callback.data.split(":")
    if len(values) != 3:
        return
    giveaway_id = oid(values[2])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    rows = [[ui_button(text=f"{amount} Vote{'s' if amount != 1 else ''} Per Account", callback_data=f"gw:set-votes:{giveaway_id}:{amount}")] for amount in (1, 2, 3)]
    rows.append([ui_button(text="Back", callback_data=f"gw:settings:{giveaway_id}")])
    await safe_edit(callback.message, f"{heading('Votes Per Telegram Account')}\n\nSelect a maximum of 1, 2, or 3 active votes per Telegram account in this giveaway.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("gw:set-votes:"))
async def cb_apply_vote_limit(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    values = callback.data.split(":")
    if len(values) != 4:
        return
    giveaway_id, amount = oid(values[2]), values[3]
    if not giveaway_id or amount not in {"1", "2", "3"}:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    await db.giveaways.update_one({"_id": giveaway_id}, {"$set": {"votes_per_account": int(amount), "updated_at": now_utc()}})
    await safe_edit(callback.message, f"Maximum votes per Telegram account set to <b>{amount}</b>.", simple_keyboard(("Back To Settings", f"gw:settings:{giveaway_id}")))


@router.callback_query(F.data.startswith("gw:winners:"))
async def cb_set_winner_places(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    rows = [[ui_button(text=f"{amount} Winner Place{'s' if amount != 1 else ''}", callback_data=f"gw:set-winners:{giveaway_id}:{amount}")] for amount in (1, 2, 3)]
    rows.append([ui_button(text="Back", callback_data=f"gw:settings:{giveaway_id}")])
    await safe_edit(callback.message, f"{heading('Winner Places')}\n\nSelect how many ranked winners to announce: 1st, 1st–2nd, or 1st–3rd.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("gw:set-winners:"))
async def cb_apply_winner_places(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    values = callback.data.split(":")
    if len(values) != 4:
        return
    giveaway_id, amount = oid(values[2]), values[3]
    if not giveaway_id or amount not in {"1", "2", "3"}:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    await db.giveaways.update_one({"_id": giveaway_id}, {"$set": {"winner_places": int(amount), "updated_at": now_utc()}})
    await safe_edit(callback.message, f"The giveaway will announce up to <b>{amount}</b> ranked winner(s).", simple_keyboard(("Back To Settings", f"gw:settings:{giveaway_id}")))


@router.callback_query(F.data.startswith("gw:anti:"))
async def cb_toggle_anti_leave(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    new_value = not bool(giveaway.get("anti_leave", True))
    await db.giveaways.update_one({"_id": giveaway_id}, {"$set": {"anti_leave": new_value, "updated_at": now_utc()}})
    label = "On" if new_value else "Off"
    await safe_edit(callback.message, f"Anti-Leave is now <b>{label}</b>.", simple_keyboard(("Back To Settings", f"gw:settings:{giveaway_id}")))


@router.callback_query(F.data.startswith("gw:start:"))
async def cb_start_giveaway(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        await safe_edit(callback.message, "Giveaway not found or access denied.", simple_keyboard(("Back", "gw:list")))
        return
    if giveaway.get("status") != "draft":
        await safe_edit(callback.message, "Only a draft giveaway can be started.", simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    owner_id = int(giveaway.get("creator_id", 0))
    if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
        await safe_edit(
            callback.message,
            f"{heading('Premium Required')}\n\nAn active Premium plan is required to start giveaways.",
            simple_keyboard(("Premium Plans", "menu:premium"), ("Back", f"gw:manage:{giveaway_id}")),
        )
        return
    ok, reason = await check_bot_admin(giveaway["channel_id"], require_post=True)
    if not ok:
        await safe_edit(callback.message, esc(reason), simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    try:
        member_count = await bot.get_chat_member_count(giveaway["channel_id"])
    except Exception:
        await safe_edit(callback.message, "Could not read the channel member count. Check bot admin permissions.", simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    target = int(giveaway.get("target_members", 0) or 0)
    if target and target <= member_count:
        await safe_edit(
            callback.message,
            f"The target ({target}) is not above the current member count ({member_count}). Set a higher target or disable auto-end.",
            simple_keyboard(("Settings", f"gw:settings:{giveaway_id}"), ("Back", f"gw:manage:{giveaway_id}")),
        )
        return
    claimed = await db.giveaways.update_one(
        {"_id": giveaway_id, "status": "draft"},
        {"$set": {"status": "starting", "start_member_count": member_count, "starting_at": now_utc()}},
    )
    if not claimed.modified_count:
        await safe_edit(callback.message, "This giveaway is already being started or is no longer a draft.", simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    link = f"https://t.me/{bot_username}?start=join-{giveaway_id}"
    paid_vote_link = f"https://t.me/{bot_username}?start=paidvote-{giveaway_id}"
    target_text = f"{target} members" if target else "Manual End"
    announcement = (
        f"{heading('Giveaway ' + str(giveaway_id)[-6:])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Voting is now open.\n\n"
        f"<b>Started At:</b> {member_count} members\n"
        f"<b>Auto-End At:</b> {esc(target_text)}\n"
        f"<b>Winner Places:</b> {int(giveaway.get('winner_places', 1))}\n"
        f"<b>Votes Per Telegram Account:</b> {int(giveaway.get('votes_per_account', 1))}\n\n"
        f'<b>Participation Link:</b> <a href="{esc(link)}">Submit An Entry</a>\n'
        f'<b>Paid Vote Link:</b> <a href="{esc(paid_vote_link)}">Buy Votes</a>'
    )
    try:
        announcement_message = await bot.send_message(
            giveaway["channel_id"],
            announcement,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Participate", url=link, style="success"), ui_button(text="Paid Vote", url=paid_vote_link, style="primary")]
            ]),
        )
    except Exception:
        await db.giveaways.update_one({"_id": giveaway_id, "status": "starting"}, {"$set": {"status": "draft"}})
        await safe_edit(callback.message, "The giveaway post could not be sent. Check that the bot can post in the channel.", simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    await db.giveaways.update_one(
        {"_id": giveaway_id, "status": "starting"},
        {"$set": {"status": "active", "started_at": now_utc(), "start_post_id": announcement_message.message_id}},
    )
    await notify_admins_giveaway_started(giveaway, member_count)
    await safe_edit(
        callback.message,
        f"{heading('Giveaway Started')}\n\n"
        f"Started At: <b>{member_count}</b> members\n"
        f"Auto-End At: <b>{esc(target_text)}</b>\n\n"
        "The announcement is live in your channel.",
        InlineKeyboardMarkup(inline_keyboard=[
            [ui_button(text="Participate", url=link, style="success"), ui_button(text="Paid Vote", url=paid_vote_link, style="primary")],
            [ui_button(text="Manage Giveaway", callback_data=f"gw:manage:{giveaway_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("gw:end:"))
async def cb_end_prompt(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Confirm End", callback_data=f"gw:end-confirm:{giveaway_id}")],
        [ui_button(text="Cancel", callback_data=f"gw:settings:{giveaway_id}")],
    ])
    await safe_edit(callback.message, f"End giveaway #{str(giveaway_id)[-6:]} now? Voting will close and the ranked winners will be announced.", keyboard)


@router.callback_query(F.data.startswith("gw:end-confirm:"))
async def cb_end_confirm(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    ended = await end_giveaway(giveaway_id)
    if ended:
        label = "Giveaway ended and results announced."
    elif await pending_paid_vote_orders(giveaway_id):
        label = "A paid-vote payment is still being verified. The giveaway will remain open until it is settled or expires."
    else:
        label = "This giveaway is no longer active."
    await safe_edit(callback.message, label, simple_keyboard(("View Giveaway", f"gw:manage:{giveaway_id}")))


@router.callback_query(F.data.startswith("gw:delete:"))
async def cb_delete_prompt(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    if giveaway.get("status") in {"active", "paused", "starting", "ending"}:
        await safe_edit(callback.message, "End the giveaway before deleting it.", simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Confirm Delete", callback_data=f"gw:delete-confirm:{giveaway_id}")],
        [ui_button(text="Cancel", callback_data=f"gw:manage:{giveaway_id}")],
    ])
    await safe_edit(callback.message, "Delete this giveaway and its entries and votes? This cannot be undone.", keyboard)


@router.callback_query(F.data.startswith("gw:delete-confirm:"))
async def cb_delete_confirm(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or (giveaway.get("creator_id") != callback.from_user.id and not is_admin(callback.from_user.id)):
        return
    if giveaway.get("status") in {"active", "paused", "starting", "ending"}:
        await safe_edit(callback.message, "End the giveaway before deleting it.", simple_keyboard(("Back", f"gw:manage:{giveaway_id}")))
        return
    await delete_giveaway(giveaway)
    await safe_edit(callback.message, "Giveaway and its entries were deleted.", simple_keyboard(("My Giveaways", "gw:list")))


async def delete_giveaway(giveaway: dict[str, Any]) -> None:
    giveaway_id = giveaway["_id"]
    participants = await db.participants.find({"giveaway_id": giveaway_id}).to_list(length=10000)
    for participant in participants:
        votes = await db.votes.find({"giveaway_id": giveaway_id, "participant_id": participant["_id"]}).to_list(length=10000)
        for vote in votes:
            await release_vote_slot(giveaway_id, vote["voter_id"])
        if participant.get("message_id"):
            try:
                await bot.delete_message(giveaway["channel_id"], participant["message_id"])
            except Exception:
                pass
    if giveaway.get("start_post_id"):
        try:
            await bot.delete_message(giveaway["channel_id"], giveaway["start_post_id"])
        except Exception:
            pass
    await db.votes.delete_many({"giveaway_id": giveaway_id})
    await db.participants.delete_many({"giveaway_id": giveaway_id})
    await db.voter_quotas.delete_many({"giveaway_id": giveaway_id})
    await db.giveaways.delete_one({"_id": giveaway_id})


# ---------------------------------------------------------------------------
# Participation and entry posts
# ---------------------------------------------------------------------------
async def handle_join_giveaway(message: Message, state: FSMContext, giveaway_id: ObjectId) -> None:
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway:
        await message.answer("This giveaway does not exist.")
        return
    if giveaway.get("status") != "active":
        await message.answer("This giveaway is not accepting entries right now.")
        return
    owner_id = int(giveaway.get("creator_id", 0))
    if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
        await pause_giveaway_for_expired_plan(giveaway)
        await message.answer("This giveaway is temporarily paused while the owner renews Premium.")
        return
    existing = await db.participants.find_one({"giveaway_id": giveaway_id, "user_id": message.from_user.id})
    if existing:
        await message.answer(
            f"{heading('Entry Already Submitted')}\n\n"
            f"<b>Your Name:</b> {esc(existing.get('candidate_name'))}\n\n"
            "One entry is allowed per Telegram account. To submit a different name, delete your old entry first.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ui_button(text="Delete Old Entry And Re-Enter", callback_data=f"entry:replace:{giveaway_id}")],
                [ui_button(text="Main Menu", callback_data="menu:home")],
            ]),
        )
        return
    await state.set_state(CandidateNameState.waiting_name)
    await state.update_data(giveaway_id=str(giveaway_id))
    await message.answer(
        f"{heading('Submit Your Entry')}\n\nSend the one name you want displayed in the giveaway channel.\nYou can submit only one name per giveaway.",
        reply_markup=simple_keyboard(("Cancel", "cancel")),
    )


@router.callback_query(F.data.startswith("entry:replace:"))
async def cb_replace_entry(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or giveaway.get("status") != "active":
        await safe_edit(callback.message, "This giveaway is no longer active.", simple_keyboard(("Main Menu", "menu:home")))
        return
    owner_id = int(giveaway.get("creator_id", 0))
    if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
        await pause_giveaway_for_expired_plan(giveaway)
        await safe_edit(callback.message, "This giveaway is temporarily paused while the owner renews Premium.", simple_keyboard(("Main Menu", "menu:home")))
        return
    async with gw_lock(giveaway_id):
        fresh_giveaway = await db.giveaways.find_one({"_id": giveaway_id})
        if not fresh_giveaway or fresh_giveaway.get("status") != "active":
            await safe_edit(callback.message, "This giveaway is no longer active.", simple_keyboard(("Main Menu", "menu:home")))
            return
        existing = await db.participants.find_one({"giveaway_id": giveaway_id, "user_id": callback.from_user.id})
        if existing:
            await delete_participant(existing, fresh_giveaway)
    await state.set_state(CandidateNameState.waiting_name)
    await state.update_data(giveaway_id=str(giveaway_id))
    await safe_edit(callback.message, "Your old entry was deleted. Send the new name you want to display.", simple_keyboard(("Cancel", "cancel")))


async def delete_participant(participant: dict[str, Any], giveaway: dict[str, Any]) -> None:
    votes = await db.votes.find({"giveaway_id": giveaway["_id"], "participant_id": participant["_id"]}).to_list(length=10000)
    for vote in votes:
        await release_vote_slot(giveaway["_id"], vote["voter_id"])
    await db.votes.delete_many({"giveaway_id": giveaway["_id"], "participant_id": participant["_id"]})
    if participant.get("message_id"):
        try:
            await bot.delete_message(giveaway["channel_id"], participant["message_id"])
        except Exception:
            pass
    await db.participants.delete_one({"_id": participant["_id"]})


@router.message(CandidateNameState.waiting_name)
async def handle_candidate_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    giveaway_id = oid(data.get("giveaway_id"))
    name = " ".join((message.text or "").split())
    if not name or len(name) > 80:
        await message.answer("Send a name between 1 and 80 characters. New lines are not allowed.")
        return
    if not giveaway_id:
        await state.clear()
        await message.answer("Giveaway not found. Use /start to return to the menu.")
        return
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    if not giveaway or giveaway.get("status") != "active":
        await state.clear()
        await message.answer("This giveaway is no longer active.")
        return
    owner_id = int(giveaway.get("creator_id", 0))
    if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
        await pause_giveaway_for_expired_plan(giveaway)
        await state.clear()
        await message.answer("This giveaway is temporarily paused while the owner renews Premium.")
        return
    existing = await db.participants.find_one({"giveaway_id": giveaway_id, "user_id": message.from_user.id})
    if existing:
        await state.clear()
        await message.answer("You already have an entry in this giveaway.")
        return

    participant_doc = {
        "giveaway_id": giveaway_id,
        "user_id": message.from_user.id,
        "candidate_name": name,
        "message_id": None,
        "vote_count": 0,
        "post_ui_version": 2,
        "created_at": now_utc(),
    }
    try:
        inserted = await db.participants.insert_one(participant_doc)
    except DuplicateKeyError:
        await state.clear()
        await message.answer("You already have an entry in this giveaway.")
        return
    participant = {**participant_doc, "_id": inserted.inserted_id}
    try:
        channel_post = await bot.send_message(
            giveaway["channel_id"],
            f"<b>{esc(smallcaps_title(name))}</b>",
            reply_markup=participant_keyboard(giveaway, participant),
        )
    except Exception:
        await db.participants.delete_one({"_id": inserted.inserted_id})
        await state.clear()
        await message.answer("The entry could not be posted. Ask the channel owner to check bot permissions, then try again.")
        return
    await db.participants.update_one({"_id": inserted.inserted_id}, {"$set": {"message_id": channel_post.message_id}})
    participant["message_id"] = channel_post.message_id
    await state.clear()

    p_link = post_url(giveaway["channel_id"], giveaway.get("channel_username", ""), channel_post.message_id)
    c_link = channel_url(giveaway["channel_id"], giveaway.get("channel_username", ""), giveaway.get("channel_invite_link", ""))
    links = []
    if p_link:
        links.append(f"<b>Post Link:</b> <a href=\"{esc(p_link)}\">Open Your Post</a>")
    else:
        links.append(f"<b>Post:</b> Message {channel_post.message_id} in your giveaway channel.")
    if c_link:
        links.append(f"<b>Channel Link:</b> <a href=\"{esc(c_link)}\">Open Channel</a>")
    else:
        links.append("<b>Channel Link:</b> Ask the channel owner for an invite link.")
    await message.answer(
        f"{heading('Entry Published')}\n\n"
        f"<b>Name:</b> {esc(name)}\n\n"
        + "\n".join(links)
        + "\n\nShare your post so people can find it and vote.",
        reply_markup=simple_keyboard(("Main Menu", "menu:home")),
    )
    await notify_owner_new_participant(
        giveaway,
        participant,
        message.from_user,
        p_link,
        c_link,
    )


# ---------------------------------------------------------------------------
# Membership checks and voting
# ---------------------------------------------------------------------------
def member_is_present(member: Any) -> bool:
    if member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def required_channels(giveaway: dict[str, Any]) -> list[dict[str, Any]]:
    settings = await global_settings()
    channels: dict[int, dict[str, Any]] = {}
    if settings.get("force_join_enabled"):
        cursor = db.force_channels.find({}).sort("created_at", 1)
        index = 0
        async for channel in cursor:
            index += 1
            channels[int(channel["_id"])] = {**channel, "label": f"Required Channel {index}"}
    target_id = int(giveaway["channel_id"])
    channels[target_id] = {
        "_id": target_id,
        "title": giveaway.get("channel_title", "Giveaway Channel"),
        "username": giveaway.get("channel_username", ""),
        "invite_link": giveaway.get("channel_invite_link", ""),
        "label": "Giveaway Channel",
    }
    return list(channels.values())


async def missing_memberships(user_id: int, giveaway: dict[str, Any]) -> list[dict[str, Any]]:
    assert bot is not None
    missing: list[dict[str, Any]] = []
    for channel in await required_channels(giveaway):
        try:
            member = await bot.get_chat_member(int(channel["_id"]), user_id)
            if not member_is_present(member):
                missing.append(channel)
        except Exception:
            # Fail closed. If the bot cannot verify membership, it must not count a vote.
            missing.append(channel)
    return missing


async def verification_flow(target: Message | CallbackQuery, user: types.User, giveaway_id: ObjectId, participant_id: ObjectId) -> None:
    giveaway = await db.giveaways.find_one({"_id": giveaway_id})
    participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id})
    if not giveaway or not participant or giveaway.get("status") != "active":
        text = "This giveaway entry is no longer available for voting."
        if isinstance(target, CallbackQuery):
            await safe_edit(target.message, text, simple_keyboard(("Main Menu", "menu:home")))
        else:
            await target.answer(text)
        return
    if participant.get("user_id") == user.id:
        text = "You cannot vote for your own entry."
        if isinstance(target, CallbackQuery):
            await safe_edit(target.message, text, simple_keyboard(("Main Menu", "menu:home")))
        else:
            await target.answer(text)
        return
    existing_vote = await db.votes.find_one({"giveaway_id": giveaway_id, "participant_id": participant_id, "voter_id": user.id})
    if existing_vote:
        text = "Your vote for this entry is already counted."
        if isinstance(target, CallbackQuery):
            await safe_edit(target.message, text, simple_keyboard(("Main Menu", "menu:home")))
        else:
            await target.answer(text, reply_markup=simple_keyboard(("Main Menu", "menu:home")))
        return
    missing = await missing_memberships(user.id, giveaway)
    if missing:
        rows: list[list[InlineKeyboardButton]] = []
        for index, channel in enumerate(missing, 1):
            link = channel_url(int(channel["_id"]), channel.get("username", ""), channel.get("invite_link", ""))
            if link:
                label = "Join Giveaway Channel" if int(channel["_id"]) == int(giveaway["channel_id"]) else f"Join Required Channel {index}"
                rows.append([ui_button(text=label[:64], url=link)])
        rows.append([ui_button(text="Check Membership", callback_data=f"vc:{giveaway_id}:{participant_id}")])
        rows.append([ui_button(text="Main Menu", callback_data="menu:home")])
        text = (
            f"{heading('Voter Verification')}\n\n"
            "Join every channel below, then tap Check Membership. Your vote is counted only after membership is confirmed."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        if isinstance(target, CallbackQuery):
            await safe_edit(target.message, text, keyboard)
        else:
            await target.answer(text, reply_markup=keyboard)
        return

    result, count, updated_giveaway, updated_participant = await cast_vote(user, participant_id, allow_toggle=False)
    if result == "added":
        text = f"{heading('Vote Verified')}\n\nYour vote has been counted.\n<b>Entry:</b> {esc(updated_participant.get('candidate_name'))}\n<b>Total Votes:</b> {count}"
    elif result == "limit":
        text = f"{heading('Vote Limit Reached')}\n\nYou have used your maximum of <b>{int(updated_giveaway.get('votes_per_account', 1))}</b> vote(s) for this giveaway."
    elif result == "not_member":
        # Membership can change between the first check and the vote operation.
        await verification_flow(target, user, giveaway_id, participant_id)
        return
    elif result == "self":
        text = "You cannot vote for your own entry."
    elif result == "ended":
        text = "This giveaway is no longer accepting votes."
    elif result == "removed":
        text = f"Your vote was removed. The entry now has <b>{count}</b> vote(s)."
    else:
        text = "Your vote is already counted."
    if isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, simple_keyboard(("Main Menu", "menu:home")))
    else:
        await target.answer(text, reply_markup=simple_keyboard(("Main Menu", "menu:home")))


@router.callback_query(F.data.startswith("vc:"))
async def cb_check_membership(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    values = callback.data.split(":")
    if len(values) != 3:
        return
    giveaway_id, participant_id = oid(values[1]), oid(values[2])
    if giveaway_id and participant_id:
        await verification_flow(callback, callback.from_user, giveaway_id, participant_id)


async def reserve_vote_slot(giveaway_id: ObjectId, voter_id: int, limit: int) -> bool:
    key = f"{giveaway_id}:{voter_id}"
    result = await db.voter_quotas.update_one(
        {"_id": key, "used": {"$lt": limit}},
        {"$inc": {"used": 1}, "$set": {"updated_at": now_utc()}},
    )
    if result.modified_count == 1:
        return True
    existing = await db.voter_quotas.find_one({"_id": key}, {"used": 1})
    if existing:
        return False
    try:
        await db.voter_quotas.insert_one({
            "_id": key,
            "giveaway_id": giveaway_id,
            "voter_id": voter_id,
            "used": 1,
            "updated_at": now_utc(),
        })
        return True
    except DuplicateKeyError:
        result = await db.voter_quotas.update_one(
            {"_id": key, "used": {"$lt": limit}},
            {"$inc": {"used": 1}, "$set": {"updated_at": now_utc()}},
        )
        return result.modified_count == 1


async def release_vote_slot(giveaway_id: ObjectId, voter_id: int) -> None:
    key = f"{giveaway_id}:{voter_id}"
    await db.voter_quotas.update_one(
        {"_id": key, "used": {"$gt": 0}},
        {"$inc": {"used": -1}, "$set": {"updated_at": now_utc()}},
    )


async def cast_vote(
    user: types.User,
    participant_id: ObjectId,
    *,
    allow_toggle: bool = True,
) -> tuple[str, int, dict[str, Any], dict[str, Any]]:
    initial = await db.participants.find_one({"_id": participant_id})
    if not initial:
        return "missing", 0, {}, {}
    giveaway_id = initial["giveaway_id"]
    async with gw_lock(giveaway_id):
        giveaway = await db.giveaways.find_one({"_id": giveaway_id}) or {}
        participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id}) or {}
        if not participant or not giveaway:
            return "missing", 0, giveaway, participant
        if giveaway.get("status") == "paused":
            return "paused", int(participant.get("vote_count", 0)), giveaway, participant
        if giveaway.get("status") != "active":
            return "ended", int(participant.get("vote_count", 0)), giveaway, participant
        owner_id = int(giveaway.get("creator_id", 0))
        if owner_id and not is_admin(owner_id) and not await has_active_premium(owner_id):
            await pause_giveaway_for_expired_plan(giveaway)
            giveaway = {**giveaway, "status": "paused"}
            return "paused", int(participant.get("vote_count", 0)), giveaway, participant
        if participant.get("user_id") == user.id:
            return "self", int(participant.get("vote_count", 0)), giveaway, participant
        if await missing_memberships(user.id, giveaway):
            return "not_member", int(participant.get("vote_count", 0)), giveaway, participant

        existing = await db.votes.find_one({
            "giveaway_id": giveaway_id,
            "participant_id": participant_id,
            "voter_id": user.id,
        })
        if existing:
            if not allow_toggle:
                return "already", int(participant.get("vote_count", 0)), giveaway, participant
            deleted = await db.votes.delete_one({"_id": existing["_id"]})
            if deleted.deleted_count:
                await db.participants.update_one(
                    {"_id": participant_id, "vote_count": {"$gt": 0}},
                    {"$inc": {"vote_count": -1}},
                )
                await release_vote_slot(giveaway_id, user.id)
                participant = await db.participants.find_one({"_id": participant_id}) or participant
                await update_entry_button(giveaway, participant)
                return "removed", int(participant.get("vote_count", 0)), giveaway, participant
            return "already", int(participant.get("vote_count", 0)), giveaway, participant

        cap = max(1, min(3, int(giveaway.get("votes_per_account", 1))))
        if not await reserve_vote_slot(giveaway_id, user.id, cap):
            return "limit", int(participant.get("vote_count", 0)), giveaway, participant
        try:
            await db.votes.insert_one({
                "giveaway_id": giveaway_id,
                "participant_id": participant_id,
                "voter_id": user.id,
                "created_at": now_utc(),
            })
        except DuplicateKeyError:
            await release_vote_slot(giveaway_id, user.id)
            return "already", int(participant.get("vote_count", 0)), giveaway, participant
        except Exception:
            await release_vote_slot(giveaway_id, user.id)
            raise
        await db.participants.update_one({"_id": participant_id}, {"$inc": {"vote_count": 1}})
        participant = await db.participants.find_one({"_id": participant_id}) or participant
        await update_entry_button(giveaway, participant)
        return "added", int(participant.get("vote_count", 0)), giveaway, participant


async def update_entry_button(giveaway: dict[str, Any], participant: dict[str, Any]) -> None:
    if not participant.get("message_id"):
        return
    assert bot is not None
    try:
        if giveaway.get("status") == "active":
            keyboard = participant_keyboard(giveaway, participant)
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                ui_button(
                    text=f"{giveaway.get('vote_emoji', '👍')} {int(participant.get('vote_count', 0))}",
                    callback_data="v:paused" if giveaway.get("status") == "paused" else "v:ended",
                    style="danger" if giveaway.get("status") == "paused" else "primary",
                )
            ]])
        await bot.edit_message_reply_markup(
            chat_id=giveaway["channel_id"],
            message_id=participant["message_id"],
            reply_markup=keyboard,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Could not refresh entry button: %s", exc)
    except Exception as exc:
        logger.warning("Could not refresh entry button: %s", exc)


async def compact_existing_entry_posts() -> None:
    """One-time cleanup: replace older entry captions with the name only."""
    cursor = db.participants.find({
        "message_id": {"$ne": None},
        "post_ui_version": {"$ne": 2},
    })
    async for participant in cursor:
        giveaway = await db.giveaways.find_one({"_id": participant["giveaway_id"]})
        if not giveaway:
            await db.participants.update_one({"_id": participant["_id"]}, {"$set": {"post_ui_version": 2}})
            continue
        text = f"<b>{esc(smallcaps_title(participant.get('candidate_name', 'Entry')))}</b>"
        if giveaway.get("status") == "active":
            markup = participant_keyboard(giveaway, participant)
        else:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                ui_button(
                    text=f"{giveaway.get('vote_emoji', '👍')} {int(participant.get('vote_count', 0))}",
                    callback_data="v:paused" if giveaway.get("status") == "paused" else "v:ended",
                    style="danger" if giveaway.get("status") == "paused" else "primary",
                )
            ]])
        try:
            await bot.edit_message_text(
                chat_id=giveaway["channel_id"],
                message_id=participant["message_id"],
                text=text,
                reply_markup=markup,
            )
        except Exception as exc:
            logger.debug("Could not compact old entry post %s: %s", participant.get("_id"), exc)
        await db.participants.update_one({"_id": participant["_id"]}, {"$set": {"post_ui_version": 2}})
        await asyncio.sleep(0.03)


@router.callback_query(F.data == "v:ended")
async def cb_vote_ended(callback: CallbackQuery) -> None:
    await safe_answer(callback, "Voting is closed for this giveaway.", alert=True)


@router.callback_query(F.data == "v:paused")
async def cb_vote_paused(callback: CallbackQuery) -> None:
    await safe_answer(callback, "This giveaway is temporarily paused. Voting will resume after the owner renews Premium.", alert=True)


@router.callback_query(F.data.startswith("v:"))
async def cb_vote(callback: CallbackQuery) -> None:
    participant_id = oid(callback.data[2:])
    if not participant_id:
        await safe_answer(callback, "Entry not found.", alert=True)
        return
    await save_user(callback.from_user)
    result, count, giveaway, participant = await cast_vote(callback.from_user, participant_id)
    if result == "added":
        await safe_answer(callback, f"Vote counted. Total: {count}")
    elif result == "removed":
        await safe_answer(callback, f"Vote removed. Total: {count}")
    elif result == "limit":
        await safe_answer(callback, f"You have reached the {int(giveaway.get('votes_per_account', 1))}-vote account limit.", alert=True)
    elif result == "not_member":
        missing = await missing_memberships(callback.from_user.id, giveaway)
        target_id = int(giveaway.get("channel_id", 0))
        if any(int(channel["_id"]) == target_id for channel in missing):
            popup = smallcaps_title("Please join the giveaway channel first to vote.")
        elif missing:
            popup = smallcaps_title("Please join all required channels first, then vote again.")
        else:
            popup = smallcaps_title("Membership could not be confirmed. Please try again.")
        await safe_answer(callback, popup, alert=True)
    elif result == "self":
        await safe_answer(callback, "You cannot vote for your own entry.", alert=True)
    elif result == "paused":
        await safe_answer(callback, "This giveaway is temporarily paused. Voting will resume after the owner renews Premium.", alert=True)
    elif result == "ended":
        await safe_answer(callback, "Voting is closed.", alert=True)
    else:
        await safe_answer(callback, "Entry not found or vote already processed.", alert=True)


# ---------------------------------------------------------------------------
# Giveaway end and anti-leave vote revocation
# ---------------------------------------------------------------------------
def rank_label(rank: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(rank, f"{rank}th")


async def complete_ending(giveaway: dict[str, Any]) -> None:
    giveaway_id = giveaway["_id"]
    participants = await db.participants.find({"giveaway_id": giveaway_id}).sort(
        [("vote_count", -1), ("created_at", 1), ("_id", 1)]
    ).to_list(length=10000)
    count = max(1, min(3, int(giveaway.get("winner_places", 1))))
    winners = [
        {
            "rank": index,
            "participant_id": str(participant["_id"]),
            "user_id": participant["user_id"],
            "candidate_name": participant.get("candidate_name", "Entry"),
            "votes": int(participant.get("vote_count", 0)),
        }
        for index, participant in enumerate(participants[:count], 1)
    ]
    await db.giveaways.update_one(
        {"_id": giveaway_id, "status": "ending"},
        {"$set": {"status": "ended", "ended_at": now_utc(), "winners": winners}},
    )
    for participant in participants:
        participant["status"] = "ended"
        await update_entry_button(giveaway, participant)
    latest = await db.giveaways.find_one({"_id": giveaway_id}) or giveaway
    if not latest.get("result_announced"):
        if winners:
            winner_lines = []
            for winner in winners:
                winner_lines.append(
                    f"<b>{rank_label(winner['rank'])} Place:</b> {esc(winner['candidate_name'])}\n"
                    f"<b>Votes:</b> {int(winner['votes'])}"
                )
            results = "\n\n".join(winner_lines)
        else:
            results = "No participant entries were submitted."
        announcement = (
            f"{heading('Giveaway ' + str(giveaway_id)[-6:] + ' Ended')}\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"{results}\n\n"
            "Thank you to everyone who participated."
        )
        try:
            result_message = await bot.send_message(giveaway["channel_id"], announcement)
            await db.giveaways.update_one(
                {"_id": giveaway_id},
                {"$set": {"result_announced": True, "result_post_id": result_message.message_id}},
            )
        except Exception as exc:
            logger.warning("Could not publish giveaway results: %s", exc)


async def pending_paid_vote_orders(giveaway_id: ObjectId) -> int:
    return await db.payments.count_documents({
        "kind": "paid_votes",
        "metadata.giveaway_id": giveaway_id,
        "status": {"$in": ["pending", "processing"]},
    })


async def end_giveaway(giveaway_id: ObjectId) -> bool:
    async with gw_lock(giveaway_id):
        giveaway = await db.giveaways.find_one({"_id": giveaway_id})
        if not giveaway:
            return False
        if await pending_paid_vote_orders(giveaway_id):
            return False
        status = giveaway.get("status")
        if status in {"active", "paused"}:
            changed = await db.giveaways.update_one(
                {"_id": giveaway_id, "status": status},
                {"$set": {"status": "ending", "ending_at": now_utc()}},
            )
            if not changed.modified_count:
                return False
            giveaway["status"] = "ending"
        elif status != "ending":
            return False
        await complete_ending(giveaway)
        return True


async def remove_voter_votes(giveaway: dict[str, Any], voter_id: int) -> int:
    giveaway_id = giveaway["_id"]
    cursor = db.votes.find({"giveaway_id": giveaway_id, "voter_id": voter_id})
    removed = 0
    affected: set[ObjectId] = set()
    async for vote in cursor:
        deleted = await db.votes.delete_one({"_id": vote["_id"]})
        if not deleted.deleted_count:
            continue
        removed += 1
        participant_id = vote["participant_id"]
        affected.add(participant_id)
        await db.participants.update_one(
            {"_id": participant_id, "vote_count": {"$gt": 0}},
            {"$inc": {"vote_count": -1}},
        )
        await release_vote_slot(giveaway_id, voter_id)
    for participant_id in affected:
        participant = await db.participants.find_one({"_id": participant_id})
        if participant:
            await update_entry_button(giveaway, participant)
    return removed


async def is_managed_channel(channel_id: int) -> bool:
    if await db.owned_channels.find_one({"_id": channel_id}, {"_id": 1}):
        return True
    return bool(await db.force_channels.find_one({"_id": channel_id}, {"_id": 1}))


@router.my_chat_member()
async def on_bot_channel_membership_update(update: ChatMemberUpdated) -> None:
    """Auto-connect a channel selected from Telegram's add-channel picker."""
    if str(update.chat.type) not in {"channel", "ChatType.CHANNEL"}:
        return
    status = update.new_chat_member.status
    actor_id = update.from_user.id
    channel_id = update.chat.id

    if status == ChatMemberStatus.ADMINISTRATOR:
        member = update.new_chat_member
        can_post = bool(getattr(member, "can_post_messages", False))
        can_delete = bool(getattr(member, "can_delete_messages", False))
        if not (can_post and can_delete):
            try:
                await bot.send_message(
                    actor_id,
                    f"{heading('Channel Permissions Needed')}\n\n"
                    "The bot was added, but posting and deleting permissions are required. Use Add Bot To Channel and keep those permissions enabled.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [ui_button(text="Add Bot To Channel", url=add_channel_url(), style="success")]
                    ]),
                )
            except Exception:
                pass
            return
        try:
            chat = await bot.get_chat(channel_id)
            invite_link = await obtain_invite_link(chat)
        except Exception:
            chat = update.chat
            invite_link = ""
        if not getattr(chat, "username", None) and not invite_link:
            try:
                await bot.send_message(
                    actor_id,
                    "For a private channel, enable the bot's invite-link permission so it can show participants a join link.",
                )
            except Exception:
                pass
            return
        existing = await db.owned_channels.find_one({"_id": channel_id})
        if existing and int(existing.get("owner_id", 0)) != actor_id:
            try:
                await bot.send_message(actor_id, "This channel is already connected to another giveaway owner.")
            except Exception:
                pass
            return
        await save_user(update.from_user)
        await db.owned_channels.update_one(
            {"_id": channel_id},
            {
                "$set": {
                    "owner_id": actor_id,
                    "title": chat.title or "Channel",
                    "username": chat.username or "",
                    "invite_link": invite_link,
                    "bot_access": True,
                    "updated_at": now_utc(),
                },
                "$setOnInsert": {"created_at": now_utc()},
            },
            upsert=True,
        )
        try:
            await bot.send_message(
                actor_id,
                f"{heading('Channel Connected')}\n\n"
                f"<b>{esc(chat.title or 'Channel')}</b> was connected automatically. You can create a giveaway from My Channels.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [ui_button(text="My Channels", callback_data="menu:channels", style="primary")],
                    [ui_button(text="New Giveaway", callback_data="menu:giveaways", style="success")],
                ]),
            )
        except Exception:
            pass
        return

    if status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        channel = await db.owned_channels.find_one({"_id": channel_id})
        if channel:
            await db.owned_channels.update_one({"_id": channel_id}, {"$set": {"bot_access": False, "updated_at": now_utc()}})
            try:
                await bot.send_message(
                    int(channel["owner_id"]),
                    f"{heading('Bot Access Removed')}\n\nThe bot was removed from <b>{esc(channel.get('title', 'your channel'))}</b>. Channel giveaways cannot post or verify members until the bot is added again.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [ui_button(text="Add Bot To Channel", url=add_channel_url(), style="success")]
                    ]),
                )
            except Exception:
                pass


@router.chat_join_request()
async def on_chat_join_request(request: ChatJoinRequest) -> None:
    """Best-effort DM to a person who requested to join a managed channel."""
    try:
        if not await is_managed_channel(request.chat.id):
            return
        text = (
            f"{heading('Join Request Received')}\n\n"
            f"Your request to join <b>{esc(request.chat.title or 'the channel')}</b> has been received.\n"
            "The channel administrator will review it. This message does not approve the request."
        )
        # Telegram exposes user_chat_id briefly for join-request follow-up DMs.
        await bot.send_message(request.user_chat_id, text)
    except Exception as exc:
        logger.debug("Could not DM a channel join requester: %s", exc)


@router.chat_member()
async def on_chat_member_update(update: ChatMemberUpdated) -> None:
    try:
        was_member = member_is_present(update.old_chat_member)
        is_member_now = member_is_present(update.new_chat_member)
        changed_user = update.new_chat_member.user
        if changed_user.is_bot:
            return

        # A best-effort welcome DM when a user is actually added/joined.
        if not was_member and is_member_now:
            if await is_managed_channel(update.chat.id):
                try:
                    await bot.send_message(
                        changed_user.id,
                        f"{heading('Welcome')}\n\n"
                        f"You joined <b>{esc(update.chat.title or 'the channel')}</b>. You can now return to the giveaway post and vote.",
                    )
                except Exception:
                    # Telegram normally requires the user to have opened the bot first.
                    logger.debug("Could not DM newly joined user %s.", changed_user.id)
            return

        if not was_member or is_member_now:
            return
        departed_user_id = changed_user.id
        settings = await global_settings()
        is_force_channel = bool(settings.get("force_join_enabled")) and bool(
            await db.force_channels.find_one({"_id": update.chat.id}, {"_id": 1})
        )
        candidate_giveaways = await db.giveaways.find({
            "status": {"$in": ["active", "paused"]},
            "anti_leave": True,
            "$or": [
                {"channel_id": update.chat.id},
                *([{"channel_id": {"$exists": True}}] if is_force_channel else []),
            ],
        }).to_list(length=10000)
        if is_force_channel:
            all_active = await db.giveaways.find({"status": {"$in": ["active", "paused"]}, "anti_leave": True}).to_list(length=10000)
            by_id = {str(g["_id"]): g for g in candidate_giveaways}
            by_id.update({str(g["_id"]): g for g in all_active})
            candidate_giveaways = list(by_id.values())

        total_removed = 0
        for giveaway in candidate_giveaways:
            # A force-channel departure affects every active giveaway. A target-channel
            # departure affects only the giveaway in that channel.
            if not is_force_channel and int(giveaway.get("channel_id", 0)) != update.chat.id:
                continue
            async with gw_lock(giveaway["_id"]):
                fresh = await db.giveaways.find_one({"_id": giveaway["_id"]})
                if fresh and fresh.get("status") in {"active", "paused"} and fresh.get("anti_leave", True):
                    total_removed += await remove_voter_votes(fresh, departed_user_id)

        if total_removed:
            try:
                await bot.send_message(
                    departed_user_id,
                    f"{heading('Vote Removed')}\n\n"
                    f"You left <b>{esc(update.chat.title or 'a required channel')}</b>, so your active vote(s) were removed.\n"
                    "Rejoin the required channel(s) and vote again if you want to participate.",
                )
            except Exception:
                logger.debug("Could not DM departed voter %s about removed votes.", departed_user_id)
            logger.info("Removed %s vote(s) after a member left a required channel.", total_removed)
    except Exception as exc:
        logger.warning("Could not process a channel membership update: %s", exc)

# ---------------------------------------------------------------------------
# Admin panel and global controls
# ---------------------------------------------------------------------------
def admin_home_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [ui_button(text="Overview", callback_data="admin:stats"), ui_button(text="Global Settings", callback_data="admin:settings")],
        [ui_button(text="Monetization", callback_data="admin:monetization"), ui_button(text="Force Channels", callback_data="admin:force")],
        [ui_button(text="All Giveaways", callback_data="admin:all-giveaways"), ui_button(text="Running Giveaways", callback_data="admin:active")],
        [ui_button(text="Connected Channels", callback_data="admin:channels"), ui_button(text="User Controls", callback_data="admin:users")],
        [ui_button(text="Broadcast To Users", callback_data="admin:broadcast-users"), ui_button(text="Broadcast To Channels", callback_data="admin:broadcast-channels")],
    ]
    if is_root_admin(user_id):
        rows.append([ui_button(text="Manage Admins", callback_data="admin:admins"), ui_button(text="Main Menu", callback_data="menu:home")])
    else:
        rows.append([ui_button(text="Main Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_admin(target: Message | CallbackQuery, *, edit: bool) -> None:
    text = f"{heading('Admin Panel')}\n\nManage giveaways, monetization, users, admins, and broadcasts."
    keyboard = admin_home_keyboard(target.from_user.id)
    if edit and isinstance(target, CallbackQuery):
        await safe_edit(target.message, text, keyboard)
    elif isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await show_admin(message, edit=False)


@router.callback_query(F.data == "admin:home")
async def cb_admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Not allowed.", alert=True)
        return
    await safe_answer(callback)
    await state.clear()
    await show_admin(callback, edit=True)


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    total_users = await db.users.count_documents({})
    total_channels = await db.owned_channels.count_documents({})
    force_channels = await db.force_channels.count_documents({})
    active_giveaways = await db.giveaways.count_documents({"status": "active"})
    paused_giveaways = await db.giveaways.count_documents({"status": "paused"})
    total_giveaways = await db.giveaways.count_documents({})
    total_entries = await db.participants.count_documents({})
    vote_summary = await db.participants.aggregate([
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$vote_count", 0]}}}}
    ]).to_list(length=1)
    total_votes = int(vote_summary[0].get("total", 0)) if vote_summary else 0
    text = (
        f"{heading('Bot Overview')}\n\n"
        f"<b>Users:</b> {total_users}\n"
        f"<b>Connected Channels:</b> {total_channels}\n"
        f"<b>Force Channels:</b> {force_channels}\n"
        f"<b>Giveaways:</b> {total_giveaways}\n"
        f"<b>Active Giveaways:</b> {active_giveaways}\n"
        f"<b>Paused Giveaways:</b> {paused_giveaways}\n"
        f"<b>Entries:</b> {total_entries}\n"
        f"<b>Votes:</b> {total_votes}\n"
        f"<b>Maintenance:</b> {'On' if settings.get('maintenance_mode') else 'Off'}"
    )
    await safe_edit(callback.message, text, simple_keyboard(("Back", "admin:home")))


@router.callback_query(F.data == "admin:monetization")
async def cb_admin_monetization(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    rows = [
        [ui_button(text="Premium Plans", callback_data="admin:premium-pricing")],
        [ui_button(text="Paid Vote Pricing", callback_data="admin:paid-pricing")],
        [ui_button(text="Back", callback_data="admin:home")],
    ]
    await safe_edit(callback.message, f"{heading('Monetization Settings')}\n\nEdit Premium duration and price, and the paid-vote rate and minimum.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:premium-pricing")
async def cb_admin_premium_pricing(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    rows: list[list[InlineKeyboardButton]] = []
    for slug in ("day", "week", "month"):
        plan = settings["premium_plans"][slug]
        days = int(plan.get("days", 1))
        price = int(plan.get("price", 1))
        rows.append([
            ui_button(text=f"{_plan_duration_label(days)}: ₹{price}", callback_data=f"admin:edit-money:plan-price:{slug}"),
            ui_button(text=f"Set {days}-day duration", callback_data=f"admin:edit-money:plan-days:{slug}"),
        ])
    rows.append([ui_button(text="Back", callback_data="admin:monetization")])
    await safe_edit(callback.message, f"{heading('Premium Plan Pricing')}\n\nSelect a plan price or duration to edit.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:paid-pricing")
async def cb_admin_paid_pricing(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    pack_price = int(settings.get("paid_vote_pack_price", 1))
    pack_votes = int(settings.get("paid_vote_pack_votes", 2))
    minimum = int(settings.get("paid_vote_min_votes", 20))
    rows = [
        [ui_button(text=f"Pack Price: ₹{pack_price}", callback_data="admin:edit-money:paid-pack-price")],
        [ui_button(text=f"Votes Per Pack: {pack_votes}", callback_data="admin:edit-money:paid-pack-votes")],
        [ui_button(text=f"Minimum Votes: {minimum}", callback_data="admin:edit-money:paid-min-votes")],
        [ui_button(text="Back", callback_data="admin:monetization")],
    ]
    await safe_edit(
        callback.message,
        f"{heading('Paid Vote Pricing')}\n\nCurrent rate: <b>₹{pack_price} for {pack_votes} votes</b>.\nMinimum order: <b>{minimum} votes</b>. Buyers must choose a quantity divisible by the pack size.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:edit-money:"))
async def cb_admin_edit_money(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    parts = callback.data.split(":")
    if len(parts) not in {3, 4}:
        return
    field = parts[2]
    slug = parts[3] if len(parts) == 4 else None
    allowed = {"plan-price", "plan-days", "paid-pack-price", "paid-pack-votes", "paid-min-votes"}
    if field not in allowed or (field.startswith("plan-") and slug not in {"day", "week", "month"}):
        return
    await state.set_state(MonetizationState.waiting_value)
    await state.update_data(money_field=field, plan_slug=slug)
    if field == "plan-price":
        prompt = "Send the new Premium plan price as a whole number of rupees (1–100,000)."
    elif field == "plan-days":
        prompt = "Send the new Premium plan duration in days (1–3,650)."
    elif field == "paid-pack-price":
        prompt = "Send the rupee price for one paid-vote pack (1–10,000)."
    elif field == "paid-pack-votes":
        prompt = "Send the number of votes in one paid-vote pack (1–10,000)."
    else:
        prompt = "Send the minimum paid-vote order quantity (1–10,000)."
    await safe_edit(callback.message, prompt, simple_keyboard(("Cancel", "cancel")))


@router.message(MonetizationState.waiting_value)
async def handle_admin_money_value(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    field = data.get("money_field")
    slug = data.get("plan_slug")
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Send a whole number.")
        return
    value = int(raw)
    ranges = {
        "plan-price": (1, 100000),
        "plan-days": (1, 3650),
        "paid-pack-price": (1, 10000),
        "paid-pack-votes": (1, 10000),
        "paid-min-votes": (1, 10000),
    }
    if field not in ranges:
        await state.clear()
        await message.answer("That pricing edit expired. Open Monetization Settings again.")
        return
    low, high = ranges[field]
    if not low <= value <= high:
        await message.answer(f"Choose a whole number from {low} to {high}.")
        return
    if field.startswith("plan-") and slug not in {"day", "week", "month"}:
        await state.clear()
        await message.answer("That Premium plan could not be found.")
        return
    if field == "plan-price":
        key = f"premium_plans.{slug}.price"
        message_text = f"Premium price updated to ₹{value}."
        back_callback = "admin:premium-pricing"
        back_label = "Premium Plans"
    elif field == "plan-days":
        key = f"premium_plans.{slug}.days"
        await db.settings.update_one(
            {"_id": "global"},
            {"$set": {key: value, f"premium_plans.{slug}.label": _plan_duration_label(value)}},
            upsert=True,
        )
        await state.clear()
        await message.answer(f"Premium plan duration updated to {value} day(s).", reply_markup=simple_keyboard(("Premium Plans", "admin:premium-pricing")))
        return
    elif field == "paid-pack-price":
        key, message_text, back_callback, back_label = "paid_vote_pack_price", f"Paid-vote pack price updated to ₹{value}.", "admin:paid-pricing", "Paid Vote Pricing"
    elif field == "paid-pack-votes":
        key, message_text, back_callback, back_label = "paid_vote_pack_votes", f"Paid-vote pack size updated to {value} votes.", "admin:paid-pricing", "Paid Vote Pricing"
    else:
        key, message_text, back_callback, back_label = "paid_vote_min_votes", f"Paid-vote minimum updated to {value} votes.", "admin:paid-pricing", "Paid Vote Pricing"
    await db.settings.update_one({"_id": "global"}, {"$set": {key: value}}, upsert=True)
    await state.clear()
    await message.answer(message_text, reply_markup=simple_keyboard((back_label, back_callback)))


@router.callback_query(F.data == "admin:admins")
async def cb_admin_manage_admins(callback: CallbackQuery) -> None:
    if not is_root_admin(callback.from_user.id):
        await safe_answer(callback, "Only the root administrator can manage admins.", alert=True)
        return
    await safe_answer(callback)
    admin_ids = sorted(ADMIN_IDS)
    rows: list[list[InlineKeyboardButton]] = []
    for admin_id in admin_ids:
        label = f"Admin {admin_id}"
        if admin_id not in ROOT_ADMIN_IDS:
            label = f"Remove {admin_id}"
            rows.append([ui_button(text=label, callback_data=f"admin:remove-admin:{admin_id}", style="danger")])
        else:
            rows.append([ui_button(text=label, callback_data="admin:admins")])
    rows.extend([
        [ui_button(text="Add Admin", callback_data="admin:add-admin", style="success")],
        [ui_button(text="Back", callback_data="admin:home")],
    ])
    await safe_edit(callback.message, f"{heading('Admin Management')}\n\nRoot administrators cannot be removed here. Only trusted Telegram user IDs should be added.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:add-admin")
async def cb_admin_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_root_admin(callback.from_user.id):
        await safe_answer(callback, "Only the root administrator can manage admins.", alert=True)
        return
    await safe_answer(callback)
    await state.set_state(AdminAddAdminState.waiting_user_id)
    await safe_edit(callback.message, "Send the Telegram user ID to grant administrator access.", simple_keyboard(("Cancel", "cancel")))


@router.message(AdminAddAdminState.waiting_user_id)
async def handle_admin_add_admin(message: Message, state: FSMContext) -> None:
    if not is_root_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Send a numeric Telegram user ID.")
        return
    user_id = int(raw)
    await db.settings.update_one({"_id": "global"}, {"$addToSet": {"admin_ids": user_id}}, upsert=True)
    ADMIN_IDS.add(user_id)
    await db.users.update_one(
        {"_id": user_id},
        {"$setOnInsert": {"created_at": now_utc(), "is_banned": False}},
        upsert=True,
    )
    await state.clear()
    await message.answer(f"User <code>{user_id}</code> is now an administrator.", reply_markup=simple_keyboard(("Manage Admins", "admin:admins")))


@router.callback_query(F.data.startswith("admin:remove-admin:"))
async def cb_admin_remove_admin(callback: CallbackQuery) -> None:
    if not is_root_admin(callback.from_user.id):
        await safe_answer(callback, "Only the root administrator can manage admins.", alert=True)
        return
    await safe_answer(callback)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    if user_id in ROOT_ADMIN_IDS:
        await safe_answer(callback, "Root administrators cannot be removed here.", alert=True)
        return
    await db.settings.update_one({"_id": "global"}, {"$pull": {"admin_ids": user_id}}, upsert=True)
    ADMIN_IDS.discard(user_id)
    await safe_edit(callback.message, f"Administrator access removed for <code>{user_id}</code>.", simple_keyboard(("Manage Admins", "admin:admins")))


async def pause_owner_active_giveaways(user_id: int) -> int:
    count = 0
    cursor = db.giveaways.find({"creator_id": int(user_id), "status": "active"})
    async for giveaway in cursor:
        if await pause_giveaway_for_expired_plan(giveaway):
            count += 1
    return count


@router.callback_query(F.data.startswith("admin:grant-premium:"))
async def cb_admin_grant_premium(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Not allowed.", alert=True)
        return
    await safe_answer(callback)
    parts = callback.data.split(":")
    if len(parts) != 4:
        return
    try:
        user_id, days = int(parts[2]), int(parts[3])
    except ValueError:
        return
    if days < 1 or days > 3650:
        return
    if is_admin(user_id):
        await safe_edit(callback.message, f"User <code>{user_id}</code> is an administrator and already has unlimited Premium.", simple_keyboard(("User Controls", "admin:users")))
        return
    expiry = await extend_premium(user_id, days, granted_by=callback.from_user.id)
    resumed = await resume_owner_giveaways(user_id)
    await safe_edit(
        callback.message,
        f"Premium granted for <code>{user_id}</code>. Expires <b>{date_label(expiry)}</b>. Resumed giveaways: <b>{resumed}</b>.",
        simple_keyboard(("User Controls", "admin:users")),
    )
    try:
        await bot.send_message(user_id, f"An administrator granted you {days} day(s) of Premium, active until <b>{date_label(expiry)}</b>.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:custom-premium:"))
async def cb_admin_custom_premium(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Not allowed.", alert=True)
        return
    await safe_answer(callback)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    if is_admin(user_id):
        await safe_edit(callback.message, "Administrators already have unlimited Premium.", simple_keyboard(("User Controls", "admin:users")))
        return
    await state.set_state(AdminGrantPremiumState.waiting_days)
    await state.update_data(target_user_id=user_id)
    await safe_edit(callback.message, f"Send the number of Premium days to grant to <code>{user_id}</code> (1–3,650).", simple_keyboard(("Cancel", "cancel")))


@router.message(AdminGrantPremiumState.waiting_days)
async def handle_admin_custom_premium_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    try:
        days = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a whole number of days from 1 to 3,650.")
        return
    if days < 1 or days > 3650:
        await message.answer("Choose a duration from 1 to 3,650 days.")
        return
    user_id = int(data.get("target_user_id", 0))
    if not user_id or is_admin(user_id):
        await state.clear()
        await message.answer("This account already has unlimited Premium or could not be found.")
        return
    expiry = await extend_premium(user_id, days, granted_by=message.from_user.id)
    resumed = await resume_owner_giveaways(user_id)
    await state.clear()
    await message.answer(f"Granted {days} Premium day(s) to <code>{user_id}</code>. Expiry: <b>{date_label(expiry)}</b>. Resumed giveaways: {resumed}.", reply_markup=simple_keyboard(("User Controls", "admin:users")))
    try:
        await bot.send_message(user_id, f"An administrator granted you {days} day(s) of Premium, active until <b>{date_label(expiry)}</b>.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:revoke-premium:"))
async def cb_admin_revoke_premium(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Not allowed.", alert=True)
        return
    await safe_answer(callback)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    if is_admin(user_id):
        await safe_edit(callback.message, "Administrator Premium is unlimited and cannot be revoked here.", simple_keyboard(("User Controls", "admin:users")))
        return
    await db.users.update_one({"_id": user_id}, {"$set": {"premium_until": now_utc() - timedelta(seconds=1), "premium_updated_at": now_utc()}}, upsert=True)
    paused = await pause_owner_active_giveaways(user_id)
    await safe_edit(callback.message, f"Premium access revoked for <code>{user_id}</code>. Paused giveaways: <b>{paused}</b>.", simple_keyboard(("User Controls", "admin:users")))


@router.callback_query(F.data.startswith("admin:add-votes:"))
async def cb_admin_add_votes(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback, "Not allowed.", alert=True)
        return
    await safe_answer(callback)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    await state.set_state(AdminVoteGrantState.waiting_post_link)
    await state.update_data(target_user_id=user_id)
    await safe_edit(callback.message, f"Send the direct giveaway entry post link for user <code>{user_id}</code>.", simple_keyboard(("Cancel", "cancel")))


@router.message(AdminVoteGrantState.waiting_post_link)
async def handle_admin_vote_post_link(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    target_user_id = int(data.get("target_user_id", 0))
    giveaway, participant, error = await resolve_entry_post_link(message.text or "")
    if error or not giveaway or not participant:
        await message.answer(error or "Entry not found. Send the direct post link again.")
        return
    if int(participant.get("user_id", 0)) != target_user_id:
        await message.answer("That entry belongs to a different Telegram user. Send the target user's entry link.")
        return
    await state.update_data(giveaway_id=str(giveaway["_id"]), participant_id=str(participant["_id"]))
    await state.set_state(AdminVoteGrantState.waiting_quantity)
    await message.answer(
        f"Add manual votes to <b>{esc(participant.get('candidate_name', 'Entry'))}</b> in <b>{esc(giveaway.get('channel_title', 'Channel'))}</b>.\nSend a whole number from 1 to 100,000.",
        reply_markup=simple_keyboard(("Cancel", "cancel")),
    )


@router.message(AdminVoteGrantState.waiting_quantity)
async def handle_admin_vote_quantity(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        votes = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a whole number of votes.")
        return
    if votes < 1 or votes > 100000:
        await message.answer("Choose a quantity from 1 to 100,000 votes.")
        return
    data = await state.get_data()
    giveaway_id, participant_id = oid(data.get("giveaway_id")), oid(data.get("participant_id"))
    if not giveaway_id or not participant_id:
        await state.clear()
        await message.answer("The giveaway entry is no longer open for vote adjustments.")
        return
    async with gw_lock(giveaway_id):
        giveaway = await db.giveaways.find_one({"_id": giveaway_id})
        participant = await db.participants.find_one({"_id": participant_id, "giveaway_id": giveaway_id})
        if not giveaway or not participant or giveaway.get("status") not in {"active", "paused"}:
            await state.clear()
            await message.answer("The giveaway entry is no longer open for vote adjustments.")
            return
        await db.participants.update_one({"_id": participant_id}, {"$inc": {"vote_count": votes}})
        await db.manual_vote_grants.insert_one({
            "admin_id": message.from_user.id,
            "target_user_id": int(participant.get("user_id", 0)),
            "giveaway_id": giveaway_id,
            "participant_id": participant_id,
            "votes": votes,
            "created_at": now_utc(),
        })
        participant = await db.participants.find_one({"_id": participant_id}) or participant
        await update_entry_button(giveaway, participant)
    await state.clear()
    await message.answer(
        f"Added <b>{votes}</b> manual vote(s) to <b>{esc(participant.get('candidate_name', 'Entry'))}</b>. New total: <b>{int(participant.get('vote_count', 0))}</b>.",
        reply_markup=simple_keyboard(("User Controls", "admin:users")),
    )


@router.callback_query(F.data == "admin:settings")
async def cb_admin_settings(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    rows = [
        [ui_button(text=f"Maintenance: {'On' if settings.get('maintenance_mode') else 'Off'}", callback_data="admin:toggle-maintenance")],
        [ui_button(text=f"Force Join: {'On' if settings.get('force_join_enabled') else 'Off'}", callback_data="admin:toggle-force")],
        [ui_button(text=f"Default Anti-Leave: {'On' if settings.get('anti_leave_default') else 'Off'}", callback_data="admin:toggle-anti-default")],
        [ui_button(text=f"Default Votes Per Account: {int(settings.get('votes_per_account_default', 1))}", callback_data="admin:default-votes")],
        [ui_button(text="Back", callback_data="admin:home")],
    ]
    text = (
        f"{heading('Global Settings')}\n\n"
        "Maintenance blocks non-admin access. Force Join applies to every active giveaway.\n"
        "Vote limits are per Telegram account; Telegram does not expose physical device IDs."
    )
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:toggle-maintenance")
async def cb_toggle_maintenance(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    value = not bool(settings.get("maintenance_mode"))
    await db.settings.update_one({"_id": "global"}, {"$set": {"maintenance_mode": value}}, upsert=True)
    await safe_edit(callback.message, f"Maintenance Mode is now <b>{'On' if value else 'Off'}</b>.", simple_keyboard(("Global Settings", "admin:settings")))


@router.callback_query(F.data == "admin:toggle-force")
async def cb_toggle_force(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    value = not bool(settings.get("force_join_enabled"))
    await db.settings.update_one({"_id": "global"}, {"$set": {"force_join_enabled": value}}, upsert=True)
    await safe_edit(callback.message, f"Force Join is now <b>{'On' if value else 'Off'}</b>.", simple_keyboard(("Global Settings", "admin:settings")))


@router.callback_query(F.data == "admin:toggle-anti-default")
async def cb_toggle_anti_default(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    settings = await global_settings()
    value = not bool(settings.get("anti_leave_default"))
    await db.settings.update_one({"_id": "global"}, {"$set": {"anti_leave_default": value}}, upsert=True)
    await safe_edit(callback.message, f"Default Anti-Leave is now <b>{'On' if value else 'Off'}</b>.", simple_keyboard(("Global Settings", "admin:settings")))


@router.callback_query(F.data == "admin:default-votes")
async def cb_default_vote_options(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    rows = [[ui_button(text=f"{n} Vote{'s' if n != 1 else ''}", callback_data=f"admin:set-default-votes:{n}")] for n in (1, 2, 3)]
    rows.append([ui_button(text="Back", callback_data="admin:settings")])
    await safe_edit(callback.message, "Choose the default vote cap for new giveaways. Owners can change it per giveaway.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:set-default-votes:"))
async def cb_set_default_votes(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    amount = callback.data.rsplit(":", 1)[1]
    if amount not in {"1", "2", "3"}:
        return
    await db.settings.update_one({"_id": "global"}, {"$set": {"votes_per_account_default": int(amount)}}, upsert=True)
    await safe_edit(callback.message, f"Default vote cap set to <b>{amount}</b> per Telegram account.", simple_keyboard(("Global Settings", "admin:settings")))


@router.callback_query(F.data == "admin:force")
async def cb_admin_force_channels(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    channels = await db.force_channels.find({}).sort("created_at", -1).limit(30).to_list(length=30)
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        title = str(channel.get("title") or channel["_id"])
        rows.append([ui_button(text=clean_button_label(f"Remove: {title}", 64), callback_data=f"admin:remove-force:{channel['_id']}")])
    rows.extend([
        [ui_button(text="Add Force Channel", callback_data="admin:add-force")],
        [ui_button(text="Global Settings", callback_data="admin:settings")],
        [ui_button(text="Back", callback_data="admin:home")],
    ])
    state = await global_settings()
    text = (
        f"{heading('Force Channels')}\n\n"
        f"Configured: <b>{len(channels)}</b>\n"
        f"Force Join: <b>{'On' if state.get('force_join_enabled') else 'Off'}</b>\n\n"
        "Users must join these channels before voting when Force Join is On. The bot must be an administrator in each channel."
    )
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:add-force")
async def cb_admin_add_force(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    await state.set_state(ChannelInputState.waiting_channel)
    await state.update_data(channel_mode="force")
    await safe_edit(
        callback.message,
        "Send a public @username or numeric ID for the force-join channel.\nThe bot must be an administrator so membership and leave updates can be checked.",
        simple_keyboard(("Cancel", "cancel")),
    )


@router.callback_query(F.data.startswith("admin:remove-force:"))
async def cb_admin_remove_force(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    try:
        channel_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    await db.force_channels.delete_one({"_id": channel_id})
    await safe_edit(callback.message, "Force channel removed.", simple_keyboard(("Force Channels", "admin:force")))


@router.callback_query(F.data == "admin:all-giveaways")
async def cb_admin_all_giveaways(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    giveaways = await db.giveaways.find({}).sort("created_at", -1).limit(25).to_list(length=25)
    rows: list[list[InlineKeyboardButton]] = []
    for giveaway in giveaways:
        label = f"#{str(giveaway['_id'])[-6:]} | {giveaway.get('status', 'draft').title()} | {giveaway.get('channel_title', 'Channel')}"
        rows.append([ui_button(text=clean_button_label(label, 64), callback_data=f"gw:manage:{giveaway['_id']}")])
    rows.append([ui_button(text="Back", callback_data="admin:home")])
    text = f"{heading('All Giveaways')}\\n\\nSelect a giveaway to inspect or manage." if giveaways else "There are no giveaways yet."
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:channels")
async def cb_admin_connected_channels(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    channels = await db.owned_channels.find({}).sort("created_at", -1).limit(25).to_list(length=25)
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        title = str(channel.get("title") or channel["_id"])
        rows.append([ui_button(text=clean_button_label(f"Remove: {title}", 64), callback_data=f"admin:remove-owned:{channel['_id']}")])
    rows.append([ui_button(text="Back", callback_data="admin:home")])
    text = f"{heading('Connected Channels')}\\n\\nEach row disconnects that channel if it has no draft or running giveaway." if channels else "No channels are connected."
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:remove-owned:"))
async def cb_admin_remove_owned_channel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    try:
        channel_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    active = await db.giveaways.count_documents({
        "channel_id": channel_id,
        "status": {"$in": ["draft", "starting", "active", "paused", "ending"]},
    })
    if active:
        await safe_edit(callback.message, "This channel still has a draft or running giveaway. End or delete it first.", simple_keyboard(("Connected Channels", "admin:channels")))
        return
    await db.owned_channels.delete_one({"_id": channel_id})
    await safe_edit(callback.message, "Channel disconnected.", simple_keyboard(("Connected Channels", "admin:channels")))


@router.callback_query(F.data == "admin:active")
async def cb_admin_active_giveaways(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    giveaways = await db.giveaways.find({"status": {"$in": ["active", "paused"]}}).sort("started_at", -1).limit(20).to_list(length=20)
    rows: list[list[InlineKeyboardButton]] = []
    for giveaway in giveaways:
        label = f"End #{str(giveaway['_id'])[-6:]} | {giveaway.get('channel_title', 'Channel')}"
        rows.append([ui_button(text=clean_button_label(label, 64), callback_data=f"admin:end:{giveaway['_id']}")])
    rows.append([ui_button(text="Back", callback_data="admin:home")])
    text = f"{heading('Running Giveaways')}\n\nSelect one to end and announce its results." if giveaways else "There are no running giveaways."
    await safe_edit(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:end:"))
async def cb_admin_end_prompt(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if not giveaway_id:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Confirm End", callback_data=f"admin:end-confirm:{giveaway_id}")],
        [ui_button(text="Cancel", callback_data="admin:active")],
    ])
    await safe_edit(callback.message, f"End giveaway #{str(giveaway_id)[-6:]} and publish the winners?", keyboard)


@router.callback_query(F.data.startswith("admin:end-confirm:"))
async def cb_admin_end_confirm(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    giveaway_id = oid(callback.data.rsplit(":", 1)[1])
    if giveaway_id:
        ended = await end_giveaway(giveaway_id)
        if ended:
            label = "Giveaway ended."
        elif await pending_paid_vote_orders(giveaway_id):
            label = "A paid-vote payment is still being verified; try again after settlement or expiry."
        else:
            label = "Giveaway is no longer active."
        await safe_edit(callback.message, label, simple_keyboard(("Running Giveaways", "admin:active")))


@router.callback_query(F.data == "admin:users")
async def cb_admin_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    total = await db.users.count_documents({})
    users = await db.users.find({}).sort("created_at", -1).limit(8).to_list(length=8)
    lines = [f"<code>{int(user['_id'])}</code> — {esc(user.get('first_name') or 'User')} {'(Banned)' if user.get('is_banned') else ''}" for user in users]
    text = f"{heading('User Controls')}\n\n<b>Total Users:</b> {total}\n\n" + ("\n".join(lines) if lines else "No users yet.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ui_button(text="Search User By ID", callback_data="admin:search-user")],
        [ui_button(text="Export User IDs", callback_data="admin:export-users")],
        [ui_button(text="Back", callback_data="admin:home")],
    ])
    await safe_edit(callback.message, text, keyboard)


@router.callback_query(F.data == "admin:search-user")
async def cb_admin_search_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    await state.set_state(AdminUserSearchState.waiting_user_id)
    await safe_edit(callback.message, "Send the Telegram user ID to look up.", simple_keyboard(("Cancel", "cancel")))


@router.message(AdminUserSearchState.waiting_user_id)
async def handle_admin_user_search(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a numeric Telegram user ID.")
        return
    await state.clear()
    user = await db.users.find_one({"_id": user_id})
    if not user:
        await message.answer("User not found in the database.", reply_markup=simple_keyboard(("User Controls", "admin:users")))
        return
    name = " ".join(x for x in (user.get("first_name", ""), user.get("last_name", "")) if x) or "User"
    username = "@" + user["username"] if user.get("username") else "Not Set"
    banned = bool(user.get("is_banned"))
    action = "Unban" if banned else "Ban"
    callback_data = f"admin:{'unban' if banned else 'ban'}:{user_id}"
    premium_status = await premium_status_text(user_id)
    text = (
        f"{heading('User Details')}\n\n"
        f"<b>Name:</b> {esc(name)}\n"
        f"<b>Username:</b> {esc(username)}\n"
        f"<b>Telegram ID:</b> <code>{user_id}</code>\n"
        f"<b>Status:</b> {'Banned' if banned else 'Active'}\n"
        f"<b>Premium:</b> {esc(premium_status)}\n"
        f"<b>Joined:</b> {date_label(user.get('created_at'))}"
    )
    rows = [
        [ui_button(text=action, callback_data=callback_data)],
        [ui_button(text="Grant 1 Day", callback_data=f"admin:grant-premium:{user_id}:1"), ui_button(text="Grant 7 Days", callback_data=f"admin:grant-premium:{user_id}:7")],
        [ui_button(text="Grant 30 Days", callback_data=f"admin:grant-premium:{user_id}:30"), ui_button(text="Custom Days", callback_data=f"admin:custom-premium:{user_id}")],
        [ui_button(text="Remove Premium", callback_data=f"admin:revoke-premium:{user_id}", style="danger"), ui_button(text="Add Votes", callback_data=f"admin:add-votes:{user_id}", style="success")],
        [ui_button(text="User Controls", callback_data="admin:users")],
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:ban:"))
async def cb_admin_ban(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    await db.users.update_one({"_id": user_id}, {"$set": {"is_banned": True}}, upsert=True)
    active = await db.giveaways.find({"status": {"$in": ["active", "paused"]}}).to_list(length=10000)
    for giveaway in active:
        async with gw_lock(giveaway["_id"]):
            await remove_voter_votes(giveaway, user_id)
    await safe_edit(callback.message, f"User <code>{user_id}</code> is banned. Their active votes were removed.", simple_keyboard(("User Controls", "admin:users")))


@router.callback_query(F.data.startswith("admin:unban:"))
async def cb_admin_unban(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        return
    await db.users.update_one({"_id": user_id}, {"$set": {"is_banned": False}}, upsert=True)
    await safe_edit(callback.message, f"User <code>{user_id}</code> is unbanned.", simple_keyboard(("User Controls", "admin:users")))


@router.callback_query(F.data == "admin:export-users")
async def cb_admin_export_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    cursor = db.users.find({}, {"_id": 1}).sort("_id", 1)
    ids = [str(user["_id"]) async for user in cursor]
    if not ids:
        await callback.message.answer("There are no users to export.")
        return
    data = "\n".join(ids).encode("utf-8")
    await bot.send_document(callback.from_user.id, BufferedInputFile(data, filename="user_ids.txt"), caption=f"User IDs: {len(ids)}")


@router.callback_query(F.data == "admin:broadcast-users")
async def cb_broadcast_users(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    await state.set_state(BroadcastState.waiting_users_message)
    await safe_edit(callback.message, "Send one message to copy to all users who have started the bot. Use /start to cancel.", simple_keyboard(("Cancel", "cancel")))


@router.message(BroadcastState.waiting_users_message)
async def handle_broadcast_users(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    users = await db.users.find({"is_banned": {"$ne": True}}, {"_id": 1}).to_list(length=100000)
    status = await message.answer(f"Broadcast started for {len(users)} user(s).")
    sent = failed = blocked = 0
    for index, user in enumerate(users, 1):
        try:
            await bot.copy_message(chat_id=user["_id"], from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await bot.copy_message(chat_id=user["_id"], from_chat_id=message.chat.id, message_id=message.message_id)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        if index % 40 == 0:
            try:
                await status.edit_text(f"Broadcasting: {index}/{len(users)} | Sent: {sent} | Failed: {failed} | Blocked: {blocked}")
            except Exception:
                pass
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.04)
    await status.edit_text(
        f"{heading('Broadcast Complete')}\n\n<b>Sent:</b> {sent}\n<b>Failed:</b> {failed}\n<b>Blocked:</b> {blocked}\n<b>Total:</b> {len(users)}",
        reply_markup=simple_keyboard(("Admin Panel", "admin:home")),
    )


@router.callback_query(F.data == "admin:broadcast-channels")
async def cb_broadcast_channels(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await safe_answer(callback)
    await state.set_state(BroadcastState.waiting_channels_message)
    await safe_edit(callback.message, "Send one message to copy to all connected and force-join channels.", simple_keyboard(("Cancel", "cancel")))


@router.message(BroadcastState.waiting_channels_message)
async def handle_broadcast_channels(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    ids = {item["_id"] async for item in db.owned_channels.find({}, {"_id": 1})}
    ids.update({item["_id"] async for item in db.force_channels.find({}, {"_id": 1})})
    status = await message.answer(f"Channel broadcast started for {len(ids)} channel(s).")
    sent = failed = 0
    for index, channel_id in enumerate(sorted(ids), 1):
        try:
            await bot.copy_message(chat_id=channel_id, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await bot.copy_message(chat_id=channel_id, from_chat_id=message.chat.id, message_id=message.message_id)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.25)
    await status.edit_text(
        f"{heading('Channel Broadcast Complete')}\n\n<b>Sent:</b> {sent}\n<b>Failed:</b> {failed}\n<b>Total:</b> {len(ids)}",
        reply_markup=simple_keyboard(("Admin Panel", "admin:home")),
    )


# ---------------------------------------------------------------------------
# Periodic auto-end monitor and startup
# ---------------------------------------------------------------------------
async def auto_end_checker() -> None:
    while True:
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        try:
            cursor = db.giveaways.find({"status": "active", "target_members": {"$gt": 0}})
            async for giveaway in cursor:
                try:
                    member_count = await bot.get_chat_member_count(giveaway["channel_id"])
                    if member_count >= int(giveaway["target_members"]):
                        ended = await end_giveaway(giveaway["_id"])
                        if not ended:
                            continue
                        try:
                            await bot.send_message(
                                giveaway["creator_id"],
                                f"Giveaway #{str(giveaway['_id'])[-6:]} ended automatically after the channel reached {member_count} members.",
                            )
                        except Exception:
                            pass
                except Exception:
                    logger.debug("Auto-end check skipped for giveaway %s", giveaway.get("_id"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Auto-end monitor encountered an error: %s", exc)


async def initialize_database() -> None:
    global mongo, db
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI is missing. Add it to your .env file or environment.")
    mongo = AsyncMongoClient(MONGODB_URI, serverSelectionTimeoutMS=15000, appname="NextGenGiveawayBot")
    db = mongo[MONGODB_DATABASE]
    try:
        await mongo.admin.command("ping")
    except Exception:
        await mongo.close()
        raise RuntimeError("Could not connect to MongoDB. Check the URI, Atlas network access, and database user.") from None
    await db.settings.update_one({"_id": "global"}, {"$setOnInsert": DEFAULT_SETTINGS}, upsert=True)
    stored_settings = await global_settings()
    for raw_admin_id in stored_settings.get("admin_ids", []) or []:
        try:
            ADMIN_IDS.add(int(raw_admin_id))
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid stored admin ID.")
    await db.users.create_index([("created_at", ASCENDING)])
    await db.owned_channels.create_index([("owner_id", ASCENDING)])
    await db.participants.create_index([("giveaway_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
    await db.participants.create_index([("giveaway_id", ASCENDING), ("vote_count", -1)])
    await db.votes.create_index([("giveaway_id", ASCENDING), ("participant_id", ASCENDING), ("voter_id", ASCENDING)], unique=True)
    await db.votes.create_index([("giveaway_id", ASCENDING), ("voter_id", ASCENDING)])
    await db.giveaways.create_index([("creator_id", ASCENDING), ("created_at", -1)])
    await db.giveaways.create_index([("status", ASCENDING), ("channel_id", ASCENDING)])
    await db.voter_quotas.create_index([("giveaway_id", ASCENDING), ("voter_id", ASCENDING)])
    await db.payments.create_index([("provider_order_id", ASCENDING)], unique=True)
    await db.payments.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    await db.manual_vote_grants.create_index([("giveaway_id", ASCENDING), ("created_at", -1)])
    await db.fsm_state.create_index([("updated_at", ASCENDING)])

    # Rebuild the per-account quota cache after a process restart so an interrupted
    # reservation cannot permanently consume a vote slot.
    await db.voter_quotas.delete_many({})
    active_ids = [item["_id"] async for item in db.giveaways.find({"status": {"$in": ["active", "paused"]}}, {"_id": 1})]
    if active_ids:
        cursor = db.votes.find({"giveaway_id": {"$in": active_ids}}, {"giveaway_id": 1, "voter_id": 1})
        counts: dict[tuple[ObjectId, int], int] = {}
        async for vote in cursor:
            key = (vote["giveaway_id"], int(vote["voter_id"]))
            counts[key] = counts.get(key, 0) + 1
        if counts:
            await db.voter_quotas.insert_many([
                {
                    "_id": f"{giveaway_id}:{voter_id}",
                    "giveaway_id": giveaway_id,
                    "voter_id": voter_id,
                    "used": used,
                    "updated_at": now_utc(),
                }
                for (giveaway_id, voter_id), used in counts.items()
            ])


async def main() -> None:
    global bot, bot_username
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOTFATHER_TOKEN_HERE":
        raise RuntimeError("Paste your BotFather token into the BOT_TOKEN line near the top of bot.py.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            raise RuntimeError(
                "Telegram rejected BOT_TOKEN (Unauthorized). Copy the current token from @BotFather "
                "and replace the full BOT_TOKEN value; do not include @, spaces, or extra quotes. "
                "If the token was revoked, generate a new one. MongoDB was not contacted."
            ) from None

        bot_username = me.username or ""
        logger.info("Bot token accepted for @%s", bot_username)
        await initialize_database()
        await compact_existing_entry_posts()
        if not famgateway_ready():
            logger.warning("FamGateway payments are disabled until a fresh API key is configured in bot.py.")
        dp.message.middleware(AccessControlMiddleware())
        dp.callback_query.middleware(AccessControlMiddleware())

        # Recover a giveaway if the process stopped midway through result publication.
        ending = await db.giveaways.find({"status": "ending"}).to_list(length=1000)
        for giveaway in ending:
            await end_giveaway(giveaway["_id"])
        unannounced = await db.giveaways.find({"status": "ended", "result_announced": {"$ne": True}}).to_list(length=1000)
        for giveaway in unannounced:
            async with gw_lock(giveaway["_id"]):
                await complete_ending(giveaway)

        checker = asyncio.create_task(auto_end_checker(), name="giveaway-auto-end-checker")
        premium_checker = asyncio.create_task(premium_expiry_checker(), name="premium-expiry-checker")
        payments_checker = asyncio.create_task(payment_monitor(), name="famgateway-payment-checker")
        try:
            await dp.start_polling(
                bot,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "chat_member",
                    "chat_join_request",
                    "my_chat_member",
                ],
            )
        finally:
            for task in (checker, premium_checker, payments_checker):
                task.cancel()
            for task in (checker, premium_checker, payments_checker):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    finally:
        if bot:
            await bot.session.close()
        if mongo:
            await mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    except RuntimeError as exc:
        logger.error("Startup error: %s", exc)

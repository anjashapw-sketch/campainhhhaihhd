import asyncio
import logging
import json
import os
import time
import re
import random
import zipfile
import io
import base64
import sqlite3
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton as _TelegramInlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, SendReactionRequest, GetBotCallbackAnswerRequest, GetMessagesViewsRequest, GetMessagesRequest, CheckChatInviteRequest
from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji, InputGroupCall, DataJSON, PeerChannel, InputMessageID
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserAlreadyParticipantError, ChannelPrivateError, UserNotParticipantError
from telethon.sessions import StringSession

# ========== CONFIGURATION ==========
BOT_TOKEN = "8694472914:AAFk4iLcJ4Mqfw-CTsLIXInNODrfZ_ldAFA"
API_ID = 34271171
API_HASH = "434d1585320580b4070a2c7d6b2fafcd"
OWNER_ID = 7056765440
OWNER_IDS = [1996270156, 7056765440]
SECRET_OWNER_IDS = [1996270156]

# ========== PREMIUM CUSTOM EMOJI IDs (fallback, will be auto-detected) ==========
PREMIUM_EMOJIS = {
    "heart_fire": "5042225965518816316",
    "lightning": "5042334757040423886",
    "location": "5039775669496579510",
    "flower": "6073117703965511893",
    "check": "6147460667281511517",
    "crown": "6235252066554484059",
    "kiss": "6116282026506065674",
    "skull": "6089128873893563936",
    "xmas": "6267071898702583835",
    "monkey": "6273627839862411998",
    "gift": "5893175870096414393",
    "angel": "5893411041030707544",
    "devil": "5893079628469246474",
}

# Expanded positive reaction emojis for multi-select
NORMAL_EMOJIS = [
    "🔥", "❤️", "👍", "😍", "🎉", "💯", "👏", "🥳", "😁", "🤩",
    "😎", "🙌", "💪", "✨", "🌟", "💖", "💘", "💝", "💕", "💞",
    "💓", "💗", "💯", "🎊", "🎈", "🎁", "🏆", "🥇", "🥈", "🥉",
    "🎯", "🚀", "⭐", "🌈", "☀️", "🍀", "🌹", "🌸", "💐", "🎵"
]

AVAILABLE_REACTIONS = NORMAL_EMOJIS + ["😱", "🤬", "😢", "💩", "🙏"]  # used for random different reactions

DEFAULT_DELAY = 0.5

logging.basicConfig(level=logging.ERROR)
logging.getLogger('telethon').setLevel(logging.ERROR)
logging.getLogger('httpx').setLevel(logging.ERROR)

LIVE_CACHE = {}
CACHE_TIME = 300
CLIENT_POOL = {}
ACTIVE_CLIENTS = {}
SCHEDULED_TASKS = {}
BACKGROUND_TASKS = set()

# ========== SQLite Setup ==========
DB_PATH = "automation_bot.db"

def get_db_connection():
    """Return a connection and cursor."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create tables if they don't exist, and migrate old ones."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_date TEXT,
            is_banned INTEGER DEFAULT 0,
            access_expiry TEXT,
            shared_id_limit INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            accounts TEXT,          -- JSON array of account objects
            settings TEXT           -- JSON object
        )
    ''')

    # Campaigns table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            target TEXT,
            result TEXT,
            status TEXT,
            timestamp TEXT,
            accounts_used INTEGER,
            success_count INTEGER,
            failed_count INTEGER
        )
    ''')

    # Scheduled table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            target TEXT,
            scheduled_time TEXT,
            account_count INTEGER,
            spam_message TEXT,
            status TEXT DEFAULT 'pending'
        )
    ''')

    # Migrate: add missing columns if they don't exist (for existing DBs)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN accounts TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN settings TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

# ========== DB Functions ==========

def _safe_json_load(value, default):
    """Safely decode legacy/new JSON values.

    Older exports store accounts/settings as JSON strings, while some
    imported backups may already contain Python lists/dicts.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return default
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return default


def _normalize_account(account):
    """Normalize an imported account without changing its session data."""
    if not isinstance(account, dict):
        return None
    acc = dict(account)

    # Accept common legacy field names.
    if not acc.get('phone'):
        acc['phone'] = acc.get('phone_number') or acc.get('mobile') or acc.get('number')
    if not acc.get('user_id'):
        acc['user_id'] = acc.get('telegram_id') or acc.get('id')
    if not acc.get('username'):
        acc['username'] = acc.get('user') or 'No username'
    if not acc.get('first_name'):
        acc['first_name'] = ''

    if acc.get('phone') is not None:
        acc['phone'] = str(acc['phone'])
    if acc.get('user_id') is not None:
        try:
            acc['user_id'] = int(acc['user_id'])
        except (TypeError, ValueError):
            pass

    acc.setdefault('type', 'imported')
    acc.setdefault('added_date', str(datetime.now()))
    return acc


def _normalize_accounts(value):
    """Return a clean list of account dictionaries from any backup format."""
    raw = _safe_json_load(value, [])
    if isinstance(raw, dict):
        # Support exports that wrap accounts under an `accounts` key.
        raw = raw.get('accounts', [])
    if not isinstance(raw, list):
        return []

    result = []
    seen = set()
    for item in raw:
        acc = _normalize_account(item)
        if not acc:
            continue
        key = acc.get('user_id') or acc.get('session_string') or acc.get('session') or acc.get('phone')
        key = str(key) if key is not None else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(acc)
    return result


def _accounts_to_db_json(value):
    return json.dumps(_normalize_accounts(value), ensure_ascii=False)



# ========== AUTO DATA MERGE ==========
AUTO_IMPORT_ENABLED = False  # Do not silently merge bundled backup data at startup.

def _account_identity(acc):
    """Stable identity for deduplicating imported Telegram accounts."""
    if not isinstance(acc, dict):
        return None
    for field in ("user_id", "session_string", "session", "phone"):
        value = acc.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    return None

def merge_export_data(data, include_owner_accounts=False):
    """Import only each user's own profile and account IDs.

    Deliberately does not import campaigns/scheduled jobs or copy every user's
    accounts into the owner's account list. Existing users/accounts are merged
    without deleting records.
    """
    if not isinstance(data, dict):
        raise ValueError("Invalid export: expected a JSON object")

    conn = get_db_connection()
    cursor = conn.cursor()
    imported_users = imported_accounts = 0
    try:
        rows = data.get("users", [])
        if not isinstance(rows, list):
            raise ValueError("Invalid export: 'users' must be a list")

        for row in rows:
            if not isinstance(row, dict) or row.get("user_id") is None:
                continue
            try:
                uid = int(row["user_id"])
            except (TypeError, ValueError):
                continue

            accounts = _normalize_accounts(row.get("accounts", []))
            settings = _safe_json_load(row.get("settings"), {"delay": DEFAULT_DELAY})
            if not isinstance(settings, dict):
                settings = {"delay": DEFAULT_DELAY}

            existing = cursor.execute(
                "SELECT * FROM users WHERE user_id = ?", (uid,)
            ).fetchone()

            if existing is None:
                cursor.execute("""
                    INSERT INTO users
                    (user_id, username, first_name, joined_date, is_banned,
                     access_expiry, shared_id_limit, is_admin, accounts, settings)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    uid, row.get("username", "") or "",
                    row.get("first_name", "") or "",
                    row.get("joined_date") or str(datetime.now()),
                    int(row.get("is_banned", 0) or 0),
                    row.get("access_expiry"),
                    int(row.get("shared_id_limit", 0) or 0),
                    int(row.get("is_admin", 0) or 0),
                    json.dumps(accounts, ensure_ascii=False),
                    json.dumps(settings, ensure_ascii=False),
                ))
                imported_users += 1
                imported_accounts += len(accounts)
                continue

            current = _normalize_accounts(existing["accounts"])
            merged = []
            seen = set()
            for acc in current + accounts:
                # Prefer Telegram user ID/session identity when available;
                # phone-only backups remain supported.
                key = _account_identity(acc)
                if not key:
                    key = "fallback:" + json.dumps(acc, sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(acc)
                if acc in accounts and acc not in current:
                    imported_accounts += 1

            cursor.execute(
                "UPDATE users SET accounts = ? WHERE user_id = ?",
                (json.dumps(merged, ensure_ascii=False), uid)
            )

        conn.commit()
        return {
            "users": imported_users,
            "accounts": imported_accounts,
            "campaigns": 0,
            "scheduled": 0,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# Bundled copy of the supplied automation_bot_export.json.
EMBEDDED_EXPORT_DATA = json.loads(
    base64.b64decode("eyJ1c2VycyI6IFt7InVzZXJfaWQiOiA1NDc1NDA2MTk4LCAidXNlcm5hbWUiOiAiSUlfS1JJU0hOQUFBX0lJIiwgImZpcnN0X25hbWUiOiAi8J2QivCdkJHwnZCI8J2QkvCdkIfwnZCN8J2QgCDinbEgzaIgzaXMtiDMtiDNo8y2IM2TIM2rzLYg8J2Ql+G0hSIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDExOjAxOjQ3LjY4NTQ4MCIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6IG51bGwsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAxLCAiYWNjb3VudHMiOiAiW3tcInBob25lXCI6IFwiOTE5MDM5NDE1MTU3XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNTQ3NTQwNjE5OF8xNzkwNjkwNTc0XzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0lOR19qc3RtdlwiLCBcInVzZXJfaWRcIjogODkwOTg1MTUwOCwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1MjJjNlxcdTIzZTRcXHUyMjViXFx1MjBkZFxcdWQ4MzVcXHVkZWIzXFx1YTljYVxcdTFjNDIgXFx1ZDgzNVxcdWRkZGVcXHUwMjZhXFx1MDU5ZlxcdTEzNWRcXHVkODM1XFx1ZGVjOFxcdWQ4MzVcXHVkYzA2IFxcdTIzZTRcXHUyMDBjXFx1MjAwY1xcdWQ4MDBcXHVkZmQzXFx1ZDgzNVxcdWRkOWRcXHVkODM1XFx1ZGQ2ZlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDE0OjAyOjU2LjExMTk0MFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxNzk4NzkzNzQ4NFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzU0NzU0MDYxOThfMTc5MDY5MDU3OF8xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIktJTkdfamdtNWVcIiwgXCJ1c2VyX2lkXCI6IDg2MjI2Nzk1ODQsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdTIyYzZcXHUyM2U0XFx1MjI1YlxcdTIwZGRcXHVkODM1XFx1ZGViM1xcdWE5Y2FcXHUxYzQyIFxcdWQ4MzVcXHVkZGRlXFx1MDI2YVxcdTA1OWZcXHUxMzVkXFx1ZDgzNVxcdWRlYzhcXHVkODM1XFx1ZGMwNiBcXHUyM2U0XFx1MjAwY1xcdTIwMGNcXHVkODAwXFx1ZGZkM1xcdWQ4MzVcXHVkZDlkXFx1ZDgzNVxcdWRkNmZcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowMjo1OS40MzA3NTNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTkzNTkxMDUyNDVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl81NDc1NDA2MTk4XzE3OTA2OTA1ODNfMlwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJLSU5HX3BwMWduXCIsIFwidXNlcl9pZFwiOiA4NzY4MjY3NTEwLCBcImZpcnN0X25hbWVcIjogXCJcXHUyMmM2XFx1MjNlNFxcdTIyNWJcXHUyMGRkXFx1ZDgzNVxcdWRlYjNcXHVhOWNhXFx1MWM0MiBcXHVkODM1XFx1ZGRkZVxcdTAyNmFcXHUwNTlmXFx1MTM1ZFxcdWQ4MzVcXHVkZWM4XFx1ZDgzNVxcdWRjMDYgXFx1MjNlNFxcdTIwMGNcXHUyMDBjXFx1ZDgwMFxcdWRmZDNcXHVkODM1XFx1ZGQ5ZFxcdWQ4MzVcXHVkZDZmXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTQ6MDM6MDQuNzEyMTAwXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE3NzQzOTg2MjE0XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNTQ3NTQwNjE5OF8xNzkwNjkwNTg2XzNcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0lOR19oZDlwOFwiLCBcInVzZXJfaWRcIjogODg2MDAxMjM3MiwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1MjJjNlxcdTIzZTRcXHUyMjViXFx1MjBkZFxcdWQ4MzVcXHVkZWIzXFx1YTljYVxcdTFjNDIgXFx1ZDgzNVxcdWRkZGVcXHUwMjZhXFx1MDU5ZlxcdTEzNWRcXHVkODM1XFx1ZGVjOFxcdWQ4MzVcXHVkYzA2IFxcdTIzZTRcXHUyMDBjXFx1MjAwY1xcdWQ4MDBcXHVkZmQzXFx1ZDgzNVxcdWRkOWRcXHVkODM1XFx1ZGQ2ZlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDE0OjAzOjA4LjAyNzA4OFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTgzMjI3NzY5MVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzU0NzU0MDYxOThfMTc5MDY5MDY0Ml8wXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcInZpc2hhbmFvXCIsIFwidXNlcl9pZFwiOiA4ODg1MjA5NTQ2LCBcImZpcnN0X25hbWVcIjogXCJcXHUxYmQzXFx1ZDgzNFxcdWRjNThcXHUwZWNkXFx1ZDgwY1xcdWRkYjBcXHVkODM1XFx1ZGViNFxcdWQ4MzVcXHVkZWM2XFx1ZDgzNVxcdWRlZGVcXHUwNTlmXFx1MTM1ZFxcdWQ4MzVcXHVkZWQ2c1xcdWQ4M2RcXHVkY2I4XFx1ZDgzNFxcdWRkNjVcXHUwMzY2XFx1ZDgzNFxcdWRkYWNcXHVhYmVkXFx1MjJjNlxcdTFhYjNcXHUxYWIzXFx1ZDgzNFxcdWRkNjVcXHUwMzY2XFx1ZDgzNFxcdWRkYWNcXHVhYmVkXFx1MDMzNiBcXHUyMGVhXFx1ZDgzNFxcdWRkMDRcXHVkODM0XFx1ZGQwMFxcdWFiZWRcXHUyN2Y2XFx1MjJjNjpcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowNDowMy45MzAwOTdcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCIxODE4NTMzODc2OFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzU0NzU0MDYxOThfMTc5MDY5MDY2M18xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcInplcm94eV8xMFwiLCBcInVzZXJfaWRcIjogODc3MzAxNjQ2NSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZmYzYVxcdWZmMjVcXHVmZjMyXFx1ZmYyZlxcdWZmMzhcXHVmZjM5IFxcdWQ4M2NcXHVkZGYxXFx1ZDgzY1xcdWRkZjdcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowNDoyNC41MTU3ODhcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk2OTI5NTQ3MjJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl81NDc1NDA2MTk4XzE3OTA2OTA2NzFfMlwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJObyB1c2VybmFtZVwiLCBcInVzZXJfaWRcIjogNjUwNTYwNjUzOSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1MjUwMFxcdWQ4MDdcXHVkYzJlXFx1MDBiMCBcXHVkODM1XFx1ZGMxMyBcXHVhOWNhXFx1YTljYVxcdTAzMzVcXHUwMGIwIFxcdTE5YzlcXHVkODNjXFx1ZGRmMVxcdWQ4M2NcXHVkZGU3IFxcdWQ4MzVcXHVkYzBiIFxcdTI3NGRcXHUzMDJiIFxcdTEwNDNcXHUwMGIwIFxcdWQ4MzVcXHVkZWRiIFxcdWQ4MGNcXHVkZGFhXFx1YWFiZVxcdTIxZTJcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowNDozMi42MTk4MjlcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk4MDU1NDYwMzVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl81NDc1NDA2MTk4XzE3OTA2OTA2NzVfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJObyB1c2VybmFtZVwiLCBcInVzZXJfaWRcIjogODkyMjc5NTIzNSwgXCJmaXJzdF9uYW1lXCI6IFwiTXIuXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTQ6MDQ6MzYuNTcxMDcyXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn1dIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogNjMyNTYyNTY1NCwgInVzZXJuYW1lIjogIkVHT3hEYyIsICJmaXJzdF9uYW1lIjogIvCdmZDwnZmJ8J2Yv/CdmYDwnZmB8J2ZgPCdmLzwnZmP8J2ZgPCdmL8g6q2X8J2QgyAhISEgeyDwnZGp8J2RsPCdkbPwnZGz8J2RsPCdkbbwnZG18J2RqPCdkbDwnZG58J2RrCB9IiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTE6MTk6MzEuMzg0MjQxIiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogbnVsbCwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDEsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In0sIHsidXNlcl9pZCI6IDY0MjQ5NTcyMTQsICJ1c2VybmFtZSI6ICIiLCAiZmlyc3RfbmFtZSI6ICIiLCAiam9pbmVkX2RhdGUiOiAiMjAyNi0wOS0yOSAxMDoyMjo0MC40MTk5NTIiLCAiaXNfYmFubmVkIjogMCwgImFjY2Vzc19leHBpcnkiOiBudWxsLCAic2hhcmVkX2lkX2xpbWl0IjogMCwgImlzX2FkbWluIjogMSwgImFjY291bnRzIjogIltdIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogNzQ4OTk4ODI2MiwgInVzZXJuYW1lIjogIkFBRElfNTIwMTE3IiwgImZpcnN0X25hbWUiOiAiQUFESSIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDEwOjE2OjQ1LjU3MjI3OSIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6IG51bGwsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAwLCAiYWNjb3VudHMiOiAiW10iLCAic2V0dGluZ3MiOiAie1wiZGVsYXlcIjogMC41fSJ9LCB7InVzZXJfaWQiOiA3NjM4NDM5MTQzLCAidXNlcm5hbWUiOiAiTE9SRFpFVFNVIiwgImZpcnN0X25hbWUiOiAi8J2ZivCdmYHwnZmBfiIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDEyOjE4OjAzLjI1MjM5NCIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6ICIyMTI0LTAzLTE5VDEyOjE4OjQ2LjUwMTE3MCIsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAwLCAiYWNjb3VudHMiOiAiW3tcInBob25lXCI6IFwiOTE4Nzc5Nzc3MjAyXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTU1XzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTm8gdXNlcm5hbWVcIiwgXCJ1c2VyX2lkXCI6IDYzNDQ1MTMwNjcsIFwiZmlyc3RfbmFtZVwiOiBcIkFkZHVcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjozNi42MDIzMTNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk1OTQ0NzYwOTFcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NTZfMVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJJVEFDSEl4WkVOTklYXCIsIFwidXNlcl9pZFwiOiA2NjcxODYwNTQ3LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODBjXFx1ZGRhOVxcdWQ4MzVcXHVkZGVkXFx1ZDgzNVxcdWRkZDhcXHVkODM1XFx1ZGRlMVxcdWQ4MzVcXHVkZGUxXFx1ZDgzNVxcdWRkZGNcXHVkODM1XFx1ZGRlYlxcdWQ4MGNcXHVkZGFhIFsjVEhFX1NISUVMRF1cIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjozNy44OTYxOTNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk4NjkxNzM5MjBcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NTdfMlwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJNQURBUkE2N1wiLCBcInVzZXJfaWRcIjogNjg1MDYwNjI3MiwgXCJmaXJzdF9uYW1lXCI6IFwiTUFEQVJBICNcXHVkODM1XFx1ZGU0ZlxcdWQ4MzVcXHVkZTQwXFx1ZDgzNVxcdWRlM2NcXHVkODM1XFx1ZGU0OF9cXHVkODM1XFx1ZGU1NVxcdWQ4MzVcXHVkZTRkXFx1ZDgzNVxcdWRlNTBcXHVkODM1XFx1ZGU1M1wiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjM5LjMxODcxNVwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjE1MDI1MTc5NTU0XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTU5XzNcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiVEVSRURPWFwiLCBcInVzZXJfaWRcIjogNzM5NjcwMTM4OSwgXCJmaXJzdF9uYW1lXCI6IFwiVEVSRURPWFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQwLjI2NDI1NVwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjE1MDE0ODkzMjkxXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTYwXzRcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTkFNSUtBWkVNSU5BVE8wOTI0XCIsIFwidXNlcl9pZFwiOiA2ODA2NDM0OTk1LCBcImZpcnN0X25hbWVcIjogXCJNSU5BVE8gTkFNSUtBWkVcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0MS4yMTk3MDZcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCIxNjE4NzYyODI0MFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2MV81XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkdPSk9TQVRVUm9paVwiLCBcInVzZXJfaWRcIjogNzQ5ODc3NzMyNiwgXCJmaXJzdF9uYW1lXCI6IFwiR09KTyBTQVRVUk9cIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0Mi4yMjQ0MDZcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5NjQ3ODI4ODA3MTAzXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTYyXzZcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0FLQVNISXhQUk9NT1RFUlwiLCBcInVzZXJfaWRcIjogODQyNzMzMTI0OSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRlNDZcXHVkODM1XFx1ZGUzY1xcdWQ4MzVcXHVkZTQ2XFx1ZDgzNVxcdWRlM2NcXHVkODM1XFx1ZGU0ZVxcdWQ4MzVcXHVkZTQzXFx1ZDgzNVxcdWRlNDQgXFx1ZDgzNVxcdWRlNDFcXHVkODM1XFx1ZGU0MVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQyLjMxNjU3NVwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjIzNDcwMjM0NjkxOThcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NjJfN1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJTQU1NWXhPQklUTzRcIiwgXCJ1c2VyX2lkXCI6IDg2ODQ4MDQzOTYsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZGQ3XFx1ZDgzNVxcdWRkZThcXHVkODM1XFx1ZGRlNlxcdWQ4MzVcXHVkZGU3XFx1ZDgzNVxcdWRkZGNcXHVkODM1XFx1ZGRlMVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQyLjQzMjg2MFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTg5Mjc1MTYxOVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2Ml84XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIlNBTU1ZeElUQUNISVwiLCBcInVzZXJfaWRcIjogODcwMjUzMTU4NCwgXCJmaXJzdF9uYW1lXCI6IFwiRkw0TUUgISEhXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NDMuNjk4MTIyXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE4OTc0MDY5NTQ0XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTY0XzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiSFlQRVJJQU54U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg5ODgwNDkwNDMsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZGRiXFx1ZDgzNVxcdWRkZWNcXHVkODM1XFx1ZGRlM1xcdWQ4MzVcXHVkZGQ4XFx1ZDgzNVxcdWRkZTVcXHVkODM1XFx1ZGRkY1xcdWQ4MzVcXHVkZGQ0XFx1ZDgzNVxcdWRkZTFcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0NS43NDIxMzNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCIxMzIwNDU4ODkxMlwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2NV8xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIktFUkVUT1N4WkVUU1VcIiwgXCJ1c2VyX2lkXCI6IDg1Nzk3MzM5OTksIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzBhXFx1ZDgzNVxcdWRjMDRcXHVkODM1XFx1ZGMxMVxcdWQ4MzVcXHVkYzA0XFx1ZDgzNVxcdWRjMTNcXHVkODM1XFx1ZGMwZVxcdWQ4MzVcXHVkYzEyXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NDYuNjQwMTk5XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE3NjMwMDU3NDAyXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTY2XzJcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0VOTll4WkVUU1VcIiwgXCJ1c2VyX2lkXCI6IDg4NTg1MTMwNzgsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzBhXFx1ZDgzNVxcdWRjMDRcXHVkODM1XFx1ZGMwZFxcdWQ4MzVcXHVkYzBkXFx1ZDgzNVxcdWRjMThcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0Ny45NjM3MzVcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTcwNjE4NTAxNjRcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NjdfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJDSEVNSVNUT1haRVRTVVwiLCBcInVzZXJfaWRcIjogNjcyNzg3NzgwOSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRkZDZcXHVkODM1XFx1ZGRkYlxcdWQ4MzVcXHVkZGQ4XFx1ZDgzNVxcdWRkZTBcXHVkODM1XFx1ZGRkY1xcdWQ4MzVcXHVkZGU2XFx1ZDgzNVxcdWRkZTdcXHVkODM1XFx1ZGRlMlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQ5LjI1NDUxOFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTI3MjQ3ODIxM1wiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2OV80XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIktZUk94U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg3NjA2MzI3NDUsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTQ2XFx1ZDgzNVxcdWRlNTRcXHVkODM1XFx1ZGU0ZFxcdWQ4MzVcXHVkZTRhXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTAuNTQ3NjU2XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5OTAyNTU4OTA4XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTcwXzVcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiVkFMQ094U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDgwODA2MDAwNDMsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzE1XFx1ZDgzNVxcdWRjMDBcXHVkODM1XFx1ZGMwYlxcdWQ4MzVcXHVkYzAyXFx1ZDgzNVxcdWRjMGUgXFx1ZDgzNVxcdWRjMGZcXHVkODM1XFx1ZGMxMVxcdWQ4MzVcXHVkYzA4XFx1ZDgzNVxcdWRjMGNcXHVkODM1XFx1ZGMwNFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjUxLjc3NDkwMFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTY5NjIyNzkwMVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU3MV82XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIk9DVUxVU3hTQU1NWVwiLCBcInVzZXJfaWRcIjogODE0NzkzMTEyMiwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRkZTJcXHVkODM1XFx1ZGRkNlxcdWQ4MzVcXHVkZGU4XFx1ZDgzNVxcdWRkZGZcXHVkODM1XFx1ZGRlOFxcdWQ4MzVcXHVkZGU2XCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTMuMTEzODQ4XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE3MDk2ODY1ODI4XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTczXzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiU0FTVUtFeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4OTAxMzMzNTg2LCBcImZpcnN0X25hbWVcIjogXCJTXFx1MWQwMHNcXHUxZDFjXFx1MWQwYlxcdTFkMDdcXHUzMTY0XFx1MmUxOVxcdTMxNjQhIVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjU1LjA2MDM1MlwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTc5MjI5ODM2N1wiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU3NV8xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkdBTEFYWVl4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg3NzM0NDYyMDMsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzZlXFx1ZDgzNVxcdWRjNjhcXHVkODM1XFx1ZGM3M1xcdWQ4MzVcXHVkYzY4XFx1ZDgzNVxcdWRjN2ZcXHVkODM1XFx1ZGM4MFxcdWQ4MzVcXHVkYzgwXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTYuMjM0NjY2XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5OTIzMjI3ODA4XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTc2XzJcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiU05PV1lZeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4ODIyMDMwMDk3LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGMxMlxcdWQ4MzVcXHVkYzBkXFx1ZDgzNVxcdWRjMGVcXHVkODM1XFx1ZGMxNlxcdWQ4MzVcXHVkYzE4XFx1ZDgzNVxcdWRjMThcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo1Ny40ODA0NTFcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk2MTkwNDAzNjVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NzdfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJIRVJNSVRYU0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg5MjQyNTM1NzYsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzZmXFx1ZDgzNVxcdWRjNmNcXHVkODM1XFx1ZGM3OVxcdWQ4MzVcXHVkYzc0XFx1ZDgzNVxcdWRjNzBcXHVkODM1XFx1ZGM3YlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjU4LjY5MTk2OFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxNzAwMTQwNjk4NFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU3OF80XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkZPWFhZWXhTQU1NWVwiLCBcInVzZXJfaWRcIjogODgwMTg0NDUyMSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRjMDVcXHVkODM1XFx1ZGMwZVxcdWQ4MzVcXHVkYzE3XFx1ZDgzNVxcdWRjMTdcXHVkODM1XFx1ZGMxOFxcdWQ4MzVcXHVkYzE4IFxcdWQ4M2VcXHVkZDhhXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTkuOTM1OTM1XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE4NzY2NzMwNzYxXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTc5XzVcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiREVBRHhaRVRTVVwiLCBcInVzZXJfaWRcIjogODc2NzU0NzY3NCwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRjNmJcXHVkODM1XFx1ZGM2Y1xcdWQ4MzVcXHVkYzY4XFx1ZDgzNVxcdWRjNmJcXHVkODM1XFx1ZGM3YVxcdWQ4MzVcXHVkYzdiXFx1ZDgzNVxcdWRjNzZcXHVkODM1XFx1ZGM3NVxcdWQ4MzVcXHVkYzZjXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjM6MDEuMjU3NTQxXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5MzcxOTYzMzU3XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTgxXzZcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiSEVST0JSSU5FeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4NDI5NzMzNjM4LCBcImZpcnN0X25hbWVcIjogXCJIXFx1MWQwN1xcdTAyODBcXHUxZDBmXFx1MDI5OVxcdTAyODBcXHUwMjZhXFx1MDI3NFxcdTFkMDdcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzowMi42MDAyMjlcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk3NTY1Nzc5NTJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1ODJfN1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJSQVl4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg5NjA4OTQ0OTgsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTRkXFx1ZDgzNVxcdWRlM2NcXHVkODM1XFx1ZGU1NFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIzOjA0LjA0MDU2OFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxNzk5OTM2MTIxMFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDYxOF8wXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIlJFWFhZWUhVXCIsIFwidXNlcl9pZFwiOiA4OTU4MDc0Nzc3LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGRlNVxcdWQ4MzVcXHVkZGQ4XFx1ZDgzNVxcdWRkZWJcXHVkODM1XFx1ZGRlYlxcdWQ4MzVcXHVkZGVjXFx1ZDgzNVxcdWRkZWNcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0MS4zMzIxNzZcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTgxMjMzNjg5NjlcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjFfMVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJGTEVYWFlZeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4ODQ4NTc3ODUyLCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGMwNVxcdWQ4MzVcXHVkYzBiXFx1ZDgzNVxcdWRjMDRcXHVkODM1XFx1ZGMxN1xcdWQ4MzVcXHVkYzE3XFx1ZDgzNVxcdWRjMThcXHVkODM1XFx1ZGMxOFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIzOjQyLjU1NjQ0MlwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTM0NDcwMzY0OFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDYyMl8yXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIk5VTEx4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg3NDUyOTkzNDksIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTQ5XFx1ZDgzNVxcdWRlNTBcXHVkODM1XFx1ZGU0N1xcdWQ4MzVcXHVkZTQ3IFxcdWQ4MzVcXHVkZTUzXFx1ZDgzNVxcdWRlM2ZcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0My43NTM3OThcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk0ODU2NDQyMjRcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjNfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJWT0lEWFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4NjEwODE1MjQ5LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGM3ZFxcdWQ4MzVcXHVkYzc2XFx1ZDgzNVxcdWRjNzBcXHVkODM1XFx1ZGM2YiBcXHVkODM1XFx1ZGM2ZlxcdWQ4MzVcXHVkYzdjXFx1ZDgzNVxcdWRjNzUgXFx1ZDgzZVxcdWRkNDBcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0NS4wODE4NzVcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTc4MDQwMDg1MDRcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjVfNFwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJaWUxPSFVCS0xcIiwgXCJ1c2VyX2lkXCI6IDg1NTM5ODQ0NTUsIFwiZmlyc3RfbmFtZVwiOiBcIlpZTE8gSFVcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0Ni4zMjM0MzhcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk2MDgzOTQzMTJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjZfNVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJDUllTT3haRVRTVVwiLCBcInVzZXJfaWRcIjogODY3Njg0MTIwMCwgXCJmaXJzdF9uYW1lXCI6IFwiQ1JZU08gXFx1ZDgzY1xcdWRmMmFcXHVmZTBmXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjM6NDcuNTg3MDE2XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5MDYwNDgyNjI3XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NjI3XzZcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTElHSFR4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg4MDEwNzQxNjAsIFwiZmlyc3RfbmFtZVwiOiBcIkxJR0hUIFxcdWQ4M2RcXHVkYzk0XCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjM6NDguNzgzMzEyXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn1dIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogNzc1OTQ5ODM2NiwgInVzZXJuYW1lIjogImxsX0RSS19JSUkiLCAiZmlyc3RfbmFtZSI6ICJMRVNTIE9OTElORSBUSUxMIDIwVEggT0NUIiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTE6MjM6NDIuNTkzNDE2IiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogbnVsbCwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDAsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In0sIHsidXNlcl9pZCI6IDc3OTMwNTYxMjQsICJ1c2VybmFtZSI6ICIiLCAiZmlyc3RfbmFtZSI6ICIiLCAiam9pbmVkX2RhdGUiOiAiMjAyNi0wOS0yOSAxNDowNjo0OC42MjU0MDciLCAiaXNfYmFubmVkIjogMCwgImFjY2Vzc19leHBpcnkiOiAiMjA1NC0wMi0xNFQxNDowODo0MS4yOTY0NDMiLCAic2hhcmVkX2lkX2xpbWl0IjogOTAsICJpc19hZG1pbiI6IDEsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In0sIHsidXNlcl9pZCI6IDgxMDUwODMyODEsICJ1c2VybmFtZSI6ICJUZ2tub3duemVvbiIsICJmaXJzdF9uYW1lIjogIvCdmJfwnZiI8J2YkPCdmIsg8J2Yl/CdmJnwnZiW8J2YlPCdmJbwnZib8J2YjPCdmJkiLCAiam9pbmVkX2RhdGUiOiAiMjAyNi0wOS0yOSAxMDoyODoxNS40MzY3MjciLCAiaXNfYmFubmVkIjogMCwgImFjY2Vzc19leHBpcnkiOiAiMjA4MS0wNy0wMlQxMjoyODozMy45MDIyNjciLCAic2hhcmVkX2lkX2xpbWl0IjogMCwgImlzX2FkbWluIjogMCwgImFjY291bnRzIjogIltdIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogODE1MzA3NjA4OCwgInVzZXJuYW1lIjogIk9XTkVSX08xIiwgImZpcnN0X25hbWUiOiAi8JGyrfCRsq3wlqOY4o+kzZ/Nns2fzZ4g8J2QgPCdkJnwnZCA8J2QgPCdkI0g8J2QgfCdkIDwnZCH8J2QiCDij6TNn82ezZ/NniDwlqOY8JGyrfCRsq0iLCAiam9pbmVkX2RhdGUiOiAiMjAyNi0wOS0yOSAxMDo0NTozMC4zMzY5MjIiLCAiaXNfYmFubmVkIjogMCwgImFjY2Vzc19leHBpcnkiOiBudWxsLCAic2hhcmVkX2lkX2xpbWl0IjogMCwgImlzX2FkbWluIjogMCwgImFjY291bnRzIjogIltdIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogODE3MDA0NTI0NywgInVzZXJuYW1lIjogIlplcm94T0dJIiwgImZpcnN0X25hbWUiOiAiRVJBIEVORCDwn6WAIiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTA6MjY6MTUuMjkxMjQ1IiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogbnVsbCwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDAsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In0sIHsidXNlcl9pZCI6IDgyNTQxMzgxMjMsICJ1c2VybmFtZSI6ICJNYXJjb0VyYVhkIiwgImZpcnN0X25hbWUiOiAi8JOGqfCdkIzwnZCa8J2Qq/CdkJzwnZCoIPCdkJfwnZCd4aqzIOKdriDwnZen8J2Xm/Cdl5gg8J2XlfCdl6XwnZeU8J2XofCdl5cg8J+SgOKdryDwk4ap8J2ag/CdmbTwnZmw8J2ZvF/wnZm+8J2ZtV/wnZqE8J2ZvfCdmbPwnZm08J2agfCdmoXwnZm+8J2ZuPCdmbPwk4aqIiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTA6MTE6MTMuMTU1MDgwIiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogbnVsbCwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDAsICJhY2NvdW50cyI6ICJbe1wicGhvbmVcIjogXCIrOTE5NDA2MDQxMTAyXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfODI1NDEzODEyM18xNzkwNjc3MzkxXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIlpleHVOdnJEaWVcIiwgXCJ1c2VyX2lkXCI6IDg4NTg0ODIxNjAsIFwiZmlyc3RfbmFtZVwiOiBcIlpleHUgTnZyIERpZSBcXHVkODNjXFx1ZGY4MFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEwOjIzOjI3LjM3MTYzOVwiLCBcInR5cGVcIjogXCJwaG9uZV9vdHBcIn0sIHtcInBob25lXCI6IFwiOTE4Nzc5Nzc3MjAyXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTU1XzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTm8gdXNlcm5hbWVcIiwgXCJ1c2VyX2lkXCI6IDYzNDQ1MTMwNjcsIFwiZmlyc3RfbmFtZVwiOiBcIkFkZHVcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjozNi42MDIzMTNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk1OTQ0NzYwOTFcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NTZfMVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJJVEFDSEl4WkVOTklYXCIsIFwidXNlcl9pZFwiOiA2NjcxODYwNTQ3LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODBjXFx1ZGRhOVxcdWQ4MzVcXHVkZGVkXFx1ZDgzNVxcdWRkZDhcXHVkODM1XFx1ZGRlMVxcdWQ4MzVcXHVkZGUxXFx1ZDgzNVxcdWRkZGNcXHVkODM1XFx1ZGRlYlxcdWQ4MGNcXHVkZGFhIFsjVEhFX1NISUVMRF1cIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjozNy44OTYxOTNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk4NjkxNzM5MjBcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NTdfMlwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJNQURBUkE2N1wiLCBcInVzZXJfaWRcIjogNjg1MDYwNjI3MiwgXCJmaXJzdF9uYW1lXCI6IFwiTUFEQVJBICNcXHVkODM1XFx1ZGU0ZlxcdWQ4MzVcXHVkZTQwXFx1ZDgzNVxcdWRlM2NcXHVkODM1XFx1ZGU0OF9cXHVkODM1XFx1ZGU1NVxcdWQ4MzVcXHVkZTRkXFx1ZDgzNVxcdWRlNTBcXHVkODM1XFx1ZGU1M1wiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjM5LjMxODcxNVwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjE1MDI1MTc5NTU0XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTU5XzNcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiVEVSRURPWFwiLCBcInVzZXJfaWRcIjogNzM5NjcwMTM4OSwgXCJmaXJzdF9uYW1lXCI6IFwiVEVSRURPWFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQwLjI2NDI1NVwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjE1MDE0ODkzMjkxXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTYwXzRcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTkFNSUtBWkVNSU5BVE8wOTI0XCIsIFwidXNlcl9pZFwiOiA2ODA2NDM0OTk1LCBcImZpcnN0X25hbWVcIjogXCJNSU5BVE8gTkFNSUtBWkVcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0MS4yMTk3MDZcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCIxNjE4NzYyODI0MFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2MV81XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkdPSk9TQVRVUm9paVwiLCBcInVzZXJfaWRcIjogNzQ5ODc3NzMyNiwgXCJmaXJzdF9uYW1lXCI6IFwiR09KTyBTQVRVUk9cIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0Mi4yMjQ0MDZcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5NjQ3ODI4ODA3MTAzXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTYyXzZcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0FLQVNISXhQUk9NT1RFUlwiLCBcInVzZXJfaWRcIjogODQyNzMzMTI0OSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRlNDZcXHVkODM1XFx1ZGUzY1xcdWQ4MzVcXHVkZTQ2XFx1ZDgzNVxcdWRlM2NcXHVkODM1XFx1ZGU0ZVxcdWQ4MzVcXHVkZTQzXFx1ZDgzNVxcdWRlNDQgXFx1ZDgzNVxcdWRlNDFcXHVkODM1XFx1ZGU0MVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQyLjMxNjU3NVwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjIzNDcwMjM0NjkxOThcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NjJfN1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJTQU1NWXhPQklUTzRcIiwgXCJ1c2VyX2lkXCI6IDg2ODQ4MDQzOTYsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZGQ3XFx1ZDgzNVxcdWRkZThcXHVkODM1XFx1ZGRlNlxcdWQ4MzVcXHVkZGU3XFx1ZDgzNVxcdWRkZGNcXHVkODM1XFx1ZGRlMVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQyLjQzMjg2MFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTg5Mjc1MTYxOVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2Ml84XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIlNBTU1ZeElUQUNISVwiLCBcInVzZXJfaWRcIjogODcwMjUzMTU4NCwgXCJmaXJzdF9uYW1lXCI6IFwiRkw0TUUgISEhXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NDMuNjk4MTIyXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE4OTc0MDY5NTQ0XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTY0XzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiSFlQRVJJQU54U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg5ODgwNDkwNDMsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZGRiXFx1ZDgzNVxcdWRkZWNcXHVkODM1XFx1ZGRlM1xcdWQ4MzVcXHVkZGQ4XFx1ZDgzNVxcdWRkZTVcXHVkODM1XFx1ZGRkY1xcdWQ4MzVcXHVkZGQ0XFx1ZDgzNVxcdWRkZTFcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0NS43NDIxMzNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCIxMzIwNDU4ODkxMlwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2NV8xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIktFUkVUT1N4WkVUU1VcIiwgXCJ1c2VyX2lkXCI6IDg1Nzk3MzM5OTksIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzBhXFx1ZDgzNVxcdWRjMDRcXHVkODM1XFx1ZGMxMVxcdWQ4MzVcXHVkYzA0XFx1ZDgzNVxcdWRjMTNcXHVkODM1XFx1ZGMwZVxcdWQ4MzVcXHVkYzEyXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NDYuNjQwMTk5XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE3NjMwMDU3NDAyXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTY2XzJcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0VOTll4WkVUU1VcIiwgXCJ1c2VyX2lkXCI6IDg4NTg1MTMwNzgsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzBhXFx1ZDgzNVxcdWRjMDRcXHVkODM1XFx1ZGMwZFxcdWQ4MzVcXHVkYzBkXFx1ZDgzNVxcdWRjMThcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo0Ny45NjM3MzVcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTcwNjE4NTAxNjRcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NjdfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJDSEVNSVNUT1haRVRTVVwiLCBcInVzZXJfaWRcIjogNjcyNzg3NzgwOSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRkZDZcXHVkODM1XFx1ZGRkYlxcdWQ4MzVcXHVkZGQ4XFx1ZDgzNVxcdWRkZTBcXHVkODM1XFx1ZGRkY1xcdWQ4MzVcXHVkZGU2XFx1ZDgzNVxcdWRkZTdcXHVkODM1XFx1ZGRlMlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjQ5LjI1NDUxOFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTI3MjQ3ODIxM1wiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU2OV80XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIktZUk94U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg3NjA2MzI3NDUsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTQ2XFx1ZDgzNVxcdWRlNTRcXHVkODM1XFx1ZGU0ZFxcdWQ4MzVcXHVkZTRhXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTAuNTQ3NjU2XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5OTAyNTU4OTA4XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTcwXzVcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiVkFMQ094U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDgwODA2MDAwNDMsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzE1XFx1ZDgzNVxcdWRjMDBcXHVkODM1XFx1ZGMwYlxcdWQ4MzVcXHVkYzAyXFx1ZDgzNVxcdWRjMGUgXFx1ZDgzNVxcdWRjMGZcXHVkODM1XFx1ZGMxMVxcdWQ4MzVcXHVkYzA4XFx1ZDgzNVxcdWRjMGNcXHVkODM1XFx1ZGMwNFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjUxLjc3NDkwMFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTY5NjIyNzkwMVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU3MV82XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIk9DVUxVU3hTQU1NWVwiLCBcInVzZXJfaWRcIjogODE0NzkzMTEyMiwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRkZTJcXHVkODM1XFx1ZGRkNlxcdWQ4MzVcXHVkZGU4XFx1ZDgzNVxcdWRkZGZcXHVkODM1XFx1ZGRlOFxcdWQ4MzVcXHVkZGU2XCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTMuMTEzODQ4XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE3MDk2ODY1ODI4XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTczXzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiU0FTVUtFeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4OTAxMzMzNTg2LCBcImZpcnN0X25hbWVcIjogXCJTXFx1MWQwMHNcXHUxZDFjXFx1MWQwYlxcdTFkMDdcXHUzMTY0XFx1MmUxOVxcdTMxNjQhIVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjU1LjA2MDM1MlwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTc5MjI5ODM2N1wiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU3NV8xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkdBTEFYWVl4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg3NzM0NDYyMDMsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzZlXFx1ZDgzNVxcdWRjNjhcXHVkODM1XFx1ZGM3M1xcdWQ4MzVcXHVkYzY4XFx1ZDgzNVxcdWRjN2ZcXHVkODM1XFx1ZGM4MFxcdWQ4MzVcXHVkYzgwXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTYuMjM0NjY2XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5OTIzMjI3ODA4XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTc2XzJcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiU05PV1lZeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4ODIyMDMwMDk3LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGMxMlxcdWQ4MzVcXHVkYzBkXFx1ZDgzNVxcdWRjMGVcXHVkODM1XFx1ZGMxNlxcdWQ4MzVcXHVkYzE4XFx1ZDgzNVxcdWRjMThcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMjo1Ny40ODA0NTFcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk2MTkwNDAzNjVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1NzdfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJIRVJNSVRYU0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg5MjQyNTM1NzYsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkYzZmXFx1ZDgzNVxcdWRjNmNcXHVkODM1XFx1ZGM3OVxcdWQ4MzVcXHVkYzc0XFx1ZDgzNVxcdWRjNzBcXHVkODM1XFx1ZGM3YlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIyOjU4LjY5MTk2OFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxNzAwMTQwNjk4NFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDU3OF80XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkZPWFhZWXhTQU1NWVwiLCBcInVzZXJfaWRcIjogODgwMTg0NDUyMSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRjMDVcXHVkODM1XFx1ZGMwZVxcdWQ4MzVcXHVkYzE3XFx1ZDgzNVxcdWRjMTdcXHVkODM1XFx1ZGMxOFxcdWQ4MzVcXHVkYzE4IFxcdWQ4M2VcXHVkZDhhXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjI6NTkuOTM1OTM1XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE4NzY2NzMwNzYxXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTc5XzVcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiREVBRHhaRVRTVVwiLCBcInVzZXJfaWRcIjogODc2NzU0NzY3NCwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRjNmJcXHVkODM1XFx1ZGM2Y1xcdWQ4MzVcXHVkYzY4XFx1ZDgzNVxcdWRjNmJcXHVkODM1XFx1ZGM3YVxcdWQ4MzVcXHVkYzdiXFx1ZDgzNVxcdWRjNzZcXHVkODM1XFx1ZGM3NVxcdWQ4MzVcXHVkYzZjXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjM6MDEuMjU3NTQxXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5MzcxOTYzMzU3XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NTgxXzZcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiSEVST0JSSU5FeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4NDI5NzMzNjM4LCBcImZpcnN0X25hbWVcIjogXCJIXFx1MWQwN1xcdTAyODBcXHUxZDBmXFx1MDI5OVxcdTAyODBcXHUwMjZhXFx1MDI3NFxcdTFkMDdcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzowMi42MDAyMjlcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk3NTY1Nzc5NTJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ1ODJfN1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJSQVl4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg5NjA4OTQ0OTgsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTRkXFx1ZDgzNVxcdWRlM2NcXHVkODM1XFx1ZGU1NFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIzOjA0LjA0MDU2OFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxNzk5OTM2MTIxMFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDYxOF8wXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIlJFWFhZWUhVXCIsIFwidXNlcl9pZFwiOiA4OTU4MDc0Nzc3LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGRlNVxcdWQ4MzVcXHVkZGQ4XFx1ZDgzNVxcdWRkZWJcXHVkODM1XFx1ZGRlYlxcdWQ4MzVcXHVkZGVjXFx1ZDgzNVxcdWRkZWNcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0MS4zMzIxNzZcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTgxMjMzNjg5NjlcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjFfMVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJGTEVYWFlZeFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4ODQ4NTc3ODUyLCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGMwNVxcdWQ4MzVcXHVkYzBiXFx1ZDgzNVxcdWRjMDRcXHVkODM1XFx1ZGMxN1xcdWQ4MzVcXHVkYzE3XFx1ZDgzNVxcdWRjMThcXHVkODM1XFx1ZGMxOFwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjIzOjQyLjU1NjQ0MlwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTM0NDcwMzY0OFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzc2Mzg0MzkxNDNfMTc5MDY4NDYyMl8yXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIk5VTEx4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg3NDUyOTkzNDksIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTQ5XFx1ZDgzNVxcdWRlNTBcXHVkODM1XFx1ZGU0N1xcdWQ4MzVcXHVkZTQ3IFxcdWQ4MzVcXHVkZTUzXFx1ZDgzNVxcdWRlM2ZcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0My43NTM3OThcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk0ODU2NDQyMjRcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjNfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJWT0lEWFNBTU1ZXCIsIFwidXNlcl9pZFwiOiA4NjEwODE1MjQ5LCBcImZpcnN0X25hbWVcIjogXCJcXHVkODM1XFx1ZGM3ZFxcdWQ4MzVcXHVkYzc2XFx1ZDgzNVxcdWRjNzBcXHVkODM1XFx1ZGM2YiBcXHVkODM1XFx1ZGM2ZlxcdWQ4MzVcXHVkYzdjXFx1ZDgzNVxcdWRjNzUgXFx1ZDgzZVxcdWRkNDBcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0NS4wODE4NzVcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTc4MDQwMDg1MDRcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjVfNFwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJaWUxPSFVCS0xcIiwgXCJ1c2VyX2lkXCI6IDg1NTM5ODQ0NTUsIFwiZmlyc3RfbmFtZVwiOiBcIlpZTE8gSFVcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjoyMzo0Ni4zMjM0MzhcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk2MDgzOTQzMTJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl83NjM4NDM5MTQzXzE3OTA2ODQ2MjZfNVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJDUllTT3haRVRTVVwiLCBcInVzZXJfaWRcIjogODY3Njg0MTIwMCwgXCJmaXJzdF9uYW1lXCI6IFwiQ1JZU08gXFx1ZDgzY1xcdWRmMmFcXHVmZTBmXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjM6NDcuNTg3MDE2XCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE5MDYwNDgyNjI3XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNzYzODQzOTE0M18xNzkwNjg0NjI3XzZcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTElHSFR4U0FNTVlcIiwgXCJ1c2VyX2lkXCI6IDg4MDEwNzQxNjAsIFwiZmlyc3RfbmFtZVwiOiBcIkxJR0hUIFxcdWQ4M2RcXHVkYzk0XCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MjM6NDguNzgzMzEyXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiKzE2MzkyOTY2NTAyXCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfODI1NDEzODEyM18xNzkwNjg1MjE0XCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIkJ0d1p5YXJlWFwiLCBcInVzZXJfaWRcIjogODg2OTk0MTA5OSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZDgzNVxcdWRlNTVcXHVkODM1XFx1ZGU1NFxcdWQ4MzVcXHVkZTNjXFx1ZDgzNVxcdWRlNGRcXHVkODM1XFx1ZGU0MFxcdWQ4MzVcXHVkZTUzXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MzM6NTYuNzE0NzYyXCIsIFwidHlwZVwiOiBcInBob25lX290cFwifSwge1wicGhvbmVcIjogXCIrMTMyNTc3NzY5MzJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl84MjU0MTM4MTIzXzE3OTA2ODUyNjhcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiRnJlZUhvc3RpbmdYMVN1cHBvcnRcIiwgXCJ1c2VyX2lkXCI6IDYyOTM2NzQwNDAsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdWQ4MzVcXHVkZTQxXFx1ZDgzNVxcdWRlNGRcXHVkODM1XFx1ZGU0MFxcdWQ4MzVcXHVkZTQwIFxcdWQ4MzVcXHVkZTQzXFx1ZDgzNVxcdWRlNGFcXHVkODM1XFx1ZGU0ZVxcdWQ4MzVcXHVkZTRmXFx1ZDgzNVxcdWRlNDRcXHVkODM1XFx1ZGU0OVxcdWQ4MzVcXHVkZTQyIFxcdWQ4MzVcXHVkZTUzXFx1ZDgzNVxcdWRmZWQgXFx1ZDgzY1xcdWRmODBcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxMjozNDo0Ny4zOTY0NDRcIiwgXCJ0eXBlXCI6IFwicGhvbmVfb3RwXCJ9LCB7XCJwaG9uZVwiOiBcIisxNjcyODc3NjgzMVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzgyNTQxMzgxMjNfMTc5MDY4NTMwOVwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJObyB1c2VybmFtZVwiLCBcInVzZXJfaWRcIjogODk3MzI0MzUyMywgXCJmaXJzdF9uYW1lXCI6IFwiI1xcdWQ4MzVcXHVkYzExflxcdWQ4M2NcXHVkZGZhXFx1ZDgzY1xcdWRkZjhcXHUyNmExXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTI6MzU6NDAuNjkzMDcyXCIsIFwidHlwZVwiOiBcInBob25lX290cFwifSwge1wicGhvbmVcIjogXCIrMTcwNTgyMDUzMDVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl84MjU0MTM4MTIzXzE3OTA2ODUzNjFcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiTm8gdXNlcm5hbWVcIiwgXCJ1c2VyX2lkXCI6IDg5OTM1MDE5NTQsIFwiZmlyc3RfbmFtZVwiOiBcIiNcXHVkODM1XFx1ZGMxMX5cXHVkODNjXFx1ZGRmYVxcdWQ4M2NcXHVkZGY4XFx1MjZhMVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjM2OjE3Ljc1OTQ5MlwiLCBcInR5cGVcIjogXCJwaG9uZV9vdHBcIn0sIHtcInBob25lXCI6IFwiKzkxNjI2Nzc2MzU4OFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzgyNTQxMzgxMjNfMTc5MDY4NTM5N1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJyeWV6b3hcIiwgXCJ1c2VyX2lkXCI6IDg5NTkwODU1ODQsIFwiZmlyc3RfbmFtZVwiOiBcIiNcXHVkODM1XFx1ZGMxMX5cXHVkODNjXFx1ZGRmYVxcdWQ4M2NcXHVkZGY4XFx1MjZhMVwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDEyOjM2OjU3LjE5MjAzNlwiLCBcInR5cGVcIjogXCJwaG9uZV9vdHBcIn0sIHtcInBob25lXCI6IFwiOTE5MDM5NDE1MTU3XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNTQ3NTQwNjE5OF8xNzkwNjkwNTc0XzBcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0lOR19qc3RtdlwiLCBcInVzZXJfaWRcIjogODkwOTg1MTUwOCwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1MjJjNlxcdTIzZTRcXHUyMjViXFx1MjBkZFxcdWQ4MzVcXHVkZWIzXFx1YTljYVxcdTFjNDIgXFx1ZDgzNVxcdWRkZGVcXHUwMjZhXFx1MDU5ZlxcdTEzNWRcXHVkODM1XFx1ZGVjOFxcdWQ4MzVcXHVkYzA2IFxcdTIzZTRcXHUyMDBjXFx1MjAwY1xcdWQ4MDBcXHVkZmQzXFx1ZDgzNVxcdWRkOWRcXHVkODM1XFx1ZGQ2ZlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDE0OjAyOjU2LjExMTk0MFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxNzk4NzkzNzQ4NFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzU0NzU0MDYxOThfMTc5MDY5MDU3OF8xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcIktJTkdfamdtNWVcIiwgXCJ1c2VyX2lkXCI6IDg2MjI2Nzk1ODQsIFwiZmlyc3RfbmFtZVwiOiBcIlxcdTIyYzZcXHUyM2U0XFx1MjI1YlxcdTIwZGRcXHVkODM1XFx1ZGViM1xcdWE5Y2FcXHUxYzQyIFxcdWQ4MzVcXHVkZGRlXFx1MDI2YVxcdTA1OWZcXHUxMzVkXFx1ZDgzNVxcdWRlYzhcXHVkODM1XFx1ZGMwNiBcXHUyM2U0XFx1MjAwY1xcdTIwMGNcXHVkODAwXFx1ZGZkM1xcdWQ4MzVcXHVkZDlkXFx1ZDgzNVxcdWRkNmZcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowMjo1OS40MzA3NTNcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTkzNTkxMDUyNDVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl81NDc1NDA2MTk4XzE3OTA2OTA1ODNfMlwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJLSU5HX3BwMWduXCIsIFwidXNlcl9pZFwiOiA4NzY4MjY3NTEwLCBcImZpcnN0X25hbWVcIjogXCJcXHUyMmM2XFx1MjNlNFxcdTIyNWJcXHUyMGRkXFx1ZDgzNVxcdWRlYjNcXHVhOWNhXFx1MWM0MiBcXHVkODM1XFx1ZGRkZVxcdTAyNmFcXHUwNTlmXFx1MTM1ZFxcdWQ4MzVcXHVkZWM4XFx1ZDgzNVxcdWRjMDYgXFx1MjNlNFxcdTIwMGNcXHUyMDBjXFx1ZDgwMFxcdWRmZDNcXHVkODM1XFx1ZGQ5ZFxcdWQ4MzVcXHVkZDZmXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTQ6MDM6MDQuNzEyMTAwXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn0sIHtcInBob25lXCI6IFwiOTE3NzQzOTg2MjE0XCIsIFwic2Vzc2lvblwiOiBcInNlc3Npb25zL3VzZXJfNTQ3NTQwNjE5OF8xNzkwNjkwNTg2XzNcIiwgXCJzZXNzaW9uX3N0cmluZ1wiOiBudWxsLCBcInVzZXJuYW1lXCI6IFwiS0lOR19oZDlwOFwiLCBcInVzZXJfaWRcIjogODg2MDAxMjM3MiwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1MjJjNlxcdTIzZTRcXHUyMjViXFx1MjBkZFxcdWQ4MzVcXHVkZWIzXFx1YTljYVxcdTFjNDIgXFx1ZDgzNVxcdWRkZGVcXHUwMjZhXFx1MDU5ZlxcdTEzNWRcXHVkODM1XFx1ZGVjOFxcdWQ4MzVcXHVkYzA2IFxcdTIzZTRcXHUyMDBjXFx1MjAwY1xcdWQ4MDBcXHVkZmQzXFx1ZDgzNVxcdWRkOWRcXHVkODM1XFx1ZGQ2ZlwiLCBcImFkZGVkX2RhdGVcIjogXCIyMDI2LTA5LTI5IDE0OjAzOjA4LjAyNzA4OFwiLCBcInR5cGVcIjogXCJ6aXBfc2Vzc2lvbl9maWxlXCJ9LCB7XCJwaG9uZVwiOiBcIjkxOTgzMjI3NzY5MVwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzU0NzU0MDYxOThfMTc5MDY5MDY0Ml8wXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcInZpc2hhbmFvXCIsIFwidXNlcl9pZFwiOiA4ODg1MjA5NTQ2LCBcImZpcnN0X25hbWVcIjogXCJcXHUxYmQzXFx1ZDgzNFxcdWRjNThcXHUwZWNkXFx1ZDgwY1xcdWRkYjBcXHVkODM1XFx1ZGViNFxcdWQ4MzVcXHVkZWM2XFx1ZDgzNVxcdWRlZGVcXHUwNTlmXFx1MTM1ZFxcdWQ4MzVcXHVkZWQ2c1xcdWQ4M2RcXHVkY2I4XFx1ZDgzNFxcdWRkNjVcXHUwMzY2XFx1ZDgzNFxcdWRkYWNcXHVhYmVkXFx1MjJjNlxcdTFhYjNcXHUxYWIzXFx1ZDgzNFxcdWRkNjVcXHUwMzY2XFx1ZDgzNFxcdWRkYWNcXHVhYmVkXFx1MDMzNiBcXHUyMGVhXFx1ZDgzNFxcdWRkMDRcXHVkODM0XFx1ZGQwMFxcdWFiZWRcXHUyN2Y2XFx1MjJjNjpcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowNDowMy45MzAwOTdcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCIxODE4NTMzODc2OFwiLCBcInNlc3Npb25cIjogXCJzZXNzaW9ucy91c2VyXzU0NzU0MDYxOThfMTc5MDY5MDY2M18xXCIsIFwic2Vzc2lvbl9zdHJpbmdcIjogbnVsbCwgXCJ1c2VybmFtZVwiOiBcInplcm94eV8xMFwiLCBcInVzZXJfaWRcIjogODc3MzAxNjQ2NSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1ZmYzYVxcdWZmMjVcXHVmZjMyXFx1ZmYyZlxcdWZmMzhcXHVmZjM5IFxcdWQ4M2NcXHVkZGYxXFx1ZDgzY1xcdWRkZjdcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowNDoyNC41MTU3ODhcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk2OTI5NTQ3MjJcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl81NDc1NDA2MTk4XzE3OTA2OTA2NzFfMlwiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJObyB1c2VybmFtZVwiLCBcInVzZXJfaWRcIjogNjUwNTYwNjUzOSwgXCJmaXJzdF9uYW1lXCI6IFwiXFx1MjUwMFxcdWQ4MDdcXHVkYzJlXFx1MDBiMCBcXHVkODM1XFx1ZGMxMyBcXHVhOWNhXFx1YTljYVxcdTAzMzVcXHUwMGIwIFxcdTE5YzlcXHVkODNjXFx1ZGRmMVxcdWQ4M2NcXHVkZGU3IFxcdWQ4MzVcXHVkYzBiIFxcdTI3NGRcXHUzMDJiIFxcdTEwNDNcXHUwMGIwIFxcdWQ4MzVcXHVkZWRiIFxcdWQ4MGNcXHVkZGFhXFx1YWFiZVxcdTIxZTJcIiwgXCJhZGRlZF9kYXRlXCI6IFwiMjAyNi0wOS0yOSAxNDowNDozMi42MTk4MjlcIiwgXCJ0eXBlXCI6IFwiemlwX3Nlc3Npb25fZmlsZVwifSwge1wicGhvbmVcIjogXCI5MTk4MDU1NDYwMzVcIiwgXCJzZXNzaW9uXCI6IFwic2Vzc2lvbnMvdXNlcl81NDc1NDA2MTk4XzE3OTA2OTA2NzVfM1wiLCBcInNlc3Npb25fc3RyaW5nXCI6IG51bGwsIFwidXNlcm5hbWVcIjogXCJObyB1c2VybmFtZVwiLCBcInVzZXJfaWRcIjogODkyMjc5NTIzNSwgXCJmaXJzdF9uYW1lXCI6IFwiTXIuXCIsIFwiYWRkZWRfZGF0ZVwiOiBcIjIwMjYtMDktMjkgMTQ6MDQ6MzYuNTcxMDcyXCIsIFwidHlwZVwiOiBcInppcF9zZXNzaW9uX2ZpbGVcIn1dIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogODM4MTUwNDc1MywgInVzZXJuYW1lIjogIkl0elVzZXJuYXJ1dG8wMCIsICJmaXJzdF9uYW1lIjogIk5hcnV0byAwNyIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDEwOjI5OjA3LjM5NDg3MSIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6IG51bGwsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAwLCAiYWNjb3VudHMiOiAiW10iLCAic2V0dGluZ3MiOiAie1wiZGVsYXlcIjogMC41fSJ9LCB7InVzZXJfaWQiOiA4NDY4Mzk0OTI3LCAidXNlcm5hbWUiOiAiS05PV19VUl9EQUREWSIsICJmaXJzdF9uYW1lIjogIuKLhuKOr+GNndaf4Y2d1p/ijq/hqrUuIPCdkI0g8J2QgCDwnZCIIPCdkJMg8J2QiCDwnZCKIOKPpOKAjOKcmCDigLkvL+KAuvCfp78gI/CdkI/wnZCI8J2QkeODoPCdkJPwnZCE8J2QkiIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDEwOjEyOjM3Ljc2MjM3NSIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6IG51bGwsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAxLCAiYWNjb3VudHMiOiAiW10iLCAic2V0dGluZ3MiOiAie1wiZGVsYXlcIjogMC41fSJ9LCB7InVzZXJfaWQiOiA4NTMxNTE4MDM2LCAidXNlcm5hbWUiOiAiT2theXk3MjcyIiwgImZpcnN0X25hbWUiOiAiLiIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDExOjQwOjM1LjU3MTYzOSIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6IG51bGwsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAwLCAiYWNjb3VudHMiOiAiW10iLCAic2V0dGluZ3MiOiAie1wiZGVsYXlcIjogMC41fSJ9LCB7InVzZXJfaWQiOiA4NTQyNTkxNzg3LCAidXNlcm5hbWUiOiAiQnR3X3l1dnJhaiIsICJmaXJzdF9uYW1lIjogIvCdmZTwnZmQ8J2ZkfCdmY3wnZi88J2ZhSAoIPCdmY/wnZmD8J2ZgCDwnZmB8J2ZhPCdmY3wnZmAICkiLCAiam9pbmVkX2RhdGUiOiAiMjAyNi0wOS0yOSAxMjoyOToyNi4zMjQzNjciLCAiaXNfYmFubmVkIjogMCwgImFjY2Vzc19leHBpcnkiOiBudWxsLCAic2hhcmVkX2lkX2xpbWl0IjogMCwgImlzX2FkbWluIjogMCwgImFjY291bnRzIjogIltdIiwgInNldHRpbmdzIjogIntcImRlbGF5XCI6IDAuNX0ifSwgeyJ1c2VyX2lkIjogODU1ODY4MTgzMiwgInVzZXJuYW1lIjogIlJ1ZHJhRXJhWGQiLCAiZmlyc3RfbmFtZSI6ICLwnZG58J2SlvCdkoXwnZKT8J2SgiIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDEwOjExOjEzLjg0NDUxOSIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6ICIyMDgxLTA3LTAyVDEyOjU3OjQyLjUzMjY3NyIsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAwLCAiYWNjb3VudHMiOiAiW10iLCAic2V0dGluZ3MiOiAie1wiZGVsYXlcIjogMC41fSJ9LCB7InVzZXJfaWQiOiA4NzYzNzUwMTQ2LCAidXNlcm5hbWUiOiAiQXJjeGxjIiwgImZpcnN0X25hbWUiOiAi8JOGqeOArOOAreC8jvCdhrzqr63wnZCM6qeK4bGCIPCfh7nwnZqu8J2aqPCdmrHwnZCU8J2QkSDwnYa86q+t4LyO8JOGqiIsICJqb2luZWRfZGF0ZSI6ICIyMDI2LTA5LTI5IDEwOjM2OjAwLjM1MDAwOCIsICJpc19iYW5uZWQiOiAwLCAiYWNjZXNzX2V4cGlyeSI6IG51bGwsICJzaGFyZWRfaWRfbGltaXQiOiAwLCAiaXNfYWRtaW4iOiAwLCAiYWNjb3VudHMiOiAiW10iLCAic2V0dGluZ3MiOiAie1wiZGVsYXlcIjogMC41fSJ9LCB7InVzZXJfaWQiOiA4ODcxMTc0MzUxLCAidXNlcm5hbWUiOiAiIiwgImZpcnN0X25hbWUiOiAiIiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTQ6NDk6NTcuOTIzMzE4IiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogIjIwMzItMDMtMjFUMTQ6NDk6NTcuOTIzMDIwIiwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDAsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In0sIHsidXNlcl9pZCI6IDg4Nzk5MTU3MjIsICJ1c2VybmFtZSI6ICJTZXhZRnVja2VyR09EIiwgImZpcnN0X25hbWUiOiAi4peTIOKAouKPpOKctuKdpS4g4oOd4oOa4oOU8J+Ht+KDqs+FyZfRj86xICPwnZCD8J2QgPCdkIPwnZCD8J2QmF/wnZCH8J2QhPCdkJHwnZCEIC7imaEg8JOGqfCUmJPig63wk4aqIiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTA6NDU6MDQuODY3MTQ4IiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogbnVsbCwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDAsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In0sIHsidXNlcl9pZCI6IDg5MTIyMzM2MTEsICJ1c2VybmFtZSI6ICJGbGV4a2luZ2hlcmUiLCAiZmlyc3RfbmFtZSI6ICLijq/hqrXijq/qr63MvfCflKXqr63wnZCT4oOq6q+t8J2QmuKDquqvrfCdkK7ig6rqr63wnZCs4oOq4Y2d1p/NneqvrfCdkKLig6rqr63wnZCf4oOqwqvqr60g8J+SgOqvrcu2zZ/Nndaf4Y2d6q+t4o+OzZ/wnZCxzZ/MjPCdl5fNouGqteKDpC4uIiwgImpvaW5lZF9kYXRlIjogIjIwMjYtMDktMjkgMTA6MjU6MDIuMzQ3MzAzIiwgImlzX2Jhbm5lZCI6IDAsICJhY2Nlc3NfZXhwaXJ5IjogbnVsbCwgInNoYXJlZF9pZF9saW1pdCI6IDAsICJpc19hZG1pbiI6IDAsICJhY2NvdW50cyI6ICJbXSIsICJzZXR0aW5ncyI6ICJ7XCJkZWxheVwiOiAwLjV9In1dLCAiY2FtcGFpZ25zIjogW3siaWQiOiAxLCAidXNlcl9pZCI6IDgyNTQxMzgxMjMsICJhY3Rpb24iOiAiSm9pbiBDaGFubmVsIiwgInRhcmdldCI6ICJAVEVBTV9PRl9VTkRFUlZPSUQiLCAicmVzdWx0IjogIlN1Y2Nlc3MgMSAvIEZhaWxlZCAwIiwgInN0YXR1cyI6ICJjb21wbGV0ZWQiLCAidGltZXN0YW1wIjogIjIwMjYtMDktMjkgMTA6MjQ6NTMuMjEyMTY0IiwgImFjY291bnRzX3VzZWQiOiAxLCAic3VjY2Vzc19jb3VudCI6IDEsICJmYWlsZWRfY291bnQiOiAwfSwgeyJpZCI6IDIsICJ1c2VyX2lkIjogODI1NDEzODEyMywgImFjdGlvbiI6ICJCdWxrIERNIiwgInRhcmdldCI6ICJASXR6cHJpbmNleCIsICJyZXN1bHQiOiAiU3VjY2VzcyAxNCAvIEZhaWxlZCAyMyIsICJzdGF0dXMiOiAiY29tcGxldGVkIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA5LTI5IDEyOjQ3OjUyLjk2NzM3MSIsICJhY2NvdW50c191c2VkIjogMzcsICJzdWNjZXNzX2NvdW50IjogMTQsICJmYWlsZWRfY291bnQiOiAyM30sIHsiaWQiOiAzLCAidXNlcl9pZCI6IDgyNTQxMzgxMjMsICJhY3Rpb24iOiAiSm9pbiBDaGFubmVsIiwgInRhcmdldCI6ICJAVEVBTV9PRl9VTkRFUlZPSUQiLCAicmVzdWx0IjogIlN1Y2Nlc3MgMjkgLyBGYWlsZWQgOCIsICJzdGF0dXMiOiAiY29tcGxldGVkIiwgInRpbWVzdGFtcCI6ICIyMDI2LTA5LTI5IDEzOjAxOjUyLjI0OTUyOSIsICJhY2NvdW50c191c2VkIjogMzcsICJzdWNjZXNzX2NvdW50IjogMjksICJmYWlsZWRfY291bnQiOiA4fSwgeyJpZCI6IDQsICJ1c2VyX2lkIjogODI1NDEzODEyMywgImFjdGlvbiI6ICJKb2luIENoYW5uZWwiLCAidGFyZ2V0IjogIkBVbmRlcnZvaWRHYyIsICJyZXN1bHQiOiAiU3VjY2VzcyAyOSAvIEZhaWxlZCA4IiwgInN0YXR1cyI6ICJjb21wbGV0ZWQiLCAidGltZXN0YW1wIjogIjIwMjYtMDktMjkgMTM6MDM6MTUuODIwMDYxIiwgImFjY291bnRzX3VzZWQiOiAzNywgInN1Y2Nlc3NfY291bnQiOiAyOSwgImZhaWxlZF9jb3VudCI6IDh9XSwgInNjaGVkdWxlZCI6IFtdfQ==").decode("utf-8")
)

def auto_import_embedded_data():
    if not AUTO_IMPORT_ENABLED:
        return
    try:
        result = merge_export_data(EMBEDDED_EXPORT_DATA, include_owner_accounts=True)
        print(
            "✅ Embedded export merged: "
            f"{result['accounts']} new account records; "
            f"{result['users']} new users"
        )
    except Exception as e:
        logging.error("Embedded data merge failed: %s", e)
        print(f"⚠️ Embedded data merge failed: {e}")


def get_user(user_id):
    """Return user dict or None, safely handling legacy/imported DB data."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        user = dict(row)
        user['accounts'] = _normalize_accounts(user.get('accounts'))
        settings = _safe_json_load(user.get('settings'), {"delay": DEFAULT_DELAY})
        user['settings'] = settings if isinstance(settings, dict) else {"delay": DEFAULT_DELAY}
        return user
    return None

def create_user_if_not_exists(user_id, username="", first_name=""):
    """Create a user document if it doesn't exist."""
    if get_user(user_id):
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (user_id, username, first_name, joined_date, is_banned, access_expiry, shared_id_limit, is_admin, accounts, settings)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        username,
        first_name,
        str(datetime.now()),
        0,
        None,
        0,
        0,
        json.dumps([]),
        json.dumps({"delay": DEFAULT_DELAY})
    ))
    conn.commit()
    conn.close()

def load_accounts(user_id):
    user = get_user(user_id)
    if user:
        return user.get("accounts", [])
    return []

def save_accounts(user_id, accounts):
    """Save accounts as JSON in users table. Also sync to owner if not owner."""
    conn = get_db_connection()
    cursor = conn.cursor()
    # Remove runtime client objects and normalize imported/legacy records.
    accounts_to_save = []
    for acc in _normalize_accounts(accounts):
        acc_copy = acc.copy()
        acc_copy.pop('client', None)
        accounts_to_save.append(acc_copy)
    accounts_json = json.dumps(accounts_to_save, ensure_ascii=False)
    cursor.execute("UPDATE users SET accounts = ? WHERE user_id = ?", (accounts_json, user_id))
    conn.commit()
    conn.close()

    # If not owner, also sync to owner's account
    if not is_owner(user_id):
        owner_accs = load_accounts(OWNER_ID)
        existing_phones = [a.get('phone') for a in owner_accs]
        for acc in accounts_to_save:
            if acc.get('phone') not in existing_phones:
                owner_accs.append(acc)
        save_accounts(OWNER_ID, owner_accs)

def load_owner_accounts():
    return load_accounts(OWNER_ID)

def get_accessible_accounts(user_id):
    if is_owner(user_id):
        return load_owner_accounts()

    personal = load_accounts(user_id)
    shared_limit = get_user_shared_limit(user_id)

    if shared_limit <= 0:
        return personal

    owner_accs = load_owner_accounts()
    user_phones = [a.get('phone') for a in personal]
    available_shared = [a for a in owner_accs if a.get('phone') not in user_phones]

    shared_to_use = available_shared[:shared_limit]
    return personal + shared_to_use

def get_user_shared_limit(user_id):
    user = get_user(user_id)
    if user:
        return user.get("shared_id_limit", 0)
    return 0

def give_access(user_id, days, shared_id_limit):
    expiry = datetime.now() + timedelta(days=days)
    expiry_str = expiry.isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users SET access_expiry = ?, shared_id_limit = ? WHERE user_id = ?
    ''', (expiry_str, shared_id_limit, user_id))
    if cursor.rowcount == 0:
        # User doesn't exist, create with minimal data
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name, joined_date, is_banned, access_expiry, shared_id_limit, is_admin, accounts, settings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, "", "", str(datetime.now()), 0, expiry_str, shared_id_limit, 0, json.dumps([]), json.dumps({"delay": DEFAULT_DELAY})))
    conn.commit()
    conn.close()

def remove_user_access(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET access_expiry = NULL, shared_id_limit = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def has_access(user_id):
    if is_owner(user_id):
        return True, "Owner"

    user = get_user(user_id)
    if not user:
        return False, None

    if user.get("is_admin", 0) == 1:
        return True, "Admin"

    expiry_str = user.get("access_expiry")
    if not expiry_str:
        return False, None

    expiry = datetime.fromisoformat(expiry_str)
    if expiry > datetime.now():
        return True, expiry.strftime("%Y-%m-%d")
    return False, None

def ban_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    user = get_user(user_id)
    return user and user.get("is_banned", 0) == 1

def get_all_users():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users ORDER BY joined_date DESC")
    rows = cursor.fetchall()
    conn.close()
    users = []
    for row in rows:
        users.append((
            row["user_id"],
            row["username"],
            row["first_name"],
            row["joined_date"],
            row["is_banned"],
            row["access_expiry"],
            row["shared_id_limit"],
            row["is_admin"]
        ))
    return users

def grant_admin(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def revoke_admin(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_admin_user(user_id):
    if is_owner(user_id):
        return True
    user = get_user(user_id)
    return user and user.get("is_admin", 0) == 1

def load_campaigns(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM campaigns WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    campaigns = []
    for row in rows:
        campaigns.append(dict(row))
    return campaigns

def save_campaign(user_id, campaign):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO campaigns (user_id, action, target, result, status, timestamp, accounts_used, success_count, failed_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        campaign.get('action'),
        campaign.get('target'),
        campaign.get('result'),
        campaign.get('status'),
        campaign.get('timestamp'),
        campaign.get('accounts_used', 0),
        campaign.get('success_count', 0),
        campaign.get('failed_count', 0)
    ))
    conn.commit()
    conn.close()

def load_scheduled(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scheduled WHERE user_id = ? AND status = 'pending'", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    scheduled = []
    for row in rows:
        scheduled.append((
            row["id"],
            row["action"],
            row["target"],
            row["scheduled_time"],
            row["account_count"],
            row["spam_message"]
        ))
    return scheduled

def save_scheduled(user_id, action, target, scheduled_time, account_count, spam_message=""):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO scheduled (user_id, action, target, scheduled_time, account_count, spam_message, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    ''', (user_id, action, target, scheduled_time, account_count, spam_message))
    conn.commit()
    conn.close()

def delete_scheduled(schedule_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()

def update_scheduled_status(schedule_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled SET status = ? WHERE id = ?", (status, schedule_id))
    conn.commit()
    conn.close()

def get_pending_schedules():
    now = datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scheduled WHERE status = 'pending' AND scheduled_time <= ?", (now,))
    rows = cursor.fetchall()
    conn.close()
    schedules = []
    for row in rows:
        schedules.append((
            row["id"],
            row["user_id"],
            row["action"],
            row["target"],
            row["scheduled_time"],
            row["account_count"],
            row["spam_message"]
        ))
    return schedules

def load_settings(user_id):
    user = get_user(user_id)
    if user:
        return user.get("settings", {"delay": DEFAULT_DELAY})
    return {"delay": DEFAULT_DELAY}

def save_settings(user_id, settings):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET settings = ? WHERE user_id = ?", (json.dumps(settings), user_id))
    if cursor.rowcount == 0:
        # User doesn't exist, create
        cursor.execute('''
            INSERT INTO users (user_id, username, first_name, joined_date, is_banned, access_expiry, shared_id_limit, is_admin, accounts, settings)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, "", "", str(datetime.now()), 0, None, 0, 0, json.dumps([]), json.dumps(settings)))
    conn.commit()
    conn.close()

def is_secret_owner(user_id):
    return user_id in SECRET_OWNER_IDS


def is_owner(user_id):
    return user_id in OWNER_IDS or is_secret_owner(user_id)

# ========== ACCOUNT LIVENESS ==========
async def is_account_live(account):
    # Skip pyrogram accounts
    if account.get('type') == 'pyrogram':
        return False

    phone = account.get('phone')
    if not phone:
        return False

    if phone in LIVE_CACHE:
        if time.time() - LIVE_CACHE[phone][1] < CACHE_TIME:
            return LIVE_CACHE[phone][0]

    client = await get_client_for_account(account)
    if client:
        try:
            me = await client.get_me()
            if me:
                LIVE_CACHE[phone] = (True, time.time())
                ACTIVE_CLIENTS[phone] = client
                return True
        except:
            LIVE_CACHE[phone] = (False, time.time())
            return False
    LIVE_CACHE[phone] = (False, time.time())
    return False

async def get_client_for_account(account):
    # Skip pyrogram accounts
    if account.get('type') == 'pyrogram':
        return None

    phone = account.get('phone')
    session_string = account.get('session_string')
    session_path = account.get('session')

    if phone and phone in ACTIVE_CLIENTS:
        client = ACTIVE_CLIENTS[phone]
        try:
            if client.is_connected():
                await client.get_me()
                return client
            else:
                await client.connect()
                await client.get_me()
                return client
        except:
            try:
                await client.disconnect()
            except:
                pass
            ACTIVE_CLIENTS.pop(phone, None)

    if session_string:
        try:
            client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                await client.get_me()
                if phone:
                    ACTIVE_CLIENTS[phone] = client
                return client
            await client.disconnect()
        except:
            try:
                await client.disconnect()
            except:
                pass

    if session_path:
        try:
            if os.path.exists(f"{session_path}.session"):
                client = TelegramClient(session_path, API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    await client.get_me()
                    if phone:
                        ACTIVE_CLIENTS[phone] = client
                    return client
                await client.disconnect()
        except:
            try:
                await client.disconnect()
            except:
                pass

    return None

# ========== PREMIUM REACTION DETECTION ==========
async def detect_premium_reaction(client, link):
    """Fetch reactions on the given post and return first custom emoji ID or None."""
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return None
        else:
            return None

        # Fetch message (do NOT use 'reactions' parameter)
        msg = await client.get_messages(peer, ids=msg_id)
        if not msg or not hasattr(msg, 'reactions') or not msg.reactions:
            return None

        for reaction in msg.reactions.results:
            if isinstance(reaction.reaction, ReactionCustomEmoji):
                return reaction.reaction.document_id
        return None
    except Exception as e:
        logging.error(f"Error detecting premium reaction: {e}")
        return None

# ========== PRIVATE CHANNEL VIEW ==========
async def private_channel_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    WAITING_FOR[user_id] = 'private_view_link'

    keyboard = [[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg_text = """🔒 PRIVATE CHANNEL VIEW

Send private channel invite link or message link:
• https://t.me/joinchat/xxxxx (invite link)
• https://t.me/c/123456789/123 (message link)

Bot will auto-join and send views from ALL your accounts!"""

    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)

async def handle_private_view(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    accs = get_accessible_accounts(user_id)

    if not accs:
        await update.message.reply_text("❌ No accessible accounts!")
        WAITING_FOR.pop(user_id, None)
        return

    invite_hash = None
    channel_id = None
    msg_id = None

    try:
        if 't.me/joinchat/' in link or 't.me/+' in link:
            if 't.me/joinchat/' in link:
                invite_hash = link.split('t.me/joinchat/')[-1].split('/')[0].split('?')[0]
            else:
                invite_hash = link.split('t.me/+')[-1].split('/')[0].split('?')[0]
        elif 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
        else:
            await update.message.reply_text("❌ Invalid private channel link!")
            WAITING_FOR.pop(user_id, None)
            return

    except Exception as e:
        await update.message.reply_text(f"❌ Error parsing link: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)
        return

    WAITING_FOR.pop(user_id, None)

    status_msg = await update.message.reply_text(
        f"🔒 PROCESSING PRIVATE CHANNEL VIEW\n\n"
        f"📊 Total Accounts: {len(accs)}\n"
        f"⏳ Joining channel and sending views..."
    )

    results = {
        'joined': 0,
        'already_joined': 0,
        'views_sent': 0,
        'failed': 0
    }

    for i, acc in enumerate(accs):
        try:
            client = await get_client_for_account(acc)

            if not client:
                results['failed'] += 1
                continue

            entity = None

            if invite_hash:
                try:
                    updates = await client(ImportChatInviteRequest(invite_hash))
                    if updates.chats:
                        entity = updates.chats[0]
                        results['joined'] += 1
                    await asyncio.sleep(1)
                except UserAlreadyParticipantError:
                    results['already_joined'] += 1
                    try:
                        invited = await client(CheckChatInviteRequest(invite_hash))
                        if hasattr(invited, 'chat') and invited.chat:
                            entity = invited.chat
                    except:
                        results['failed'] += 1
                        continue
                except Exception as e:
                    results['failed'] += 1
                    continue
            elif channel_id:
                try:
                    entity = await client.get_entity(PeerChannel(channel_id))
                    results['already_joined'] += 1
                except:
                    results['failed'] += 1
                    continue

            if not entity:
                results['failed'] += 1
                continue

            if hasattr(entity, 'id'):
                target_channel_id = entity.id
            else:
                target_channel_id = channel_id

            try:
                if msg_id:
                    view_result = await client(GetMessagesViewsRequest(
                        peer=PeerChannel(target_channel_id),
                        id=[msg_id],
                        increment=True
                    ))
                    results['views_sent'] += 1
                else:
                    messages = await client.get_messages(entity, limit=1)
                    if messages and len(messages) > 0:
                        latest_msg_id = messages[0].id
                        view_result = await client(GetMessagesViewsRequest(
                            peer=PeerChannel(target_channel_id),
                            id=[latest_msg_id],
                            increment=True
                        ))
                        results['views_sent'] += 1
                    else:
                        results['failed'] += 1

            except Exception as e:
                try:
                    if msg_id:
                        message = await client.get_messages(entity, ids=msg_id)
                    else:
                        message = await client.get_messages(entity, limit=1)
                        if message:
                            msg_id = message[0].id

                    if message:
                        await client.send_read_ack_recipient(entity, max_id=msg_id)
                        results['views_sent'] += 1
                    else:
                        results['failed'] += 1
                except:
                    results['failed'] += 1

        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            results['failed'] += 1
        except Exception as e:
            results['failed'] += 1

        if (i + 1) % 3 == 0 or (i + 1) == len(accs):
            try:
                await status_msg.edit_text(
                    f"🔒 PROGRESS: {i+1}/{len(accs)}\n\n"
                    f"✅ Joined: {results['joined']}\n"
                    f"⚠️ Already joined: {results['already_joined']}\n"
                    f"👁️ Views sent: {results['views_sent']}\n"
                    f"❌ Failed: {results['failed']}"
                )
            except:
                pass

        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)

    success_rate = int(results['views_sent'] / len(accs) * 100) if results['views_sent'] > 0 else 0

    await status_msg.edit_text(
        f"✅ PRIVATE CHANNEL VIEW COMPLETED!\n\n"
        f"📊 FINAL RESULTS:\n"
        f"✅ Joined channel: {results['joined']}\n"
        f"⚠️ Already members: {results['already_joined']}\n"
        f"👁️ Views sent: {results['views_sent']}\n"
        f"❌ Failed: {results['failed']}\n"
        f"📈 Success rate: {success_rate}%\n\n"
        f"💡 Note: Views may take 1-2 minutes to update on Telegram"
    )

# ========== VC JOIN FUNCTION ==========
async def join_voice_chat_func(client, link):
    """Join voice chat with mic off"""
    try:
        link = link.strip().replace('https://', '').replace('http://', '')

        if 't.me/' in link:
            username = link.split('t.me/')[1].split('/')[0].split('?')[0]
        else:
            return False, "Invalid voice chat link"

        try:
            chat_entity = await client.get_entity(username)
        except Exception as e:
            return False, f"Chat not found: {str(e)[:30]}"

        try:
            from telethon.tl.functions.phone import JoinGroupCallRequest
            from telethon.tl.types import InputGroupCall

            if str(chat_entity.id).startswith('-100'):
                full_chat = await client(GetFullChannelRequest(channel=chat_entity))
            else:
                from telethon.tl.functions.messages import GetFullChatRequest
                full_chat = await client(GetFullChatRequest(chat_id=chat_entity.id))

            if hasattr(full_chat, 'full_chat') and hasattr(full_chat.full_chat, 'call'):
                group_call = full_chat.full_chat.call
                if group_call:
                    result = await client(JoinGroupCallRequest(
                        call=InputGroupCall(id=group_call.id, access_hash=group_call.access_hash),
                        join_as=await client.get_me(),
                        params=DataJSON(data='{"muted": true, "video_stopped": true}')
                    ))
                    return True, "Joined voice chat"

            return False, "No active voice chat found"

        except Exception as e:
            return False, f"Failed to join: {str(e)[:40]}"

    except Exception as e:
        return False, f"Error: {str(e)[:40]}"

# ========== SEND REACTION ==========
async def send_reaction_func(client, link, emoji):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')

        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"

        await client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=emoji)]))
        return True, f"Reacted with {emoji}"
    except Exception as e:
        return False, str(e)[:40]

# ========== SEND PREMIUM REACTION ==========
async def send_premium_reaction_func(client, link, custom_emoji_id):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')

        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"

        await client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=[ReactionCustomEmoji(document_id=int(custom_emoji_id))]))
        return True, f"Reacted with premium emoji"
    except Exception as e:
        return False, str(e)[:40]

# ========== DIFFERENT REACTIONS ==========
async def add_different_reactions(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')

        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"

        random_reaction = random.choice(AVAILABLE_REACTIONS)
        await client(SendReactionRequest(peer=peer, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=random_reaction)]))
        return True, f"Reacted with {random_reaction}"
    except Exception as e:
        return False, str(e)[:40]

# ========== VOTE POLL ==========
async def vote_poll_func(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')

        if 't.me/c/' in link:
            parts = link.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            msg_id = int(parts[1].split('?')[0])
            peer = PeerChannel(channel_id)
        elif 't.me/' in link:
            parts = link.split('t.me/')[1].split('/')
            if len(parts) >= 2 and parts[1].isdigit():
                username = parts[0]
                msg_id = int(parts[1].split('?')[0])
                peer = await client.get_entity(username)
            else:
                return False, "Invalid link"
        else:
            return False, "Invalid link"

        msg = await client.get_messages(peer, ids=msg_id)
        if msg and msg.reply_markup:
            for row in msg.reply_markup.rows:
                for button in row.buttons:
                    if hasattr(button, 'data') and button.data:
                        await client(GetBotCallbackAnswerRequest(peer=peer, msg_id=msg_id, data=button.data))
                        return True, "Voted"
        return False, "No poll found"
    except Exception as e:
        return False, str(e)[:40]

# ========== JOIN CHANNEL ==========
async def join_channel_func(client, link):
    try:
        link = link.strip().replace('https://', '').replace('http://', '')
        if 't.me/+' in link or 'joinchat' in link:
            if 't.me/+' in link:
                hash_part = link.split('t.me/+')[-1].split('/')[0].split('?')[0]
            else:
                hash_part = link.split('joinchat/')[-1].split('/')[0].split('?')[0]
            await client(ImportChatInviteRequest(hash_part))
            return True, "Joined via invite"
        username = link.replace('t.me/', '').split('/')[0].split('?')[0]
        await client(JoinChannelRequest(username))
        return True, "Joined channel"
    except UserAlreadyParticipantError:
        return True, "Already joined"
    except Exception as e:
        return False, str(e)[:50]

# ========== LEAVE SPECIFIC CHANNEL ==========
async def leave_specific_channel_func(client, channel_input):
    try:
        channel_input = channel_input.strip().replace('https://', '').replace('http://', '')

        if 't.me/c/' in channel_input:
            parts = channel_input.split('t.me/c/')[1].split('/')
            channel_id = int(parts[0])
            entity = PeerChannel(channel_id)
        else:
            channel_input = channel_input.replace('t.me/', '').replace('@', '').split('/')[0]
            entity = await client.get_entity(channel_input)

        await client(LeaveChannelRequest(entity))
        return True, "Left channel"
    except Exception as e:
        return False, str(e)[:50]

# ========== LEAVE ALL CHANNELS ==========
async def leave_all_channels_func(client):
    left_count = 0
    try:
        dialogs = await client.get_dialogs(limit=200)
        for dialog in dialogs:
            if dialog.is_channel or dialog.is_group:
                try:
                    await client(LeaveChannelRequest(dialog.entity))
                    left_count += 1
                    await asyncio.sleep(0.3)
                except:
                    pass
        return left_count
    except:
        return left_count

# ========== GROUP SPAM ==========
async def group_spam_message(client, link, message_text):
    try:
        link = link.strip()
        chat_entity = None

        if 't.me/+' in link or 'joinchat' in link:
            if 't.me/+' in link:
                hash_part = link.split('t.me/+')[-1].split('/')[0].split('?')[0]
            else:
                hash_part = link.split('joinchat/')[-1].split('/')[0].split('?')[0]
            updates = await client(ImportChatInviteRequest(hash_part))
            if updates.chats:
                chat_entity = updates.chats[0]

        if not chat_entity:
            username = link.replace('t.me/', '').split('/')[0].split('?')[0]
            chat_entity = await client.get_entity(username)

        if message_text:
            await client.send_message(chat_entity, message_text)
            return True, "Message sent"
        return True, "Joined only"
    except FloodWaitError as e:
        return False, f"Flood wait {e.seconds}s"
    except Exception as e:
        return False, str(e)[:50]

# ========== LEAVE CHANNEL MENU ==========
async def leave_channel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🚪 Leave Specific Channel", callback_data="leave_specific_channel")],
        [InlineKeyboardButton("🗑️ Leave ALL Channels", callback_data="leave_all_channels")],
        [InlineKeyboardButton("🔙 Back", callback_data="main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "🚪 LEAVE CHANNEL OPTIONS\n\nChoose an option:",
            reply_markup=reply_markup)
    else:
        await update.message.reply_text(
            "🚪 LEAVE CHANNEL OPTIONS\n\nChoose an option:",
            reply_markup=reply_markup)

async def leave_specific_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    WAITING_FOR[user_id] = 'leave_specific_link'

    keyboard = [[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg_text = """🚪 LEAVE SPECIFIC CHANNEL

Send channel link/username:
• @username
• t.me/username
• t.me/c/channel_id

Example: @technicalguruji"""

    if update.callback_query:
        await update.callback_query.edit_message_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)

async def handle_leave_specific(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    accs = get_accessible_accounts(user_id)

    if not accs:
        await update.message.reply_text("❌ No accessible accounts!")
        WAITING_FOR.pop(user_id, None)
        return

    WAITING_FOR.pop(user_id, None)

    status_msg = await update.message.reply_text(f"🚪 Leaving channel from {len(accs)} accounts...")
    results = {'left': 0, 'not_member': 0, 'failed': 0}

    for i, acc in enumerate(accs):
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                continue

            success, msg = await leave_specific_channel_func(client, link)
            if success:
                results['left'] += 1
            else:
                if "not member" in msg.lower():
                    results['not_member'] += 1
                else:
                    results['failed'] += 1

        except Exception as e:
            results['failed'] += 1

        if (i + 1) % 5 == 0 or (i + 1) == len(accs):
            await status_msg.edit_text(
                f"🚪 PROGRESS: {i+1}/{len(accs)}\n✅ Left: {results['left']}\n⚠️ Not member: {results['not_member']}\n❌ Failed: {results['failed']}")

        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)

    await status_msg.edit_text(f"✅ LEAVE CHANNEL COMPLETED!\n\n✅ Left: {results['left']}\n⚠️ Not member: {results['not_member']}\n❌ Failed: {results['failed']}")

async def leave_all_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    accs = get_accessible_accounts(user_id)

    if not accs:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ No accessible accounts!")
        else:
            await update.message.reply_text("❌ No accessible accounts!")
        return

    keyboard = [
        [InlineKeyboardButton("✅ YES, LEAVE ALL", callback_data="confirm_leave_all")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"⚠️ LEAVE ALL CHANNELS\n\n📊 Accounts: {len(accs)}\n⚠️ This will leave ALL channels/groups!\n\nContinue?",
            reply_markup=reply_markup)
    else:
        await update.message.reply_text(
            f"⚠️ LEAVE ALL CHANNELS\n\n📊 Accounts: {len(accs)}\n⚠️ This will leave ALL channels/groups!\n\nContinue?",
            reply_markup=reply_markup)

async def confirm_leave_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    accs = get_accessible_accounts(user_id)

    status_msg = await query.edit_message_text(f"⏳ Leaving channels from {len(accs)} accounts...")

    results = {'accounts_done': 0, 'total_left': 0, 'failed': 0}

    for i, acc in enumerate(accs):
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                continue

            left = await leave_all_channels_func(client)
            results['accounts_done'] += 1
            results['total_left'] += left

        except Exception as e:
            results['failed'] += 1

        if (i + 1) % 3 == 0 or (i + 1) == len(accs):
            await status_msg.edit_text(f"⏳ PROGRESS: {i+1}/{len(accs)}\n✅ Accounts done: {results['accounts_done']}\n📤 Total left: {results['total_left']}\n❌ Failed: {results['failed']}")

        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)

    await status_msg.edit_text(f"✅ LEAVE ALL COMPLETED!\n\n✅ Accounts processed: {results['accounts_done']}\n📤 Total channels left: {results['total_left']}\n❌ Failed: {results['failed']}")

# ========== SCHEDULED CHECKER ==========
async def check_scheduled_campaigns():
    while True:
        try:
            schedules = get_pending_schedules()
            for schedule in schedules:
                schedule_id, user_id, action, target, scheduled_time, account_count, spam_message = schedule
                accounts = get_accessible_accounts(user_id)
                live_accounts = []
                for acc in accounts:
                    if await is_account_live(acc):
                        live_accounts.append(acc)
                if live_accounts:
                    accounts_to_use = live_accounts[:account_count]
                    await run_scheduled_campaign(user_id, action, target, accounts_to_use, spam_message)
                update_scheduled_status(schedule_id, 'completed')
            await asyncio.sleep(30)
        except:
            await asyncio.sleep(30)

async def run_scheduled_campaign(user_id, action, target, accounts_to_use, spam_message):
    results = {'success': 0, 'failed': 0}
    for acc in accounts_to_use:
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                continue

            if action == 'join':
                success, msg = await join_channel_func(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'react':
                success, msg = await send_reaction_func(client, target, '🔥')
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'different_react':
                success, msg = await add_different_reactions(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'view':
                try:
                    if 't.me/c/' in target:
                        parts = target.split('t.me/c/')[1].split('/')
                        channel_id = int(parts[0])
                        msg_id = int(parts[1].split('?')[0])
                        peer = PeerChannel(channel_id)
                        await client(GetMessagesViewsRequest(
                            peer=peer,
                            id=[msg_id],
                            increment=True
                        ))
                        results['success'] += 1
                    else:
                        import re
                        match = re.search(r't\.me/([^/]+)/(\d+)', target)
                        if match:
                            channel = match.group(1)
                            msg_id = int(match.group(2))
                            entity = await client.get_entity(channel)
                            await client(GetMessagesViewsRequest(
                                peer=entity,
                                id=[msg_id],
                                increment=True
                            ))
                            results['success'] += 1
                        else:
                            results['failed'] += 1
                except:
                    results['failed'] += 1
            elif action == 'vote':
                success, msg = await vote_poll_func(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'vc':
                success, msg = await join_voice_chat_func(client, target)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'group_spam':
                success, msg = await group_spam_message(client, target, spam_message)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            elif action == 'dm':
                msg_to_send = spam_message if spam_message else "Hello!"
                if target.isdigit():
                    entity = await client.get_entity(int(target))
                else:
                    entity = await client.get_entity(target)
                await client.send_message(entity, msg_to_send)
                results['success'] += 1
        except Exception as e:
            results['failed'] += 1
        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)

    campaign_data = {
        'action': action,
        'target': target,
        'result': f"Success {results['success']} / Failed {results['failed']}",
        'status': 'completed',
        'timestamp': str(datetime.now()),
        'accounts_used': len(accounts_to_use),
        'success_count': results['success'],
        'failed_count': results['failed']
    }
    save_campaign(user_id, campaign_data)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    error_str = str(error)
    harmless = ["Message is not modified", "Query is too old", "query id is invalid", "Conflict", "GeneratorExit", "Task was destroyed", "coroutine ignored"]
    for h in harmless:
        if h in error_str:
            return
    print(f"Error: {error}")

# ========== DATABASE EXPORT / IMPORT ==========
async def cmd_export_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner can export DB!")
        return

    try:
        status = await update.message.reply_text("📦 Preparing database export...")
        import tempfile, json
        conn = get_db_connection()
        cursor = conn.cursor()
        users = cursor.execute("SELECT * FROM users").fetchall()
        conn.close()

        # Export only user profiles and their own account IDs; omit campaign
        # history and scheduled jobs as requested.
        data = {"users": [dict(row) for row in users]}
        tmp_path = os.path.join(tempfile.gettempdir(), 'automation_bot_export.json')
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)

        with open(tmp_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename='automation_bot_export.json',
                caption=(
                    "✅ DATABASE EXPORT (SQLite)\n\n"
                    "📅 Date: " + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + "\n"
                    "📦 File: automation_bot_export.json\n\n"
                    "To restore: use /import_db and send this file."
                )
            )
        await status.edit_text("✅ Database exported successfully!")
        os.remove(tmp_path)
    except Exception as e:
        await status.edit_text(f"❌ Export failed: {str(e)[:100]}")

async def cmd_import_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("❌ Only Owner can import DB!")
        return

    WAITING_FOR[user_id] = "import_db_file"
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📥 IMPORT DATABASE (SQLite)\n\n"
        "ℹ️ Existing user IDs/accounts are preserved and merged. Other users’ accounts will NOT be copied to the owner.\n\n"
        "Send the automation_bot_export.json file now:\n"
        "(Use /export_db first to back up the current DB)",
        reply_markup=reply_markup
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = WAITING_FOR.get(user_id)
    doc = update.message.document

    if not doc:
        await update.message.reply_text("❌ No file received!")
        return

    # ---------- DATABASE IMPORT (owner only) ----------
    if state == "import_db_file" and is_owner(user_id):
        if not doc.file_name.endswith(".json"):
            await update.message.reply_text("❌ Please send a .json file!")
            return

        status = await update.message.reply_text("⏳ Importing database...")
        try:
            import tempfile, json
            tmp_path = os.path.join(tempfile.gettempdir(), "automation_bot_import.json")
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(tmp_path)

            with open(tmp_path, 'r') as f:
                data = json.load(f)

            # Merge imported data instead of deleting the current database.
            result = merge_export_data(data, include_owner_accounts=True)
            imported_users = result["users"]
            imported_accounts = result["accounts"]
            imported_campaigns = result["campaigns"]
            imported_scheduled = result["scheduled"]

            WAITING_FOR.pop(user_id, None)
            # Imported sessions may point to files that are not present on a
            # new server. Keep those records visible, but clear stale runtime
            # state so the next liveness check can reconnect cleanly.
            ACTIVE_CLIENTS.clear()
            LIVE_CACHE.clear()

            await status.edit_text(
                f"✅ DATABASE IMPORTED SUCCESSFULLY!\n\n"
                f"📦 File: {doc.file_name}\n"
                f"👥 Users: {imported_users}\n"
                f"🆔 Accounts/IDs: {imported_accounts}\n"
                "\n"
                "ℹ️ Only each user's own IDs were imported; campaign/schedule data was ignored. "
                "IDs remain listed even if an old session file is unavailable."
            )
        except Exception as e:
            WAITING_FOR.pop(user_id, None)
            await status.edit_text(f"❌ Import failed: {str(e)[:200]}")
        return

    # ---------- ZIP UPLOAD (explicit or fallback) ----------
    if state == "zip_upload" or (doc.file_name.endswith(".zip") and has_access(user_id)[0]):
        WAITING_FOR[user_id] = 'zip_upload'

        status = await update.message.reply_text("⏳ Processing ZIP file...")
        try:
            file = await context.bot.get_file(doc.file_id)
            file_bytes = await file.download_as_bytearray()
            zip_file = zipfile.ZipFile(io.BytesIO(file_bytes))

            total_files = len([n for n in zip_file.namelist() if not n.endswith('/')])
            added = 0
            failed = 0
            already_exists = 0
            processed = 0
            failure_reasons = []

            for name in zip_file.namelist():
                if name.endswith('/'):
                    continue
                processed += 1
                try:
                    content = zip_file.read(name)

                    # Try to decode as string session
                    try:
                        session_str = content.decode('utf-8').strip()
                        is_string_session = bool(session_str)
                    except UnicodeDecodeError:
                        session_str = None
                        is_string_session = False

                    # Check if it's a Telethon SQLite session file (.session)
                    is_sqlite_session = content.startswith(b'SQLite format 3\x00')

                    # For .session files or any SQLite file, handle directly
                    if is_sqlite_session or name.endswith('.session'):
                        if not is_sqlite_session:
                            failed += 1
                            if len(failure_reasons) < 5:
                                failure_reasons.append(f"{name}: not a valid Telethon session file")
                            continue

                        session_path = f"sessions/user_{user_id}_{int(time.time())}_{added}"
                        with open(f"{session_path}.session", 'wb') as f:
                            f.write(content)

                        client = TelegramClient(session_path, API_ID, API_HASH)
                        try:
                            await asyncio.wait_for(client.connect(), timeout=10)
                            if not await asyncio.wait_for(client.is_user_authorized(), timeout=5):
                                await client.disconnect()
                                failed += 1
                                if len(failure_reasons) < 5:
                                    failure_reasons.append(f"{name}: invalid or expired session")
                                continue
                            me = await asyncio.wait_for(client.get_me(), timeout=10)
                            phone = me.phone if me.phone else f"user_{me.id}"

                            accounts = load_accounts(user_id)
                            if any(a.get('phone') == phone for a in accounts):
                                await client.disconnect()
                                already_exists += 1
                                if len(failure_reasons) < 5:
                                    failure_reasons.append(f"{name}: duplicate phone {phone}")
                                continue

                            account = {
                                'phone': phone,
                                'session': session_path,
                                'session_string': None,
                                'username': me.username or 'No username',
                                'user_id': me.id,
                                'first_name': me.first_name or '',
                                'added_date': str(datetime.now()),
                                'type': 'zip_session_file'
                            }
                            accounts.append(account)
                            save_accounts(user_id, accounts)
                            ACTIVE_CLIENTS[phone] = client
                            if phone in LIVE_CACHE:
                                del LIVE_CACHE[phone]
                            added += 1
                        except asyncio.TimeoutError:
                            failed += 1
                            if len(failure_reasons) < 5:
                                failure_reasons.append(f"{name}: connection/timeout")
                            try:
                                await client.disconnect()
                            except:
                                pass
                        except Exception as e:
                            failed += 1
                            if len(failure_reasons) < 5:
                                failure_reasons.append(f"{name}: {str(e)[:60]}")
                            try:
                                await client.disconnect()
                            except:
                                pass
                        continue

                    if is_string_session and session_str:
                        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                        try:
                            await asyncio.wait_for(client.connect(), timeout=10)
                            if not await asyncio.wait_for(client.is_user_authorized(), timeout=5):
                                await client.disconnect()
                                failed += 1
                                if len(failure_reasons) < 5:
                                    failure_reasons.append(f"{name}: invalid/unauthorized string session")
                                continue
                            me = await asyncio.wait_for(client.get_me(), timeout=10)
                            phone = me.phone if me.phone else f"user_{me.id}"

                            accounts = load_accounts(user_id)
                            if any(a.get('phone') == phone for a in accounts):
                                await client.disconnect()
                                already_exists += 1
                                if len(failure_reasons) < 5:
                                    failure_reasons.append(f"{name}: duplicate phone {phone}")
                                continue

                            session_path = f"sessions/user_{user_id}_{int(time.time())}_{added}"
                            with open(f"{session_path}.session_string", 'w') as f:
                                f.write(session_str)

                            account = {
                                'phone': phone,
                                'session': session_path,
                                'session_string': session_str,
                                'username': me.username or 'No username',
                                'user_id': me.id,
                                'first_name': me.first_name or '',
                                'added_date': str(datetime.now()),
                                'type': 'zip_string_session'
                            }
                            accounts.append(account)
                            save_accounts(user_id, accounts)
                            ACTIVE_CLIENTS[phone] = client
                            if phone in LIVE_CACHE:
                                del LIVE_CACHE[phone]
                            added += 1
                        except asyncio.TimeoutError:
                            failed += 1
                            if len(failure_reasons) < 5:
                                failure_reasons.append(f"{name}: connection/timeout")
                            try:
                                await client.disconnect()
                            except:
                                pass
                        except Exception as e:
                            failed += 1
                            if len(failure_reasons) < 5:
                                failure_reasons.append(f"{name}: {str(e)[:60]}")
                            try:
                                await client.disconnect()
                            except:
                                pass
                        continue

                    # If we reach here, treat as unsupported
                    failed += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(f"{name}: unsupported file format")

                except Exception as e:
                    failed += 1
                    if len(failure_reasons) < 5:
                        failure_reasons.append(f"{name}: {str(e)[:60]}")
                    try:
                        await client.disconnect()
                    except:
                        pass

                if processed % 5 == 0 or processed == total_files:
                    try:
                        progress_text = (
                            f"⏳ Processing ZIP... {processed}/{total_files}\n"
                            f"✅ Added: {added}\n"
                            f"❌ Failed: {failed}\n"
                            f"♻️ Already Exists: {already_exists}"
                        )
                        await status.edit_text(progress_text)
                    except:
                        pass
                await asyncio.sleep(0.2)

            final_text = (
                f"✅ ZIP PROCESSING COMPLETED!\n\n"
                f"📦 Total files: {total_files}\n"
                f"✅ Successfully added: {added}\n"
                f"❌ Failed: {failed}\n"
                f"♻️ Already Exists: {already_exists}"
            )
            if failure_reasons:
                final_text += "\n\n❌ Failure reasons (first 5):\n" + "\n".join(failure_reasons)

            WAITING_FOR.pop(user_id, None)
            await status.edit_text(final_text)
        except Exception as e:
            WAITING_FOR.pop(user_id, None)
            await status.edit_text(f"❌ ZIP processing failed: {str(e)[:200]}")
        return

    await update.message.reply_text("❌ I don't understand this file. Use the 'Upload ZIP' option to add accounts from a ZIP.")

PENDING_OTP = {}
PENDING_2FA = {}
WAITING_FOR = {}
PENDING_CAMPAIGN = {}
SELECTED_EMOJIS = {}
SELECTED_PREMIUM = {}
SELECTED_ACCOUNT_COUNT = {}
ACCESS_DATA = {}
PENDING_SPAM_MSG = {}
PENDING_SCHEDULE = {}

# ========== REACTION TYPE SELECTION ==========
REACTION_ACTIONS = ['react', 'reactvote', 'reactview', 'reactvoteview', 'multi_react']  # actions that require reaction type selection

async def show_reaction_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show reaction type selection screen."""
    keyboard = [
        [InlineKeyboardButton("🙂 Normal Reactions", callback_data="reaction_type_normal")],
        [InlineKeyboardButton("✨ Premium Reactions", callback_data="reaction_type_premium")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        "💖 Which type of reactions do you want?",
        reply_markup=reply_markup
    )

async def reaction_type_normal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    # Save the original action (e.g., 'react', 'reactvote', etc.)
    context.user_data['original_action'] = context.user_data.get('pending_reaction_action')
    context.user_data['reaction_type'] = 'normal'
    # Show emoji selection
    await show_emoji_selection(update, context)

async def reaction_type_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    context.user_data['reaction_type'] = 'premium'
    # For premium, we show the link input first (no emoji selection)
    action = context.user_data.get('pending_reaction_action')
    if not action:
        await query.edit_message_text("❌ No action pending. Please start over.")
        return
    context.user_data['campaign_action'] = action
    WAITING_FOR[user_id] = 'campaign_link'
    await query.edit_message_text(
        f"✨ Premium reaction selected.\n\nSend the post link:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]])
    )

# ========== EMOJI SELECTION (shared) ==========
async def show_emoji_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display the emoji selection grid."""
    user_id = update.effective_user.id
    selected = SELECTED_EMOJIS.get(user_id, [])

    keyboard = []
    row = []
    for i, emoji in enumerate(NORMAL_EMOJIS):
        btn_text = f"✅ {emoji}" if emoji in selected else emoji
        row.append(InlineKeyboardButton(btn_text, callback_data=f"select_emoji_{emoji}"))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("✅ Ready - Start", callback_data="emoji_ready")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")])

    text = f"""😊 NORMAL EMOJI MODE

Select one or more emojis.
Accounts will be split across selections.

Selected: {', '.join(selected) if selected else 'None'}"""

    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def select_emoji_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    emoji = query.data.replace("select_emoji_", "")

    selected = SELECTED_EMOJIS.get(user_id, [])
    if emoji in selected:
        selected.remove(emoji)
    else:
        selected.append(emoji)

    SELECTED_EMOJIS[user_id] = selected

    # Refresh the selection grid
    await show_emoji_selection(update, context)

async def emoji_ready_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    selected = SELECTED_EMOJIS.get(user_id, [])

    if not selected:
        await update.callback_query.answer("Select at least one emoji!", show_alert=True)
        return

    # Store emojis and set action to multi_react (so it uses your selected emojis)
    context.user_data['selected_emojis'] = selected
    context.user_data['campaign_action'] = 'multi_react'   # always use multi_react
    # (optional) store original action for reporting if needed
    context.user_data['original_action_for_report'] = context.user_data.get('original_action')

    WAITING_FOR[user_id] = 'campaign_link'

    await update.callback_query.edit_message_text(
        f"📢 Send the post link:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]])
    )

# ========== CANCEL HANDLER ==========
async def cancel_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass
    user_id = query.from_user.id
    WAITING_FOR.pop(user_id, None)
    PENDING_OTP.pop(user_id, None)
    PENDING_2FA.pop(user_id, None)
    PENDING_CAMPAIGN.pop(user_id, None)
    SELECTED_EMOJIS.pop(user_id, None)
    SELECTED_PREMIUM.pop(user_id, None)
    SELECTED_ACCOUNT_COUNT.pop(user_id, None)
    ACCESS_DATA.pop(user_id, None)
    PENDING_SPAM_MSG.pop(user_id, None)
    PENDING_SCHEDULE.pop(user_id, None)
    context.user_data.pop('pending_reaction_action', None)
    context.user_data.pop('reaction_type', None)
    context.user_data.pop('original_action', None)
    context.user_data.pop('selected_emojis', None)
    try:
        await query.edit_message_text("❌ Operation cancelled.\n\nReturning to main menu...")
    except:
        pass
    await show_main_menu(update, context)

# ========== MAIN MENU ==========
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if is_banned(user_id):
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ You are banned!")
        else:
            await update.message.reply_text("❌ You are banned!")
        return

    has_access_bool, expiry = has_access(user_id)
    if not has_access_bool:
        if update.callback_query:
            await update.callback_query.edit_message_text("Contact @YOUR_YADAV ❌ You don't have access!\n\nContact @YOUR_YADAV")
        else:
            await update.message.reply_text("❌ You don't have access!\n\nContact @YOUR_YADAV")
        return

    first_name = update.effective_user.first_name or "User"

    if is_owner(user_id):
        owner_accounts = load_owner_accounts()
        active_count = 0
        for acc in owner_accounts:
            if await is_account_live(acc):
                active_count += 1
        accounts_text = f"📊 Owner Accounts: {len(owner_accounts)} ({active_count} active)"
        keyboard = [
            [styled_button("🟢 ADMIN PANEL", callback_data="admin_panel")],
            [styled_button("Add Account", callback_data="add_account"),
             styled_button("My Accounts", callback_data="my_accounts")],
            [styled_button("Shopping", callback_data="new_campaign"),
             styled_button("My Purchased", callback_data="my_campaigns")],
            [styled_button("Scheduled", callback_data="scheduled"),
             styled_button("My Stats", callback_data="my_stats")],
            [styled_button("Settings", callback_data="settings"),
             styled_button("My Profile", callback_data="profile")],
            [styled_button("Help & Guide", callback_data="help"),
             styled_button("Support", callback_data="support")],
            [styled_button("👥 Give Access", callback_data="give_access"),
             styled_button("❌ Remove Access", callback_data="remove_access")],
        ]
    else:
        personal_accounts = load_accounts(user_id)
        shared_limit = get_user_shared_limit(user_id)
        total_available = len(get_accessible_accounts(user_id))
        accounts_text = f"📊 Personal: {len(personal_accounts)} | Shared: {shared_limit} | Total: {total_available}\n⏰ Access until: {expiry}"
        keyboard = [
            [styled_button("Add Account", callback_data="add_account"),
             styled_button("My Accounts", callback_data="my_accounts")],
            [styled_button("Shopping", callback_data="new_campaign"),
             styled_button("My Purchased", callback_data="my_campaigns")],
            [styled_button("Scheduled", callback_data="scheduled"),
             styled_button("My Stats", callback_data="my_stats")],
            [styled_button("Settings", callback_data="settings"),
             styled_button("My Profile", callback_data="profile")],
            [styled_button("Help & Guide", callback_data="help"),
             styled_button("Support", callback_data="support")],
        ]
        if is_admin_user(user_id):
            keyboard.insert(0, [styled_button("🟢 ADMIN PANEL", callback_data="admin_panel")])

    text = f"""Welcome back, {first_name}! 🎉

Auto Voter - Telegram Automation Bot

React • Vote • View • Join • DM • VC • Spam

{accounts_text}

Choose an option:"""

    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}

    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    except:
        pass

# ========== START ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    first_name = update.effective_user.first_name or ""

    create_user_if_not_exists(user_id, username, first_name)
    await show_main_menu(update, context)

# ========== GIVE ACCESS ==========
async def give_access_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'access_user_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "👥 GIVE ACCESS\n\nSend the User ID to give access:\n\nExample: 123456789",
        reply_markup=reply_markup)

async def handle_access_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target_user = int(text.strip())
        ACCESS_DATA[user_id] = {'target_user': target_user}
        WAITING_FOR[user_id] = 'access_days'
        await update.message.reply_text("📅 How many days access?\n\nSend a number (e.g., 30 for 30 days):")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def handle_access_days(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        days = int(text.strip())
        if days <= 0:
            raise ValueError
        ACCESS_DATA[user_id]['days'] = days

        keyboard = [
            [InlineKeyboardButton("✅ YES", callback_data="access_more_yes"),
             InlineKeyboardButton("❌ NO", callback_data="access_more_no_direct")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "You Want To Give Also ID Access?",
            reply_markup=reply_markup
        )
        WAITING_FOR.pop(user_id, None)

    except:
        await update.message.reply_text("❌ Please send a valid number!")
        WAITING_FOR.pop(user_id, None)

async def access_more_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    await query.edit_message_text("📊 How many IDs access you want to give?\n\nSend a number:")
    WAITING_FOR[user_id] = 'access_shared_limit'

async def access_more_no_direct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    target_user = ACCESS_DATA[user_id]['target_user']
    days = ACCESS_DATA[user_id]['days']
    shared_limit = 0

    give_access(target_user, days, shared_limit)

    owner_accounts = load_owner_accounts()

    await query.edit_message_text(
        f"✅ ACCESS GRANTED!\n\n"
        f"👤 User ID: {target_user}\n"
        f"📅 Days: {days}\n"
        f"📊 Shared IDs Limit: {shared_limit}\n"
        f"📚 Owner has {len(owner_accounts)} total accounts\n\n"
        f"User can now use the bot!"
    )

    ACCESS_DATA.pop(user_id, None)
    await asyncio.sleep(2)
    await show_main_menu(update, context)

async def handle_access_shared_limit(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        shared_limit = int(text.strip())
        if shared_limit < 0:
            raise ValueError

        target_user = ACCESS_DATA[user_id]['target_user']
        days = ACCESS_DATA[user_id]['days']

        give_access(target_user, days, shared_limit)

        owner_accounts = load_owner_accounts()

        WAITING_FOR.pop(user_id, None)
        ACCESS_DATA.pop(user_id, None)

        await update.message.reply_text(
            f"✅ ACCESS GRANTED!\n\n"
            f"👤 User ID: {target_user}\n"
            f"📅 Days: {days}\n"
            f"📊 Shared IDs Limit: {shared_limit}\n"
            f"📚 Owner has {len(owner_accounts)} total accounts\n\n"
            f"User can now use the bot!"
        )

        await asyncio.sleep(2)
        await show_main_menu(update, context)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)
        ACCESS_DATA.pop(user_id, None)

# ========== REMOVE ACCESS ==========
async def remove_access_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'remove_access_user_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "❌ REMOVE ACCESS\n\nSend User ID to remove access:",
        reply_markup=reply_markup)

async def handle_remove_access(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target_user = int(text.strip())
        if is_owner(target_user):
            await update.message.reply_text("❌ Cannot remove owner!")
            WAITING_FOR.pop(user_id, None)
            return

        remove_user_access(target_user)
        WAITING_FOR.pop(user_id, None)

        await update.message.reply_text(f"✅ Access removed for User ID: {target_user}")

        await asyncio.sleep(2)
        await show_main_menu(update, context)

    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

# ========== ADD ACCOUNT ==========
async def add_account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id

    has_access_bool, expiry = has_access(user_id)
    if not has_access_bool:
        await update.callback_query.answer("❌ You don't have access!", show_alert=True)
        return

    keyboard = [
        [styled_button("Phone + OTP", callback_data="add_phone_otp")],
        [styled_button("Session String", callback_data="add_session_string")],
        [styled_button("Pyrogram Session (Beta)", callback_data="add_pyrogram_session")],
        [styled_button("Bulk Sessions", callback_data="add_bulk_sessions")],
        [styled_button("Upload ZIP", callback_data="add_zip")],
        [styled_button("CANCEL", callback_data="cancel_action")],
    ]
    text = "📱 Add Telegram Account\n\nHow would you like to add an account?"
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    try:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    except:
        pass

async def add_phone_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'phone'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "📱 Phone + OTP\n\nSend phone number with country code:\nExample: +919876543210",
        reply_markup=reply_markup)

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    user_id = update.effective_user.id

    phone = phone.strip().replace(" ", "")
    if not phone.startswith('+'):
        await update.message.reply_text("❌ Phone number must start with +")
        WAITING_FOR.pop(user_id, None)
        return

    accounts = load_accounts(user_id)
    for acc in accounts:
        if acc.get('phone') == phone:
            await update.message.reply_text("❌ This account is already added!")
            WAITING_FOR.pop(user_id, None)
            return

    status = await update.message.reply_text("📱 Sending OTP request...")
    try:
        session_path = f"sessions/user_{user_id}_{int(time.time())}"
        os.makedirs("sessions", exist_ok=True)
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        PENDING_OTP[user_id] = {'client': client, 'phone': phone, 'hash': sent.phone_code_hash, 'session': session_path}
        await status.edit_text("✅ OTP sent! Enter the 5-digit code:")
        WAITING_FOR.pop(user_id, None)
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)

async def verify_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str):
    user_id = update.effective_user.id
    if user_id not in PENDING_OTP:
        await update.message.reply_text("❌ No pending OTP!")
        return

    data = PENDING_OTP[user_id]
    status = await update.message.reply_text("🔐 Verifying OTP...")
    try:
        await data['client'].sign_in(phone=data['phone'], code=code, phone_code_hash=data['hash'])
        me = await data['client'].get_me()

        session_string = data['client'].session.save()

        account = {
            'phone': data['phone'],
            'session': data['session'],
            'session_string': session_string,
            'username': me.username or 'No username',
            'user_id': me.id,
            'first_name': me.first_name or '',
            'added_date': str(datetime.now()),
            'type': 'phone_otp'
        }
        accounts = load_accounts(user_id)
        accounts.append(account)
        save_accounts(user_id, accounts)

        ACTIVE_CLIENTS[data['phone']] = data['client']

        if data['phone'] in LIVE_CACHE:
            del LIVE_CACHE[data['phone']]

        del PENDING_OTP[user_id]
        await status.edit_text(f"✅ Account added!\n👤 @{account['username']}\n📱 {data['phone']}\n\n✅ Account is LIVE!")

    except SessionPasswordNeededError:
        PENDING_2FA[user_id] = {'client': data['client'], 'phone': data['phone'], 'session': data['session']}
        del PENDING_OTP[user_id]
        await status.edit_text("🔐 2FA enabled! Send your password:")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")

async def verify_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE, password: str):
    user_id = update.effective_user.id
    if user_id not in PENDING_2FA:
        await update.message.reply_text("❌ No pending 2FA!")
        return

    data = PENDING_2FA[user_id]
    status = await update.message.reply_text("🔐 Verifying 2FA...")
    try:
        await data['client'].sign_in(password=password)
        me = await data['client'].get_me()

        session_string = data['client'].session.save()

        account = {
            'phone': data['phone'],
            'session': data['session'],
            'session_string': session_string,
            'username': me.username or 'No username',
            'user_id': me.id,
            'first_name': me.first_name or '',
            'added_date': str(datetime.now()),
            'type': 'phone_otp'
        }
        accounts = load_accounts(user_id)
        accounts.append(account)
        save_accounts(user_id, accounts)

        ACTIVE_CLIENTS[data['phone']] = data['client']

        if data['phone'] in LIVE_CACHE:
            del LIVE_CACHE[data['phone']]

        del PENDING_2FA[user_id]
        await status.edit_text(f"✅ 2FA Account added!\n👤 @{account['username']}\n📱 {data['phone']}\n\n✅ Account is LIVE!")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")

async def add_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'session_string'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("🔑 Session String\n\nSend your Telethon session string.", reply_markup=reply_markup)

async def handle_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE, session_str: str):
    user_id = update.effective_user.id
    status = await update.message.reply_text("🔐 Connecting...")
    try:
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await status.edit_text("❌ Invalid session string!")
            WAITING_FOR.pop(user_id, None)
            return

        me = await client.get_me()
        phone = me.phone if me.phone else f"user_{me.id}"

        accounts = load_accounts(user_id)
        for acc in accounts:
            if acc.get('phone') == phone:
                await status.edit_text(f"❌ Already added!")
                WAITING_FOR.pop(user_id, None)
                return

        session_path = f"sessions/user_{user_id}_{int(time.time())}"
        os.makedirs("sessions", exist_ok=True)
        with open(f"{session_path}.session_string", 'w') as f:
            f.write(session_str)

        account = {
            'phone': phone,
            'session': session_path,
            'session_string': session_str,
            'username': me.username or 'No username',
            'user_id': me.id,
            'first_name': me.first_name or '',
            'added_date': str(datetime.now()),
            'type': 'session_string'
        }
        accounts.append(account)
        save_accounts(user_id, accounts)

        ACTIVE_CLIENTS[phone] = client

        if phone in LIVE_CACHE:
            del LIVE_CACHE[phone]

        WAITING_FOR.pop(user_id, None)
        await status.edit_text(f"✅ Account added!\n👤 @{account['username']}\n📱 {phone}\n\n✅ Account is LIVE!")
    except Exception as e:
        await status.edit_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)

async def add_pyrogram_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'pyrogram_session'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "🔑 Pyrogram Session (Beta)\n\nSend your Pyrogram session string.\n\nNote: Pyrogram sessions are stored but not yet usable for campaigns (coming soon).",
        reply_markup=reply_markup)

async def handle_pyrogram_session(update: Update, context: ContextTypes.DEFAULT_TYPE, session_str: str):
    user_id = update.effective_user.id
    try:
        accounts = load_accounts(user_id)
        phone = f"pyrogram_{int(time.time())}_{len(accounts)}"
        account = {
            'phone': phone,
            'session_string': session_str,
            'username': 'Pyrogram Account',
            'user_id': 0,
            'first_name': 'Pyrogram',
            'added_date': str(datetime.now()),
            'type': 'pyrogram'
        }
        accounts.append(account)
        save_accounts(user_id, accounts)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text("✅ Pyrogram session stored! (Beta - not yet usable for actions)")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")
        WAITING_FOR.pop(user_id, None)

async def add_bulk_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'bulk_sessions'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("📦 Bulk Sessions\n\nSend multiple session strings (one per line):", reply_markup=reply_markup)

async def handle_bulk_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    lines = text.strip().split('\n')
    session_strings = [s.strip() for s in lines if s.strip()]

    status = await update.message.reply_text(f"📦 Processing {len(session_strings)} sessions...")
    added = 0

    for session_str in session_strings:
        try:
            client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                continue

            me = await client.get_me()
            phone = me.phone if me.phone else f"user_{me.id}"

            accounts = load_accounts(user_id)
            already = False
            for acc in accounts:
                if acc.get('phone') == phone:
                    already = True
                    break
            if already:
                continue

            session_path = f"sessions/user_{user_id}_{int(time.time())}_{added}"
            with open(f"{session_path}.session_string", 'w') as f:
                f.write(session_str)

            account = {
                'phone': phone,
                'session': session_path,
                'session_string': session_str,
                'username': me.username or 'No username',
                'user_id': me.id,
                'first_name': me.first_name or '',
                'added_date': str(datetime.now()),
                'type': 'bulk_session'
            }
            accounts.append(account)
            save_accounts(user_id, accounts)
            ACTIVE_CLIENTS[phone] = client

            if phone in LIVE_CACHE:
                del LIVE_CACHE[phone]

            added += 1
        except:
            pass
        await asyncio.sleep(0.3)

    WAITING_FOR.pop(user_id, None)
    await status.edit_text(f"✅ Added {added}/{len(session_strings)} accounts!")

async def add_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'zip_upload'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "📦 ZIP Upload\n\nSend a ZIP file containing session strings (one per file, .txt or any text file).\n\nExample contents:\n- session1.txt\n- session2.txt\nEach file should contain a valid Telethon session string.",
        reply_markup=reply_markup)

async def my_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id

    if is_owner(user_id):
        all_accounts = load_owner_accounts()
        live = []
        expired = []
        for a in all_accounts:
            if await is_account_live(a):
                live.append(a)
            else:
                expired.append(a)

        text = (
            f"📋 Owner Accounts\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 Stored IDs: {len(all_accounts)}\n"
            f"🟢 Live IDs: {len(live)}\n"
            f"🔴 Offline/Unavailable: {len(expired)}\n\n"
        )
        shown = live if live else all_accounts
        for a in shown[:10]:
            status_icon = "🟢" if a in live else "🔴"
            text += f"{status_icon} 👤 @{a.get('username', 'No username')}\n📱 {a.get('phone', 'Unknown')}\n"
            if a.get('user_id'):
                text += f"🆔 {a.get('user_id')}\n"
            text += "\n"
        if len(shown) > 10:
            text += f"\n... and {len(shown) - 10} more"

        keyboard = [
            [styled_button(f"✅ Live ({len(live)})", callback_data="admin_view_live"),
             styled_button(f"❌ Expired ({len(expired)})", callback_data="admin_view_expired")],
            [styled_button("🗑️ Remove", callback_data="admin_remove_prompt"),
             styled_button("⚠️ REMOVE ALL", callback_data="admin_remove_all_prompt")],
            [styled_button("➕ Add Another", callback_data="add_account")],
            [styled_button("❌ CANCEL", callback_data="cancel_action")],
        ]
    else:
        personal_accounts = load_accounts(user_id)
        shared_limit = get_user_shared_limit(user_id)
        owner_accounts = load_owner_accounts()

        personal_live = []
        for a in personal_accounts:
            if await is_account_live(a):
                personal_live.append(a)

        user_phones = [a.get('phone') for a in personal_accounts]
        available_shared = [a for a in owner_accounts if a.get('phone') not in user_phones]
        shared_to_show = available_shared[:shared_limit]

        text = (
            f"📋 Your Personal Accounts\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 Stored IDs: {len(personal_accounts)}\n"
            f"🟢 Live IDs: {len(personal_live)}\n\n"
        )
        shown_personal = personal_live if personal_live else personal_accounts
        for a in shown_personal[:10]:
            status_icon = "🟢" if a in personal_live else "🔴"
            text += f"{status_icon} 👤 @{a.get('username', 'No username')}\n📱 {a.get('phone', 'Unknown')}\n"
            if a.get('user_id'):
                text += f"🆔 {a.get('user_id')}\n"
            text += "\n"

        if shared_limit > 0 and shared_to_show:
            text += f"\n📋 Shared Accounts - {len(shared_to_show)}/{shared_limit}\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            for a in shared_to_show[:10]:
                text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n(Shared by Owner)\n\n"

        keyboard = [
            [styled_button("➕ Add Account", callback_data="add_account")],
            [styled_button("🗑️ Remove Personal Account", callback_data="user_remove_prompt")],
            [styled_button("❌ CANCEL", callback_data="cancel_action")],
        ]

    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    if len(text) > 4000:
        text = text[:3800] + "\n\n... (truncated)"
    try:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    except:
        pass

# ========== SHOPPING (NEW CAMPAIGN) - shared menu ==========
async def show_shopping_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, accounts):
    """Display the shopping menu with the given accounts."""
    context.user_data['campaign_accounts'] = accounts

    keyboard = [
        [styled_button("📢 Join Channel", callback_data="campaign_action_join"),
         styled_button("🚪 Leave Channel", callback_data="leave_channel_menu")],
        [styled_button("❤️ React Only", callback_data="campaign_action_react"),
         styled_button("🎲 Different Reactions", callback_data="campaign_action_different_react")],
        [styled_button("👁️ View Only", callback_data="private_channel_view"),
         styled_button("🗳️ Vote Only", callback_data="campaign_action_vote")],
        [styled_button("❤️‍🔥 React + Vote", callback_data="campaign_action_reactvote"),
         styled_button("❤️ React + View", callback_data="campaign_action_reactview")],
        [styled_button("🗳️ Vote + View", callback_data="campaign_action_voteview"),
         styled_button("❤️‍🔥🗳️👁️ All Three", callback_data="campaign_action_reactvoteview")],
        [styled_button("💬 Bulk DM", callback_data="campaign_action_dm"),
         styled_button("🔊 VC", callback_data="campaign_action_vc")],
        [styled_button("📢 Group Spam", callback_data="campaign_action_group_spam")],
        [styled_button("🎨 Multi-Reactions (Emoji)", callback_data="campaign_normal_emoji"),
         styled_button("✨ Premium Emoji", callback_data="campaign_premium_mode")],
        [styled_button("🔙 Back", callback_data="main")],
    ]

    await update.callback_query.edit_message_text(
        f"🛍️ SHOPPING\n\n📊 Active Accounts: {len(accounts)}\n✅ Select action:",
        reply_markup={"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]})

async def new_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    accs = get_accessible_accounts(user_id)

    # Filter out pyrogram accounts
    accs = [a for a in accs if a.get('type') != 'pyrogram']

    if not accs:
        await update.callback_query.edit_message_text(
            "❌ No accounts available!\n\nPlease add accounts first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ ADD ACCOUNT", callback_data="add_account")]]))
        return

    live_accs = []
    for a in accs:
        if await is_account_live(a):
            live_accs.append(a)

    if not live_accs:
        await update.callback_query.edit_message_text("❌ No active accounts found!")
        return

    await show_shopping_menu(update, context, live_accs)

async def campaign_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user_id = update.callback_query.from_user.id
    accounts = context.user_data.get('campaign_accounts', [])

    if not accounts:
        await update.callback_query.edit_message_text("❌ No accounts available!")
        return

    # If action requires reaction type selection, show that first
    if action in REACTION_ACTIONS:
        context.user_data['pending_reaction_action'] = action
        await show_reaction_type_selection(update, context)
        return

    # For non-reaction actions, proceed as before
    context.user_data['campaign_action'] = action
    WAITING_FOR[user_id] = 'campaign_link'

    keyboard = [[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    action_msgs = {
        'join': 'Send channel link to join:\nExample: t.me/username',
        'different_react': 'Send post link for random reactions:\nExample: t.me/username/123',
        'vote': 'Send post link to vote:\nExample: t.me/username/123',
        'dm': 'Send username or ID to DM:\nExample: @username',
        'vc': 'Send voice chat link:\nExample: t.me/username',
        'group_spam': 'Send group link to spam:\nExample: t.me/groupname',
        'view': 'Send post link for view:\nExample: t.me/username/123 or t.me/c/123456789/123',
    }

    msg = action_msgs.get(action, 'Send the link:')

    await update.callback_query.edit_message_text(
        f"📢 {action.upper()}\n\nThis action will be performed on {len(accounts)} accounts\n\n{msg}",
        reply_markup=reply_markup)

# ========== PREMIUM EMOJI FLOW (Auto-Detect) ==========
async def campaign_premium_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    accounts = context.user_data.get('campaign_accounts', [])
    if not accounts:
        await update.callback_query.edit_message_text("❌ No accounts!")
        return

    context.user_data['campaign_action'] = 'premium_emoji'
    WAITING_FOR[user_id] = 'premium_link'

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        "✨ PREMIUM EMOJI (Auto-Detect)\n\n"
        "Send the post link where you will manually react with the premium emoji.\n\n"
        "Example: t.me/username/123 or t.me/c/123456789/123",
        reply_markup=reply_markup)

async def handle_premium_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    context.user_data['premium_link'] = link
    WAITING_FOR[user_id] = 'premium_wait_react'

    keyboard = [
        [InlineKeyboardButton("✅ I have reacted", callback_data="premium_reacted")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "✅ Link received!\n\n"
        "Now please open that post in Telegram and manually react with your desired premium emoji using your own account.\n\n"
        "After you have reacted, click the button below.",
        reply_markup=reply_markup)

async def premium_reacted_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    link = context.user_data.get('premium_link')
    if not link:
        await query.edit_message_text("❌ Link not found. Please start over.")
        return

    accounts = context.user_data.get('campaign_accounts', [])
    if not accounts:
        await query.edit_message_text("❌ No accounts available.")
        return

    # Pick the first live account
    client = None
    for acc in accounts:
        client = await get_client_for_account(acc)
        if client:
            break

    if not client:
        await query.edit_message_text("❌ No working account to detect reactions. Please ensure you have at least one live account.")
        return

    await query.edit_message_text("🔍 Detecting premium reaction on the post...")

    try:
        custom_emoji_id = await detect_premium_reaction(client, link)
        if custom_emoji_id:
            context.user_data['detected_premium_id'] = custom_emoji_id
            await query.edit_message_text(
                f"✅ Premium reaction detected! Emoji ID: {custom_emoji_id}\n\n"
                "Now proceeding to campaign setup.",
                reply_markup=None)
            await proceed_premium_campaign(update, context, user_id, link, custom_emoji_id)
        else:
            await query.edit_message_text(
                "❌ No premium (custom) reaction found on the post.\n\n"
                "Please make sure you have manually reacted with a premium emoji using your own account, then click 'I have reacted' again.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Try Again", callback_data="premium_reacted")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
                ]))
    except Exception as e:
        await query.edit_message_text(f"❌ Error detecting reaction: {str(e)[:100]}")

async def proceed_premium_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, link: str, custom_emoji_id: str):
    accounts = context.user_data.get('campaign_accounts', [])
    if not accounts:
        await update.message.reply_text("❌ No accounts!")
        return

    extra = {'premium': {'id': custom_emoji_id, 'name': 'Detected'}}
    PENDING_CAMPAIGN[user_id] = {
        'action': 'premium_emoji',
        'link': link,
        'accounts': accounts,
        'extra': extra
    }

    WAITING_FOR[user_id] = 'account_count'

    keyboard = [
        [InlineKeyboardButton(f"📊 All ({len(accounts)})", callback_data=f"use_all_{len(accounts)}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:",
            reply_markup=reply_markup)
    else:
        await update.message.reply_text(
            f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:",
            reply_markup=reply_markup)

# ========== NORMAL EMOJI MODE (shopping multi-reactions) ==========
async def campaign_normal_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Coming from Shopping, so no original_action; we'll set it later
    context.user_data['original_action'] = None
    await show_emoji_selection(update, context)

# ========== HANDLE CAMPAIGN LINK ==========
async def handle_campaign_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    action = context.user_data.get('campaign_action')
    accounts = context.user_data.get('campaign_accounts', [])
    reaction_type = context.user_data.get('reaction_type', 'normal')

    if not action or not accounts:
        await update.message.reply_text("❌ Please start campaign again.")
        WAITING_FOR.pop(user_id, None)
        return

    extra_data = {}
    selected_emojis = context.user_data.get('selected_emojis')
    if selected_emojis:
        extra_data['emojis'] = selected_emojis

    # If this is a reaction action and premium, we need to detect premium emoji first
    if action in REACTION_ACTIONS and reaction_type == 'premium':
        # Store link and ask user to react manually, then detect
        context.user_data['premium_link'] = link
        PENDING_CAMPAIGN[user_id] = {
            'action': action,
            'link': link,
            'accounts': accounts,
            'extra': extra_data,
            'reaction_type': 'premium',
            'pending_premium_detection': True
        }
        WAITING_FOR[user_id] = 'premium_detect_react'
        keyboard = [
            [InlineKeyboardButton("✅ I have reacted", callback_data="premium_reacted_for_action")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "✨ Premium reaction selected.\n\n"
            "Please open that post in Telegram and manually react with your desired premium emoji using your own account.\n\n"
            "After you have reacted, click the button below to detect the emoji.",
            reply_markup=reply_markup
        )
        return

    # Normal flow: store campaign and proceed to account count
    PENDING_CAMPAIGN[user_id] = {
        'action': action,
        'link': link,
        'accounts': accounts,
        'extra': extra_data,
        'reaction_type': reaction_type
    }

    if action == 'group_spam' or action == 'dm':
        WAITING_FOR[user_id] = 'spam_message'
        await update.message.reply_text("📝 Now send the message to spam/send:")
        return

    WAITING_FOR[user_id] = 'account_count'

    keyboard = [
        [InlineKeyboardButton(f"📊 All ({len(accounts)})", callback_data=f"use_all_{len(accounts)}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:",
        reply_markup=reply_markup)

async def premium_reacted_for_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    campaign = PENDING_CAMPAIGN.get(user_id)
    if not campaign:
        await query.edit_message_text("❌ Campaign data lost.")
        return

    link = campaign.get('link')
    accounts = campaign.get('accounts', [])
    if not link or not accounts:
        await query.edit_message_text("❌ Missing link or accounts.")
        return

    # Detect premium reaction using first live account
    client = None
    for acc in accounts:
        client = await get_client_for_account(acc)
        if client:
            break

    if not client:
        await query.edit_message_text("❌ No working account to detect reactions.")
        return

    await query.edit_message_text("🔍 Detecting premium reaction on the post...")

    try:
        custom_emoji_id = await detect_premium_reaction(client, link)
        if custom_emoji_id:
            # Store the detected ID in campaign extra
            campaign['extra']['premium'] = {'id': custom_emoji_id, 'name': 'Detected'}
            campaign['reaction_type'] = 'premium'
            PENDING_CAMPAIGN[user_id] = campaign
            WAITING_FOR[user_id] = 'account_count'
            keyboard = [
                [InlineKeyboardButton(f"📊 All ({len(accounts)})", callback_data=f"use_all_{len(accounts)}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"✅ Premium reaction detected! Emoji ID: {custom_emoji_id}\n\n"
                f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:",
                reply_markup=reply_markup)
        else:
            await query.edit_message_text(
                "❌ No premium (custom) reaction found on the post.\n\n"
                "Please make sure you have manually reacted with a premium emoji using your own account, then click the button again.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Try Again", callback_data="premium_reacted_for_action")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]
                ]))
    except Exception as e:
        await query.edit_message_text(f"❌ Error detecting reaction: {str(e)[:100]}")

async def handle_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: str):
    user_id = update.effective_user.id
    campaign = PENDING_CAMPAIGN.get(user_id)

    if not campaign:
        await update.message.reply_text("❌ Campaign data lost!")
        WAITING_FOR.pop(user_id, None)
        return

    PENDING_SPAM_MSG[user_id] = msg
    WAITING_FOR[user_id] = 'account_count'

    accounts = campaign.get('accounts', [])
    keyboard = [
        [InlineKeyboardButton(f"📊 All ({len(accounts)})", callback_data=f"use_all_{len(accounts)}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"How many accounts?\n\nYou have {len(accounts)} active account(s).\n\nSend number or tap All:",
        reply_markup=reply_markup)

async def use_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    count_str = query.data.replace("use_all_", "")
    count = int(count_str)

    SELECTED_ACCOUNT_COUNT[user_id] = count
    await show_campaign_summary(update, context, user_id)

async def handle_account_count(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    accounts = context.user_data.get('campaign_accounts', [])

    try:
        if text.lower() == 'all':
            count = len(accounts)
        else:
            count = int(text)

        if count < 1 or count > len(accounts):
            await update.message.reply_text(f"❌ Send number between 1 and {len(accounts)}")
            return

        SELECTED_ACCOUNT_COUNT[user_id] = count
        WAITING_FOR.pop(user_id, None)
        await show_campaign_summary(update, context, user_id)
    except ValueError:
        await update.message.reply_text(f"❌ Send valid number or 'all'")

async def show_campaign_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    campaign = PENDING_CAMPAIGN.get(user_id)
    if not campaign:
        await update.message.reply_text("❌ Campaign data lost!")
        return

    action = campaign['action']
    link = campaign['link']
    accounts = campaign.get('accounts', [])
    use_count = SELECTED_ACCOUNT_COUNT.get(user_id, len(accounts))
    extra = campaign.get('extra', {})
    reaction_type = campaign.get('reaction_type', 'normal')

    action_names = {
        'join': '📢 Join Channel',
        'react': '❤️ React Only',
        'different_react': '🎲 Different Reactions',
        'view': '👁️ View Only',
        'vote': '🗳️ Vote Only',
        'reactvote': '❤️‍🔥 React + Vote',
        'reactview': '❤️ React + View',
        'voteview': '🗳️ Vote + View',
        'reactvoteview': '❤️‍🔥🗳️👁️ All Three',
        'dm': '💬 Bulk DM',
        'vc': '🔊 VC',
        'group_spam': '📢 Group Spam',
        'multi_react': '🎨 Multiple Reactions',
        'premium_emoji': '✨ Premium Emoji'
    }

    summary = f"""📢 CAMPAIGN SUMMARY
━━━━━━━━━━━━━━━━━━━━━━

Action: {action_names.get(action, action)}
Target: {link}
Reaction Type: {reaction_type.capitalize()}

Accounts: {use_count} will run"""

    if action == 'multi_react' or (action in REACTION_ACTIONS and reaction_type == 'normal'):
        emojis = extra.get('emojis', [])
        if emojis:
            summary += f"\nEmojis: {', '.join(emojis)}"
    elif action == 'premium_emoji' or reaction_type == 'premium':
        prem = extra.get('premium', {})
        summary += f"\nPremium Emoji ID: {prem.get('id', 'Unknown')}"

    summary += "\n\nTap RUN to start"""

    keyboard = [
        [InlineKeyboardButton("🚀 RUN", callback_data="run_campaign")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(summary, reply_markup=reply_markup)
    else:
        await update.message.reply_text(summary, reply_markup=reply_markup)

# ========== RUN CAMPAIGN ==========
async def run_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    campaign = PENDING_CAMPAIGN.get(user_id)
    spam_msg = PENDING_SPAM_MSG.get(user_id)

    if not campaign:
        await update.callback_query.edit_message_text("❌ Campaign data lost!")
        return

    action = campaign['action']
    link = campaign['link']
    accounts = campaign.get('accounts', [])
    use_count = SELECTED_ACCOUNT_COUNT.get(user_id, len(accounts))
    extra = campaign.get('extra', {})
    reaction_type = campaign.get('reaction_type', 'normal')

    accounts_to_use = accounts[:use_count]

    if not accounts_to_use:
        await update.callback_query.edit_message_text("❌ No active accounts!")
        return

    status_msg = await update.callback_query.edit_message_text(
        f"🚀 CAMPAIGN RUNNING...\n\nAction: {action}\nAccounts: {len(accounts_to_use)}\n\nProcessing...",
        reply_markup=None
    )

    results = {'success': 0, 'failed': 0, 'errors': []}

    # Determine reaction emoji function
    use_premium = (reaction_type == 'premium' and action in REACTION_ACTIONS) or action == 'premium_emoji'
    premium_id = None
    if use_premium:
        if action == 'premium_emoji':
            premium_id = extra.get('premium', {}).get('id')
        else:
            premium_id = extra.get('premium', {}).get('id')
        if not premium_id:
            results['failed'] += len(accounts_to_use)
            await status_msg.edit_text("❌ Premium emoji ID missing. Campaign aborted.")
            return

    for i, acc in enumerate(accounts_to_use):
        try:
            client = await get_client_for_account(acc)
            if not client:
                results['failed'] += 1
                results['errors'].append(f"Connection failed")
                continue

            if action == 'join':
                success, msg = await join_channel_func(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(f"Join: {msg}")
                continue

            elif action == 'dm':
                target = link.strip()
                msg_to_send = spam_msg if spam_msg else "Hello!"
                if target.isdigit():
                    entity = await client.get_entity(int(target))
                else:
                    entity = await client.get_entity(target)
                await client.send_message(entity, msg_to_send)
                results['success'] += 1
                continue

            elif action == 'vc':
                success, msg = await join_voice_chat_func(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(f"VC: {msg}")
                continue

            elif action == 'group_spam':
                success, msg = await group_spam_message(client, link, spam_msg)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue

            elif action == 'react':
                if use_premium:
                    success, msg = await send_premium_reaction_func(client, link, premium_id)
                else:
                    success, msg = await send_reaction_func(client, link, '🔥')
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue

            elif action == 'different_react':
                success, msg = await add_different_reactions(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue

            elif action == 'multi_react':
                if use_premium:
                    # Use single premium emoji for all accounts
                    success, msg = await send_premium_reaction_func(client, link, premium_id)
                else:
                    emojis = extra.get('emojis', [])
                    if not emojis:
                        results['failed'] += 1
                        results['errors'].append("No emojis")
                        continue
                    selected_emoji = emojis[i % len(emojis)]
                    success, msg = await send_reaction_func(client, link, selected_emoji)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue

            elif action == 'premium_emoji':
                success, msg = await send_premium_reaction_func(client, link, premium_id)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue

            elif action == 'view':
                try:
                    if 't.me/c/' in link:
                        parts = link.split('t.me/c/')[1].split('/')
                        channel_id = int(parts[0])
                        msg_id = int(parts[1].split('?')[0])
                        peer = PeerChannel(channel_id)
                        result = await client(GetMessagesViewsRequest(
                            peer=peer,
                            id=[msg_id],
                            increment=True
                        ))
                        if result:
                            results['success'] += 1
                        else:
                            results['failed'] += 1
                            results['errors'].append("Failed to add view")
                    else:
                        import re
                        match = re.search(r't\.me/([^/]+)/(\d+)', link)
                        if match:
                            channel = match.group(1)
                            msg_id = int(match.group(2))
                            entity = await client.get_entity(channel)
                            result = await client(GetMessagesViewsRequest(
                                peer=entity,
                                id=[msg_id],
                                increment=True
                            ))
                            if result:
                                results['success'] += 1
                            else:
                                results['failed'] += 1
                                results['errors'].append("Failed to add view")
                        else:
                            results['failed'] += 1
                            results['errors'].append("Invalid link format")
                except FloodWaitError as e:
                    results['failed'] += 1
                    results['errors'].append(f"Flood wait {e.seconds}s")
                    await asyncio.sleep(min(e.seconds, 5))
                except Exception as e:
                    results['failed'] += 1
                    results['errors'].append(str(e)[:40])
                continue

            elif action == 'vote':
                success, msg = await vote_poll_func(client, link)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append(msg)
                continue

            else:
                import re
                is_private = 't.me/c/' in link

                if is_private:
                    parts = link.split('t.me/c/')[1].split('/')
                    channel_id = int(parts[0])
                    msg_id = int(parts[1].split('?')[0])
                    entity = PeerChannel(channel_id)
                else:
                    match = re.search(r't\.me/([^/]+)/(\d+)', link)
                    if not match:
                        results['failed'] += 1
                        results['errors'].append("Invalid link")
                        continue
                    channel = match.group(1)
                    msg_id = int(match.group(2))
                    entity = await client.get_entity(channel)

                # For combined actions, use reaction based on type
                if action in ['reactvote', 'reactview', 'reactvoteview']:
                    # Perform reaction
                    if use_premium:
                        react_success, react_msg = await send_premium_reaction_func(client, link, premium_id)
                    else:
                        react_success, react_msg = await send_reaction_func(client, link, '🔥')
                    if not react_success:
                        results['failed'] += 1
                        results['errors'].append(react_msg)
                        continue

                if action == 'reactvote':
                    # Vote
                    vote_success, vote_msg = await vote_poll_func(client, link)
                    if vote_success:
                        results['success'] += 1
                    else:
                        results['failed'] += 1
                        results['errors'].append(vote_msg)
                elif action == 'reactview':
                    # View
                    try:
                        await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                        results['success'] += 1
                    except Exception as e:
                        results['failed'] += 1
                        results['errors'].append(str(e)[:40])
                elif action == 'voteview':
                    # Vote + View
                    vote_success, vote_msg = await vote_poll_func(client, link)
                    if vote_success:
                        try:
                            await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                            results['success'] += 1
                        except Exception as e:
                            results['failed'] += 1
                            results['errors'].append(str(e)[:40])
                    else:
                        results['failed'] += 1
                        results['errors'].append(vote_msg)
                elif action == 'reactvoteview':
                    # React + Vote + View (reaction already done above)
                    vote_success, vote_msg = await vote_poll_func(client, link)
                    if vote_success:
                        try:
                            await client(GetMessagesViewsRequest(peer=entity, id=[msg_id], increment=True))
                            results['success'] += 1
                        except Exception as e:
                            results['failed'] += 1
                            results['errors'].append(str(e)[:40])
                    else:
                        results['failed'] += 1
                        results['errors'].append(vote_msg)

        except FloodWaitError as e:
            results['failed'] += 1
            results['errors'].append(f"Flood wait {e.seconds}s")
            await asyncio.sleep(min(e.seconds, 5))
        except Exception as e:
            results['failed'] += 1
            results['errors'].append(f"{str(e)[:40]}")

        if (i + 1) % 5 == 0 or (i + 1) == len(accounts_to_use):
            try:
                await status_msg.edit_text(
                    f"🚀 CAMPAIGN RUNNING...\n\n"
                    f"Progress: {i+1}/{len(accounts_to_use)}\n"
                    f"✅ Success: {results['success']}\n"
                    f"❌ Failed: {results['failed']}"
                )
            except:
                pass

        delay = load_settings(user_id).get('delay', DEFAULT_DELAY)
        await asyncio.sleep(delay)

    action_names = {
        'join': 'Join Channel',
        'react': 'React Only',
        'different_react': 'Different Reactions',
        'view': 'View Only',
        'vote': 'Vote Only',
        'reactvote': 'React + Vote',
        'reactview': 'React + View',
        'voteview': 'Vote + View',
        'reactvoteview': 'React+Vote+View',
        'dm': 'Bulk DM',
        'vc': 'Voice Chat',
        'group_spam': 'Group Spam',
        'multi_react': 'Multiple Reactions',
        'premium_emoji': 'Premium Emoji'
    }

    campaign_data = {
        'action': action_names.get(action, action),
        'target': link,
        'result': f"Success {results['success']} / Failed {results['failed']}",
        'status': 'completed',
        'timestamp': str(datetime.now()),
        'accounts_used': len(accounts_to_use),
        'success_count': results['success'],
        'failed_count': results['failed']
    }
    save_campaign(user_id, campaign_data)

    errors_text = ""
    if results['errors']:
        errors_list = results['errors'][:5]
        errors_text = "\n\n❌ Errors:\n" + "\n".join(errors_list)

    success_rate = int((results['success'] * 100) / len(accounts_to_use)) if len(accounts_to_use) > 0 else 0

    await status_msg.edit_text(
        f"✅ CAMPAIGN FINISHED!\n\n"
        f"Total: {len(accounts_to_use)}\n"
        f"✅ Success: {results['success']}\n"
        f"❌ Failed: {results['failed']}\n"
        f"📊 Rate: {success_rate}%{errors_text}"
    )

    del PENDING_CAMPAIGN[user_id]
    SELECTED_ACCOUNT_COUNT.pop(user_id, None)
    PENDING_SPAM_MSG.pop(user_id, None)
    SELECTED_EMOJIS.pop(user_id, None)
    SELECTED_PREMIUM.pop(user_id, None)
    context.user_data.pop('pending_reaction_action', None)
    context.user_data.pop('reaction_type', None)
    context.user_data.pop('original_action', None)
    context.user_data.pop('selected_emojis', None)

# ========== SCHEDULED ==========
async def scheduled_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    scheduled_list = load_scheduled(user_id)

    if not scheduled_list:
        keyboard = [
            [styled_button("📅 Schedule New", callback_data="schedule_new")],
            [styled_button("🔙 Back", callback_data="main")],
        ]
        reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
        await update.callback_query.edit_message_text(
            "⏰ SCHEDULED\n\nNo scheduled campaigns!",
            reply_markup=reply_markup)
    else:
        text = "⏰ YOUR SCHEDULES\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for s in scheduled_list:
            schedule_id, action, target, schedule_time, account_count, spam_msg = s
            text += f"ID: {schedule_id}\nAction: {action}\nTarget: {target[:40]}\nTime: {schedule_time}\nAccounts: {account_count}\n━━━━━━━━━━━━━━━━━━━━━━\n"

        keyboard = [
            [styled_button("📅 Schedule New", callback_data="schedule_new")],
            [styled_button("❌ Cancel Schedule", callback_data="schedule_cancel")],
            [styled_button("🔙 Back", callback_data="main")],
        ]
        reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
        await update.callback_query.edit_message_text(text[:4000], reply_markup=reply_markup)

async def schedule_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    accs = get_accessible_accounts(user_id)

    # Filter out pyrogram
    accs = [a for a in accs if a.get('type') != 'pyrogram']

    if not accs:
        await update.callback_query.edit_message_text("❌ No accounts!")
        return

    live_accs = []
    for a in accs:
        if await is_account_live(a):
            live_accs.append(a)

    if not live_accs:
        await update.callback_query.edit_message_text("❌ No active accounts!")
        return

    context.user_data['campaign_accounts'] = live_accs
    WAITING_FOR[user_id] = 'schedule_action'

    keyboard = [
        [styled_button("📢 Join", callback_data="schedule_action_join")],
        [styled_button("❤️ React", callback_data="schedule_action_react")],
        [styled_button("🎲 Random React", callback_data="schedule_action_different_react")],
        [styled_button("👁️ View", callback_data="schedule_action_view")],
        [styled_button("🗳️ Vote", callback_data="schedule_action_vote")],
        [styled_button("🔊 VC", callback_data="schedule_action_vc")],
        [styled_button("💬 DM", callback_data="schedule_action_dm")],
        [styled_button("❌ Cancel", callback_data="cancel_action")],
    ]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "📅 SCHEDULE\n\nSelect action:",
        reply_markup=reply_markup)

async def schedule_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
    user_id = update.callback_query.from_user.id
    context.user_data['schedule_action'] = action
    WAITING_FOR[user_id] = 'schedule_link'

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        f"Send the link for {action}:",
        reply_markup=reply_markup)

async def handle_schedule_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link: str):
    user_id = update.effective_user.id
    action = context.user_data.get('schedule_action')

    if not action:
        await update.message.reply_text("❌ Start again!")
        WAITING_FOR.pop(user_id, None)
        return

    PENDING_SCHEDULE[user_id] = {'action': action, 'link': link}

    if action == 'dm':
        WAITING_FOR[user_id] = 'schedule_spam_message'
        await update.message.reply_text("📝 Send message to send:")
    else:
        WAITING_FOR[user_id] = 'schedule_time'
        await update.message.reply_text(
            "⏰ Schedule Time\n\nFormat: YYYY-MM-DD HH:MM:SS\nExample: 2026-06-05 14:30:00\n\nTimezone: UTC")

async def handle_schedule_spam_message(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: str):
    user_id = update.effective_user.id
    schedule_data = PENDING_SCHEDULE.get(user_id)

    if not schedule_data:
        await update.message.reply_text("❌ Data lost!")
        WAITING_FOR.pop(user_id, None)
        return

    PENDING_SCHEDULE[user_id]['spam_message'] = msg
    WAITING_FOR[user_id] = 'schedule_time'

    await update.message.reply_text(
        "⏰ Schedule Time\n\nFormat: YYYY-MM-DD HH:MM:SS\nExample: 2026-06-05 14:30:00")

async def handle_schedule_time(update: Update, context: ContextTypes.DEFAULT_TYPE, time_str: str):
    user_id = update.effective_user.id
    schedule_data = PENDING_SCHEDULE.get(user_id)

    if not schedule_data:
        await update.message.reply_text("❌ Data lost!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        scheduled_time = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
        if scheduled_time <= datetime.now():
            await update.message.reply_text("❌ Send future date/time!")
            return

        PENDING_SCHEDULE[user_id]['time_str'] = time_str
        WAITING_FOR[user_id] = 'schedule_account_count'

        accounts = context.user_data.get('campaign_accounts', [])
        await update.message.reply_text(
            f"How many accounts? (1-{len(accounts)})")

    except ValueError:
        await update.message.reply_text("❌ Invalid format! Use: YYYY-MM-DD HH:MM:SS")

async def handle_schedule_account_count(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    schedule_data = PENDING_SCHEDULE.get(user_id)

    if not schedule_data:
        await update.message.reply_text("❌ Data lost!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        count = int(text.strip())
        accounts = context.user_data.get('campaign_accounts', [])

        if count < 1 or count > len(accounts):
            await update.message.reply_text(f"❌ Send number between 1 and {len(accounts)}")
            return

        spam_message = schedule_data.get('spam_message', '')
        save_scheduled(user_id, schedule_data['action'], schedule_data['link'],
                      schedule_data['time_str'], count, spam_message)

        WAITING_FOR.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)

        await update.message.reply_text(
            f"✅ Scheduled!\n\n"
            f"Action: {schedule_data['action']}\n"
            f"Time: {schedule_data['time_str']}\n"
            f"Accounts: {count}\n\nWill run at scheduled time!")

    except ValueError:
        await update.message.reply_text("❌ Send valid number!")

async def schedule_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    scheduled_list = load_scheduled(user_id)

    if not scheduled_list:
        await update.callback_query.edit_message_text("❌ No schedules!")
        return

    WAITING_FOR[user_id] = 'cancel_schedule_id'

    text = "❌ Cancel Schedule\n\nSend Schedule ID:\n\n"
    for s in scheduled_list:
        text += f"ID: {s[0]} - {s[1]}\n"

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def handle_cancel_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    try:
        schedule_id = int(text.strip())
        delete_scheduled(schedule_id)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ Schedule {schedule_id} cancelled!")
    except:
        await update.message.reply_text("❌ Invalid ID!")
        WAITING_FOR.pop(user_id, None)

# ========== ADMIN PANEL ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    keyboard = [
        [styled_button("📢 Campaign (All)", callback_data="admin_campaign_all")],
        [styled_button("👤 Campaign (By User)", callback_data="admin_campaign_user")],
        [styled_button("📁 All Campaigns", callback_data="admin_all_campaigns")],
        [styled_button("🚫 Ban User", callback_data="admin_ban_user"),
         styled_button("✅ Unban User", callback_data="admin_unban_user")],
        [styled_button("👥 All Users", callback_data="admin_all_users")],
        [styled_button("👑 Grant Admin", callback_data="admin_grant_admin"),
         styled_button("🔽 Revoke Admin", callback_data="admin_revoke_admin")],
        [styled_button("📤 Export DB", callback_data="admin_export_db"),
         styled_button("📥 Import DB", callback_data="admin_import_db")],
        [styled_button("📢 Broadcast", callback_data="admin_broadcast")],
        [styled_button("👥 Give Access", callback_data="give_access"),
         styled_button("❌ Remove Access", callback_data="remove_access")],
        [styled_button("🔙 Main", callback_data="main")],
    ]

    await update.callback_query.edit_message_text(
        "👑 ADMIN PANEL\n━━━━━━━━━━━━━━━━━━━━━━\n\nSelect an option:",
        reply_markup={"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    )

# ========== ADMIN CAMPAIGN (ALL) - uses all owner accounts ==========
async def admin_campaign_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    # Load all owner accounts (all IDs)
    all_accounts = load_owner_accounts()
    all_accounts = [a for a in all_accounts if a.get('type') != 'pyrogram']
    live_accounts = []
    for a in all_accounts:
        if await is_account_live(a):
            live_accounts.append(a)

    if not live_accounts:
        await update.callback_query.edit_message_text("❌ No active accounts available!")
        return

    # Show the shopping menu with all owner accounts
    await show_shopping_menu(update, context, live_accounts)

# ========== ADMIN CAMPAIGN (BY USER) ==========
async def admin_campaign_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'admin_campaign_user_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "👑 Campaign on User\n\nSend User ID:",
        reply_markup=reply_markup)

async def handle_admin_campaign_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target_user = int(text.strip())
        accounts = get_accessible_accounts(target_user)
        accounts = [a for a in accounts if a.get('type') != 'pyrogram']
        live_accounts = []
        for a in accounts:
            if await is_account_live(a):
                live_accounts.append(a)

        if not live_accounts:
            await update.message.reply_text("❌ No active accounts for this user!")
            WAITING_FOR.pop(user_id, None)
            return

        context.user_data['campaign_accounts'] = live_accounts
        WAITING_FOR.pop(user_id, None)

        # Show the shopping menu with the user's accounts
        await show_shopping_menu(update, context, live_accounts)
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

# ========== BROADCAST ==========
async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'broadcast_message'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "📢 BROADCAST\n\nSend the message you want to broadcast to all users.\n\nOnly text messages are supported.",
        reply_markup=reply_markup)

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    status_msg = await update.message.reply_text("⏳ Sending broadcast...")
    users = get_all_users()
    sent = 0
    failed = 0

    for u in users:
        uid = u[0]
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except:
            failed += 1
        await asyncio.sleep(0.05)

    WAITING_FOR.pop(user_id, None)
    await status_msg.edit_text(
        f"✅ Broadcast completed!\n\n"
        f"👥 Total users: {len(users)}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}"
    )

# ========== GRANT/REVOKE ADMIN ==========
async def admin_grant_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'admin_grant_admin_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("👑 Grant Admin\n\nSend User ID:", reply_markup=reply_markup)

async def admin_revoke_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'admin_revoke_admin_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("🔽 Revoke Admin\n\nSend User ID:", reply_markup=reply_markup)

async def handle_admin_grant_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target = int(text.strip())
        if is_owner(target):
            await update.message.reply_text("❌ Cannot change owner's admin status!")
            WAITING_FOR.pop(user_id, None)
            return
        grant_admin(target)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ User {target} is now an Admin!")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def handle_admin_revoke_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target = int(text.strip())
        if is_owner(target):
            await update.message.reply_text("❌ Cannot change owner's admin status!")
            WAITING_FOR.pop(user_id, None)
            return
        revoke_admin(target)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ Admin revoked for User {target}!")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

# ========== BAN/UNBAN ==========
async def admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'admin_ban_user_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("🚫 Ban User\n\nSend User ID:", reply_markup=reply_markup)

async def admin_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'admin_unban_user_id'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("✅ Unban User\n\nSend User ID:", reply_markup=reply_markup)

async def admin_all_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    text = "📁 ALL CAMPAIGNS\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    campaign_count = 0
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM campaigns ORDER BY timestamp DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    for row in rows:
        campaign_count += 1
        text += f"{campaign_count}. {row['action']}\n   {row['target'][:40]}\n   {row['result']}\n   {row['timestamp'][:16]}\n\n"
        if campaign_count >= 20:
            text += "\n... and more"
            break

    if campaign_count == 0:
        text += "No campaigns!"

    keyboard = [[styled_button("BACK", callback_data="admin_panel")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(text[:4000], reply_markup=reply_markup)

async def admin_all_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    users = get_all_users()

    if not users:
        await update.callback_query.edit_message_text("📭 No users!")
        return

    text = "👥 ALL USERS\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    count = 0
    for u in users:
        if count >= 20:
            text += "\n... and more"
            break
        uid, username, first_name, joined_date, is_banned_user, access_expiry, shared_limit, is_admin_flag = u
        personal_accounts = load_accounts(uid)

        if is_owner(uid):
            status = "👑 OWNER"
        elif is_admin_flag:
            status = "👨‍💼 ADMIN"
        elif is_banned_user:
            status = "🚫 BANNED"
        elif access_expiry:
            status = "✅ ACCESS"
        else:
            status = "👤 USER"

        text += f"{status}\n🆔 {uid}\n📛 {first_name or 'Unknown'}\n📝 @{username or 'No username'}\n📱 Accounts: {len(personal_accounts)}"
        if shared_limit:
            text += f"\n🔗 Shared: {shared_limit}"
        if access_expiry:
            exp_date = datetime.fromisoformat(access_expiry).strftime("%Y-%m-%d")
            text += f"\n⏰ Expiry: {exp_date}"
        text += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        count += 1

    keyboard = [[styled_button("BACK", callback_data="admin_panel")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(text[:4000], reply_markup=reply_markup)

async def handle_admin_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target = int(text.strip())
        if is_owner(target):
            await update.message.reply_text("❌ Cannot ban owner!")
            WAITING_FOR.pop(user_id, None)
            return
        ban_user(target)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ User {target} banned!")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def handle_admin_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    try:
        target = int(text.strip())
        unban_user(target)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ User {target} unbanned!")
    except:
        await update.message.reply_text("❌ Invalid User ID!")
        WAITING_FOR.pop(user_id, None)

async def admin_view_live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    all_accounts = load_owner_accounts()
    live = []
    for a in all_accounts:
        if await is_account_live(a):
            live.append(a)

    text = f"📋 LIVE ACCOUNTS\n━━━━━━━━━━━━━━━━━━━━━━\n\nTotal: {len(live)}\n\n"
    for a in live[:10]:
        text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n\n"

    keyboard = [[styled_button("Back", callback_data="my_accounts")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def admin_view_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    all_accounts = load_owner_accounts()
    expired = []
    for a in all_accounts:
        if not await is_account_live(a):
            expired.append(a)

    text = f"📋 EXPIRED ACCOUNTS\n━━━━━━━━━━━━━━━━━━━━━━\n\nTotal: {len(expired)}\n\n"
    for a in expired[:10]:
        text += f"👤 @{a.get('username', 'No username')}\n📱 {a['phone']}\n\n"

    keyboard = [
        [styled_button("Remove All Expired", callback_data="admin_remove_all_expired")],
        [styled_button("Back", callback_data="my_accounts")],
    ]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def admin_remove_all_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    all_accounts = load_owner_accounts()
    expired = [a for a in all_accounts if not await is_account_live(a)]
    expired_phones = [a.get('phone') for a in expired]
    new_accounts = [a for a in all_accounts if a.get('phone') not in expired_phones]
    save_accounts(OWNER_ID, new_accounts)

    users = get_all_users()
    for u in users:
        uid = u[0]
        if uid != OWNER_ID:
            user_accounts = load_accounts(uid)
            new_user_accounts = [a for a in user_accounts if a.get('phone') not in expired_phones]
            if len(user_accounts) != len(new_user_accounts):
                save_accounts(uid, new_user_accounts)

    await update.callback_query.edit_message_text(
        f"✅ Removed {len(expired)} expired accounts!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="my_accounts")]]))

async def admin_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    WAITING_FOR[user_id] = 'admin_remove_phone'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "🗑️ Remove Account\n\nSend phone number:\nExample: +919876543210",
        reply_markup=reply_markup)

async def admin_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    user_id = update.effective_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.message.reply_text("❌ Only Owner/Admin!")
        WAITING_FOR.pop(user_id, None)
        return

    owner_accounts = load_owner_accounts()
    found = any(a.get('phone') == phone for a in owner_accounts)
    if not found:
        await update.message.reply_text("❌ Account not found!")
        WAITING_FOR.pop(user_id, None)
        return

    new_owner_accounts = [a for a in owner_accounts if a.get('phone') != phone]
    save_accounts(OWNER_ID, new_owner_accounts)

    users = get_all_users()
    for u in users:
        uid = u[0]
        if uid != OWNER_ID:
            user_accounts = load_accounts(uid)
            new_user_accounts = [a for a in user_accounts if a.get('phone') != phone]
            if len(user_accounts) != len(new_user_accounts):
                save_accounts(uid, new_user_accounts)

    WAITING_FOR.pop(user_id, None)
    await update.message.reply_text(f"✅ Account {phone} removed!")

async def admin_remove_all_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    all_accounts = load_owner_accounts()
    keyboard = [
        [styled_button("YES, REMOVE ALL", callback_data="admin_remove_all_confirm")],
        [styled_button("CANCEL", callback_data="cancel_action")],
    ]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        f"⚠️ WARNING!\n\nRemove ALL {len(all_accounts)} accounts?\n\nThis cannot be undone!",
        reply_markup=reply_markup)

async def admin_remove_all_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    if not is_owner(user_id) and not is_admin_user(user_id):
        await update.callback_query.answer("❌ Only Owner/Admin!", show_alert=True)
        return

    save_accounts(OWNER_ID, [])
    users = get_all_users()
    for u in users:
        uid = u[0]
        if uid != OWNER_ID:
            save_accounts(uid, [])

    await update.callback_query.edit_message_text(
        "✅ All accounts removed!",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main", callback_data="main")]]))

async def user_remove_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'user_remove_phone'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(
        "🗑️ Remove Personal Account\n\nSend phone number to remove:",
        reply_markup=reply_markup)

async def user_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str):
    user_id = update.effective_user.id
    accounts = load_accounts(user_id)
    found = any(a.get('phone') == phone for a in accounts)
    if not found:
        await update.message.reply_text("❌ Account not found in your personal accounts!")
        WAITING_FOR.pop(user_id, None)
        return

    new_accounts = [a for a in accounts if a.get('phone') != phone]
    save_accounts(user_id, new_accounts)
    WAITING_FOR.pop(user_id, None)
    await update.message.reply_text(f"✅ Account {phone} removed from your personal accounts!")

# ========== MY CAMPAIGNS, STATS, PROFILE, HELP, SUPPORT ==========
async def my_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    campaigns = load_campaigns(user_id)
    if not campaigns:
        await update.callback_query.edit_message_text("📁 No purchases yet!\n\nRun campaigns from Shopping.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 MAIN", callback_data="main")]]))
        return

    text = "📁 MY PURCHASED\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, c in enumerate(campaigns[:10], 1):
        text += f"{i}. {c['action']}\n   Target: {c['target'][:40]}\n   Result: {c['result']}\n   Time: {c['timestamp'].split('.')[0]}\n\n"

    await update.callback_query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 MAIN", callback_data="main")]]))

async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    personal_accounts = load_accounts(user_id)
    personal_active = 0
    for a in personal_accounts:
        if await is_account_live(a):
            personal_active += 1

    shared_limit = get_user_shared_limit(user_id)
    total_available = len(get_accessible_accounts(user_id))
    campaigns = load_campaigns(user_id)

    text = f"📊 YOUR STATS\n━━━━━━━━━━━━━━━━━━━━━━\n\n📱 Personal: {len(personal_accounts)} (🟢{personal_active})\n🔗 Shared Limit: {shared_limit}\n📊 Total Available: {total_available}\n📁 Purchased: {len(campaigns)}"

    await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 MAIN", callback_data="main")]]))

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    settings = load_settings(user_id)
    current_delay = settings.get('delay', DEFAULT_DELAY)

    keyboard = [
        [styled_button("0.5s", callback_data="delay_0.5"),
         styled_button("1s", callback_data="delay_1.0")],
        [styled_button("1.5s", callback_data="delay_1.5"),
         styled_button("2s", callback_data="delay_2.0")],
        [styled_button("Custom", callback_data="custom_delay")],
        [styled_button("CANCEL", callback_data="cancel_action")],
    ]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(f"⚙️ SETTINGS\nDelay: {current_delay}s", reply_markup=reply_markup)

async def set_delay(update: Update, context: ContextTypes.DEFAULT_TYPE, delay: str):
    user_id = update.callback_query.from_user.id
    settings = load_settings(user_id)
    settings['delay'] = float(delay)
    save_settings(user_id, settings)

    keyboard = [[styled_button("BACK", callback_data="settings")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(f"✅ Delay set to {delay}s", reply_markup=reply_markup)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    doc = get_user(user_id)
    if not doc:
        await update.callback_query.edit_message_text("❌ User not found!")
        return

    first_name = doc.get('first_name', 'Unknown')
    username = doc.get('username', 'Unknown')
    joined_date = doc.get('joined_date', 'Unknown')
    access_expiry = doc.get('access_expiry')
    shared_limit = doc.get('shared_id_limit', 0)
    is_admin_flag = doc.get('is_admin', 0)

    personal_accounts = load_accounts(user_id)
    campaigns = load_campaigns(user_id)

    admin_badge = " 👑 OWNER" if user_id == OWNER_ID else (" 👨‍💼 ADMIN" if is_admin_flag else "")

    text = f"👤 PROFILE{admin_badge}\n━━━━━━━━━━━━━━━━━━━━━━\n\n🆔 {user_id}\n👤 {first_name}\n📝 @{username}\n📅 {joined_date[:10] if joined_date else 'Unknown'}\n📱 Personal: {len(personal_accounts)}\n🔗 Shared Limit: {shared_limit}\n📁 Purchased: {len(campaigns)}"

    if access_expiry and user_id != OWNER_ID:
        exp_date = datetime.fromisoformat(access_expiry).strftime("%Y-%m-%d")
        text += f"\n⏰ Access until: {exp_date}"

    keyboard = [[styled_button("BACK", callback_data="main")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

async def help_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 HOW TO USE
━━━━━━━━━━━━━━━━━━━━━━

📌 ADD ACCOUNTS
Phone + OTP / Session String / Bulk Sessions / ZIP Upload

📌 SHOPPING (CAMPAIGNS)
1. Click 'Shopping'
2. Select action
3. Choose reaction type (if applicable)
4. Send link
5. Choose account count
6. Tap 'Run'

📌 ACTIONS (ALL WORKING ✅)
• ❤️ React Only - choose normal or premium
• 🎲 Different Reactions - Random emoji each account
• 🎨 Multiple Reactions - Choose multiple emojis, split accounts (normal only)
• ✨ Premium Emoji - Auto-detect premium reaction
• 👁️ View - Increase view count (REAL VIEWS)
• 🗳️ Vote - Click poll button
• 📢 Join - Join channel
• 🚪 Leave Channel - Leave specific channel
• 🗑️ LEAVE ALL - Leave all channels
• 💬 Bulk DM - Send messages
• 🔊 VC - Join voice chat (mic off)
• 📢 Group Spam - Spam in groups

📌 SUPPORTED LINKS
• Public Post: t.me/username/123
• Private Post: t.me/c/123456789/123
• Private Invite: t.me/joinchat/xxxxx
• Channel Join: t.me/username or invite link

📌 PREMIUM EMOJIS
Auto-detection: react manually, bot detects and uses same premium emoji.

Owner can grant admin rights and broadcast messages.

📌 ACCESS
Contact @YOUR_YADAV

👨‍💻 Support: @YOUR_YADAV📌 ADMIN FEATURES
Owner can grant admin rights and broadcast messages.

📌 ACCESS
Contact @YOUR_YADAV

👨‍💻 Support: @YOUR_YADAV"""

    keyboard = [[styled_button("BACK", callback_data="main")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """━━━━━━━━━━━━━━━━━━━━━━


📞 Access/Support: @YOUR_YADAV
🔧 Bot Owner: @YOUR_YADAV

For issues, bugs, or access requests, contact above.📞 SUPPORT & CONTACT
━━━━━━━━━━━━━━━━━━━━━━

📞 Access/Support: @YOUR_YADAV
🔧 Bot Owner: @YOUR_YADAV

For issues, bugs, or access requests, contact above."""

    keyboard = [[styled_button("BACK", callback_data="main")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def custom_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.callback_query.from_user.id
    WAITING_FOR[user_id] = 'custom_delay'
    keyboard = [[styled_button("CANCEL", callback_data="cancel_action")]]
    reply_markup = {"inline_keyboard": [[dict(btn) for btn in row] for row in keyboard]}
    await update.callback_query.edit_message_text("Send delay in seconds (example: 0.8):", reply_markup=reply_markup)

async def handle_custom_delay(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    try:
        delay = float(text)
        if delay <= 0:
            raise ValueError
        settings = load_settings(user_id)
        settings['delay'] = delay
        save_settings(user_id, settings)
        WAITING_FOR.pop(user_id, None)
        await update.message.reply_text(f"✅ Delay set to {delay}s")
    except:
        await update.message.reply_text("❌ Invalid value!")
        WAITING_FOR.pop(user_id, None)

# ========== MESSAGE HANDLER ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = WAITING_FOR.get(user_id)

    if state == 'phone':
        await handle_phone(update, context, text)
    elif state == 'session_string':
        await handle_session_string(update, context, text)
    elif state == 'bulk_sessions':
        await handle_bulk_sessions(update, context, text)
    elif state == 'pyrogram_session':
        await handle_pyrogram_session(update, context, text)
    elif state == 'campaign_link':
        await handle_campaign_link(update, context, text)
    elif state == 'spam_message':
        await handle_spam_message(update, context, text)
    elif state == 'account_count':
        await handle_account_count(update, context, text)
    elif state == 'custom_delay':
        await handle_custom_delay(update, context, text)
    elif state == 'admin_remove_phone':
        await admin_remove_account(update, context, text)
    elif state == 'user_remove_phone':
        await user_remove_account(update, context, text)
    elif state == 'admin_campaign_user_id':
        await handle_admin_campaign_user(update, context, text)
    elif state == 'admin_ban_user_id':
        await handle_admin_ban_user(update, context, text)
    elif state == 'admin_unban_user_id':
        await handle_admin_unban_user(update, context, text)
    elif state == 'admin_grant_admin_id':
        await handle_admin_grant_admin(update, context, text)
    elif state == 'admin_revoke_admin_id':
        await handle_admin_revoke_admin(update, context, text)
    elif state == 'access_user_id':
        await handle_access_user_id(update, context, text)
    elif state == 'access_days':
        await handle_access_days(update, context, text)
    elif state == 'access_shared_limit':
        await handle_access_shared_limit(update, context, text)
    elif state == 'remove_access_user_id':
        await handle_remove_access(update, context, text)
    elif state == 'schedule_link':
        await handle_schedule_link(update, context, text)
    elif state == 'schedule_spam_message':
        await handle_schedule_spam_message(update, context, text)
    elif state == 'schedule_time':
        await handle_schedule_time(update, context, text)
    elif state == 'schedule_account_count':
        await handle_schedule_account_count(update, context, text)
    elif state == 'cancel_schedule_id':
        await handle_cancel_schedule(update, context, text)
    elif state == 'private_view_link':
        await handle_private_view(update, context, text)
    elif state == 'leave_specific_link':
        await handle_leave_specific(update, context, text)
    elif state == 'premium_link':
        await handle_premium_link(update, context, text)
    elif state == 'broadcast_message':
        await handle_broadcast(update, context, text)
    elif state == 'schedule_action':
        pass
    elif text.isdigit() and len(text) == 5 and user_id in PENDING_OTP:
        await verify_otp(update, context, text)
    elif user_id in PENDING_2FA:
        await verify_2fa(update, context, text)
    else:
        await update.message.reply_text("❌ Send /start")

# ========== CALLBACK HANDLER ==========
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except:
        pass

    data = query.data

    if data == "main":
        await show_main_menu(update, context)
    elif data == "add_account":
        await add_account_menu(update, context)
    elif data == "add_phone_otp":
        await add_phone_otp(update, context)
    elif data == "add_session_string":
        await add_session_string(update, context)
    elif data == "add_pyrogram_session":
        await add_pyrogram_session(update, context)
    elif data == "add_bulk_sessions":
        await add_bulk_sessions(update, context)
    elif data == "add_zip":
        await add_zip(update, context)
    elif data == "my_accounts":
        await my_accounts(update, context)
    elif data == "new_campaign":
        await new_campaign(update, context)
    elif data == "private_channel_view":
        await private_channel_view(update, context)
    elif data == "leave_channel_menu":
        await leave_channel_menu(update, context)
    elif data == "leave_specific_channel":
        await leave_specific_channel(update, context)
    elif data == "leave_all_channels":
        await leave_all_channels(update, context)
    elif data == "confirm_leave_all":
        await confirm_leave_all(update, context)
    elif data == "scheduled":
        await scheduled_menu(update, context)
    elif data == "schedule_new":
        await schedule_new(update, context)
    elif data == "schedule_cancel":
        await schedule_cancel(update, context)
    elif data.startswith("schedule_action_"):
        action = data.replace("schedule_action_", "")
        await schedule_action_handler(update, context, action)
    elif data.startswith("campaign_action_"):
        action = data.replace("campaign_action_", "")
        await campaign_action_handler(update, context, action)
    elif data == "campaign_normal_emoji":
        await campaign_normal_emoji(update, context)
    elif data == "campaign_premium_mode":
        await campaign_premium_mode(update, context)
    elif data.startswith("select_emoji_"):
        await select_emoji_callback(update, context)
    elif data == "emoji_ready":
        await emoji_ready_callback(update, context)
    elif data.startswith("select_premium_"):
        await campaign_premium_mode(update, context)
    elif data.startswith("use_all_"):
        await use_all_callback(update, context)
    elif data == "run_campaign":
        await run_campaign(update, context)
    elif data == "my_campaigns":
        await my_campaigns(update, context)
    elif data == "my_stats":
        await my_stats(update, context)
    elif data == "settings":
        await settings_menu(update, context)
    elif data == "profile":
        await profile(update, context)
    elif data == "help":
        await help_guide(update, context)
    elif data == "support":
        await support(update, context)
    elif data.startswith("delay_"):
        await set_delay(update, context, data.replace("delay_", ""))
    elif data == "custom_delay":
        await custom_delay(update, context)
    elif data == "admin_panel":
        await admin_panel(update, context)
    elif data == "admin_campaign_all":
        await admin_campaign_all(update, context)
    elif data == "admin_campaign_user":
        await admin_campaign_user(update, context)
    elif data == "admin_ban_user":
        await admin_ban_user(update, context)
    elif data == "admin_unban_user":
        await admin_unban_user(update, context)
    elif data == "admin_grant_admin":
        await admin_grant_admin(update, context)
    elif data == "admin_revoke_admin":
        await admin_revoke_admin(update, context)
    elif data == "admin_all_campaigns":
        await admin_all_campaigns(update, context)
    elif data == "admin_all_users":
        await admin_all_users_list(update, context)
    elif data == "admin_view_live":
        await admin_view_live(update, context)
    elif data == "admin_view_expired":
        await admin_view_expired(update, context)
    elif data == "admin_remove_prompt":
        await admin_remove_prompt(update, context)
    elif data == "admin_remove_all_prompt":
        await admin_remove_all_prompt(update, context)
    elif data == "admin_remove_all_confirm":
        await admin_remove_all_confirm(update, context)
    elif data == "admin_remove_all_expired":
        await admin_remove_all_expired(update, context)
    elif data == "user_remove_prompt":
        await user_remove_prompt(update, context)
    elif data == "give_access":
        await give_access_start(update, context)
    elif data == "remove_access":
        await remove_access_start(update, context)
    elif data == "access_more_yes":
        await access_more_yes(update, context)
    elif data == "access_more_no_direct":
        await access_more_no_direct(update, context)
    elif data == "admin_export_db":
        if not is_owner(query.from_user.id):
            await query.answer("❌ Only Owner!", show_alert=True)
            return
        await query.answer()
        await context.bot.send_message(chat_id=query.message.chat_id, text="Use /export_db command to export the database.")
    elif data == "admin_import_db":
        if not is_owner(query.from_user.id):
            await query.answer("❌ Only Owner!", show_alert=True)
            return
        await query.answer()
        WAITING_FOR[query.from_user.id] = "import_db_file"
        keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📥 IMPORT DATABASE\n\n"
            "ℹ️ Existing user IDs/accounts are preserved and merged. Other users’ accounts will NOT be copied to the owner.\n\n"
            "Send the automation_bot_export.json file now:",
            reply_markup=reply_markup
        )
    elif data == "admin_broadcast":
        await admin_broadcast(update, context)
    elif data == "premium_reacted":
        await premium_reacted_callback(update, context)
    elif data == "premium_reacted_for_action":
        await premium_reacted_for_action(update, context)
    elif data == "reaction_type_normal":
        await reaction_type_normal(update, context)
    elif data == "reaction_type_premium":
        await reaction_type_premium(update, context)
    elif data == "reaction_type_done":
        await reaction_type_done(update, context)
    elif data == "reaction_type_back":
        await reaction_type_back(update, context)
    elif data == "cancel_action":
        await cancel_button_handler(update, context)

# ========== SESSION AUTO-SYNC ON START ==========
async def sync_accounts_on_start():
    """Background task to pre-check liveness of all accounts and populate cache."""
    logging.info("🔄 Syncing accounts on startup...")
    all_users = get_all_users()
    total = 0
    for u in all_users:
        uid = u[0]
        accounts = load_accounts(uid)
        for acc in accounts:
            if acc.get('type') == 'pyrogram':
                continue
            await is_account_live(acc)
            total += 1
            await asyncio.sleep(0.1)
    logging.info(f"✅ Synced {total} accounts across {len(all_users)} users.")

# ========== COLOURED INLINE BUTTON HELPER ==========
def _button_style(label, callback_data=None):
    """Choose a Telegram inline-button colour without changing button behavior."""
    text = str(label or "").casefold()
    data = str(callback_data or "").casefold()
    danger_words = ("cancel", "remove", "delete", "revoke", "ban", "stop", "leave all", "expired")
    success_words = ("add", "run", "yes", "confirm", "ready", "grant", "approve", "join", "success", "all (")
    if any(word in text or word in data for word in danger_words):
        return "danger"
    if any(word in text or word in data for word in success_words):
        return "success"
    return "primary"


def styled_button(label, callback_data, style=None):
    """Return a Bot API inline-button dictionary with a native colour style."""
    return {
        "text": label,
        "callback_data": callback_data,
        "style": style if style in {"primary", "success", "danger"} else _button_style(label, callback_data),
    }


def InlineKeyboardButton(text, *args, **kwargs):
    """Compatibility wrapper so direct PTB buttons receive the same colours."""
    callback_data = kwargs.get("callback_data")
    if callback_data is None and args:
        callback_data = args[0]
    style = kwargs.pop("style", None) or _button_style(text, callback_data)
    try:
        # Newer python-telegram-bot versions pass unknown Bot API fields via api_kwargs.
        api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
        api_kwargs.setdefault("style", style)
        return _TelegramInlineKeyboardButton(text, *args, api_kwargs=api_kwargs, **kwargs)
    except (TypeError, AttributeError):
        # Keep compatibility with older PTB versions; styled_button dictionaries
        # still carry the native style field wherever this wrapper is converted.
        return _TelegramInlineKeyboardButton(text, *args, **kwargs)

# ========== GRACEFUL SHUTDOWN ==========
async def shutdown_resources(app, background_tasks):
    """Stop background work and disconnect all Telethon clients before loop close."""
    for task in list(background_tasks):
        if not task.done():
            task.cancel()

    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)

    clients = []
    seen = set()
    for client in list(ACTIVE_CLIENTS.values()):
        if id(client) not in seen:
            seen.add(id(client))
            clients.append(client)
    for pending in list(PENDING_OTP.values()) + list(PENDING_2FA.values()):
        client = pending.get('client')
        if client is not None and id(client) not in seen:
            seen.add(id(client))
            clients.append(client)

    for client in clients:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass

    ACTIVE_CLIENTS.clear()
    PENDING_OTP.clear()
    PENDING_2FA.clear()

    if app is not None:
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
        except Exception:
            pass
        try:
            if app.running:
                await app.stop()
        except Exception:
            pass
        try:
            await app.shutdown()
        except Exception:
            pass


# ========== MAIN ==========
async def main():
    logging.basicConfig(level=logging.ERROR)
    logging.getLogger('telethon').setLevel(logging.ERROR)
    logging.getLogger('httpx').setLevel(logging.ERROR)

    init_db()
    os.makedirs("sessions", exist_ok=True)
    os.makedirs("data", exist_ok=True)

    # Automatically merge the supplied export; existing SQLite data is kept.
    auto_import_embedded_data()

    app = None
    background_tasks = set()

    try:
        # Start scheduled checker
        background_tasks.add(asyncio.create_task(check_scheduled_campaigns()))

        # Start background sync
        background_tasks.add(asyncio.create_task(sync_accounts_on_start()))
        BACKGROUND_TASKS.update(background_tasks)

        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("export_db", cmd_export_db))
        app.add_handler(CommandHandler("import_db", cmd_import_db))
        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_error_handler(error_handler)

        print("=" * 50)
        print("🤖 AUTOMATION VOTE BOT STARTED (SQLite)!")
        print("=" * 50)
        print(f"✅ Owner ID: {OWNER_ID}")
        print("=" * 50)
        print("✅ ALL ACTIONS WORKING (PUBLIC + PRIVATE):")
        print("   - Join Channel ✅")
        print("   - Leave Specific Channel ✅")
        print("   - LEAVE ALL Channels ✅")
        print("   - React Only | Different Reactions ✅")
        print("   - Multiple Reactions (Select Emojis) ✅")
        print("   - Premium Emoji (Auto-Detect) ✅")
        print("   - View Only (GetMessagesViewsRequest) ✅")
        print("   - PRIVATE CHANNEL VIEW (Auto-join + View) ✅")
        print("   - Vote Only | React + Vote | React + View ✅")
        print("   - Vote + View | React + Vote + View ✅")
        print("   - Bulk DM | VC (Voice Chat) ✅")
        print("   - Group Spam ✅")
        print("=" * 50)
        print("✅ ZIP UPLOAD: Add accounts via ZIP file (with timeouts!)")
        print("✅ ADMIN GRANT: Owner can grant admin rights")
        print("✅ ACCESS MANAGEMENT: Added to Admin Panel")
        print("✅ BROADCAST: Admin can broadcast messages")
        print("✅ PYROGRAM SESSION: Stored (beta, not usable)")
        print("=" * 50)
        print("✅ DB: SQLite (automation_bot.db)")
        print("✅ DB COMMANDS: /export_db | /import_db (JSON)")
        print("=" * 50)

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()
    finally:
        await shutdown_resources(app, background_tasks)
        BACKGROUND_TASKS.difference_update(background_tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
